"""Read replies aloud with a local neural voice, following a reply while it streams."""

import threading
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import pyaudiowpatch  # type: ignore[import-untyped]
from numpy.typing import NDArray
from PyQt6.QtCore import QObject, pyqtSignal

from core.settings import SettingsStore
from core.transcriber import ONNX_THREADS, PORTAUDIO_LOCK, _wasapi_devices
from ui.chat.speech_text import SpeechUnit, speech_units

VOICE_REPO = "fastrtc/kokoro-onnx"
VOICE_REVISION = "8d07950c9b6c87ce6809e9bba7bd494336217c2a"  # Pinned so the voice never changes under the user
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"
VOICE = "af_heart"
VOICE_RATE = 24000  # Kokoro's sample rate
SPEED = 1.0
WAIT_SECONDS = (
    0.05  # How often the speech thread rechecks the stream and the playback queue
)
BUFFER_SECONDS = 0.02  # Audio handed to Windows per callback
MISSING_VOICE_STATUS = "No speech model yet. Download Que's voice in Settings → Voice."
DOWNLOADING_VOICE_STATUS = (
    "Que's voice is still downloading. Start speech once it finishes."
)
LOAD_FAILED = "Could not load Que's voice. Try again, or restart Que."
DEVICE_FAILED = "Could not play speech. Reconnect the speech output device or choose another in Settings → Audio."


class Speaker(QObject):
    """Speak one reply at a time on the chosen output device.

    The Qt thread says what to read; one daemon thread synthesizes a sentence ahead of the
    one playing and hands audio to Windows. Reading waits whenever it catches up with a
    streaming reply and carries on as soon as the next whole word arrives.
    """

    spoken = pyqtSignal(
        str
    )  # Words handed to the output, for the transcript's echo filter
    download_finished = pyqtSignal(str)  # Error or an empty string
    model_needed = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self, settings_store: SettingsStore, cache_dir: Path | None = None
    ) -> None:
        super().__init__()
        self.settings_store = settings_store
        self.cache_dir = cache_dir or (
            Path(__file__).resolve().parent.parent / "data" / "cache" / "kokoro"
        )
        self.downloading = False
        self.download_finished.connect(self._on_download_finished)
        self.condition = threading.Condition()
        self.reply: object | None = None  # The bubble being read; Qt thread only
        # Shared with the speech thread under the condition
        self.text = ""
        self.complete = True
        self.cursor = 0
        self.active = False
        self.paused = False
        self.generation = 0
        self.output: _Output | None = None
        self.voice: Any = (
            None  # The loaded Kokoro voice, kept for the rest of the session
        )
        self.speech_thread: threading.Thread | None = None

    def model_downloaded(self) -> bool:
        """Check whether both voice files are in the local cache.

        Returns:
            bool: Whether the model and its voices are available.
        """
        return (self.cache_dir / MODEL_FILE).is_file() and (
            self.cache_dir / VOICES_FILE
        ).is_file()

    def download(self) -> bool:
        """Download the model on demand in a background thread.

        Returns:
            bool: Whether a new download was started.
        """
        if self.downloading or self.model_downloaded():
            return False
        self.downloading = True
        threading.Thread(target=self._download, daemon=True).start()
        return True

    def speak(self, reply: object, text: str, complete: bool, offset: int = 0) -> None:
        """Read a reply from an offset, replacing whatever was being read.

        Args:
            reply (object): The bubble whose text this is; later updates must name it.
            text (str): The reply's Markdown so far.
            complete (bool): Whether the reply has finished streaming.
            offset (int): The Markdown offset to start reading from.
        """
        if self.voice is None and not self.model_downloaded():
            self.model_needed.emit(
                DOWNLOADING_VOICE_STATUS if self.downloading else MISSING_VOICE_STATUS
            )
            return
        self.reply = reply
        with self.condition:
            self.generation += 1
            self.text = text
            self.complete = complete
            self.cursor = offset
            self.paused = False
            self.active = True
            if self.output is not None:
                self.output.clear()
            self.condition.notify_all()
        if self.speech_thread is None or not self.speech_thread.is_alive():
            self.speech_thread = threading.Thread(target=self._run, daemon=True)
            self.speech_thread.start()

    def update(self, reply: object, text: str, complete: bool) -> None:
        """Pass new streamed text of the reply being read to the speech thread.

        Args:
            reply (object): The bubble that changed.
            text (str): Its Markdown so far.
            complete (bool): Whether it has finished streaming.
        """
        if reply is not self.reply:
            return
        with self.condition:
            self.text = text
            self.complete = complete
            self.condition.notify_all()

    def toggle_pause(self) -> None:
        """Pause or resume reading; does nothing when nothing is being read."""
        with self.condition:
            if self.active:
                self.paused = not self.paused
                self.condition.notify_all()

    def stop(self) -> None:
        """Stop reading at once and release the output device."""
        self.reply = None
        with self.condition:
            self.generation += 1
            self.active = False
            self.paused = False
            if self.output is not None:
                self.output.clear()
            self.condition.notify_all()

    def _run(self) -> None:
        """Read whatever the Qt thread asks for, for the rest of the session."""
        while True:
            with self.condition:
                while not self.active:
                    self.condition.wait()
            try:
                voice = self._load_voice()
            except Exception as error:  # noqa: BLE001  # A load failure only stops speech
                self._finish(str(error))
                continue
            try:
                self._read(voice)
            except OSError:
                self._finish(DEVICE_FAILED)
            finally:
                with self.condition:
                    output, self.output = self.output, None
                if output is not None:
                    output.close()

    def _read(self, voice: Any) -> None:
        """Synthesize and play sentences until the reply is read or reading stops.

        Args:
            voice (Any): The loaded Kokoro voice.
        """
        while True:
            with self.condition:
                if not self.active:
                    return
                generation = self.generation
                units = speech_units(self.text, self.complete, self.cursor)
                drained = self.output is None or self.output.drained()
                if not units:
                    if self.complete and drained:
                        self.active = False
                        return
                    self.condition.wait(WAIT_SECONDS)
                    continue
                unit = units[0]
                if unit.open and not drained:
                    # Finish what is playing before reading a sentence the stream may still extend
                    self.condition.wait(WAIT_SECONDS)
                    continue
            samples = self._synthesize(voice, unit)
            with self.condition:
                if generation != self.generation or not self.active:
                    continue  # Stopped or restarted while this sentence was synthesized
            output = self.output
            if output is None:
                # Opened outside the condition so a slow device never blocks the Qt thread
                output = _Output(self)
                with self.condition:
                    self.output = output
            with self.condition:
                # Queued under the condition so a restart cannot be followed by stale audio
                if generation == self.generation:
                    self.cursor = unit.end
                    if samples.size:
                        output.push(samples, unit.pause)
            if samples.size:
                self.spoken.emit(unit.text)
            with self.condition:
                # Stay one sentence ahead of the one playing
                while (
                    self.active
                    and generation == self.generation
                    and output.queued() > 1
                ):
                    self.condition.wait(WAIT_SECONDS)

    def _synthesize(self, voice: Any, unit: SpeechUnit) -> NDArray[np.float32]:
        """Turn one sentence into audio, or silence if the voice cannot say it.

        Args:
            voice (Any): The loaded Kokoro voice.
            unit (SpeechUnit): The sentence.

        Returns:
            NDArray[np.float32]: Mono samples at Kokoro's rate.
        """
        try:
            samples, _rate = voice.create(
                unit.text, voice=VOICE, speed=SPEED, lang="en-us"
            )
        except Exception:  # noqa: BLE001  # A sentence the phonemizer rejects is skipped, not fatal
            return np.zeros(0, np.float32)
        return np.asarray(samples, np.float32).ravel()

    def _download(self) -> None:
        """Download the pinned voice files into the cache and report the outcome."""
        try:
            from huggingface_hub import (  # Slow import, only when downloading
                snapshot_download,
            )

            snapshot_download(
                VOICE_REPO,
                revision=VOICE_REVISION,
                local_dir=str(self.cache_dir),
                allow_patterns=[MODEL_FILE, VOICES_FILE],
            )
            if not self.model_downloaded():
                raise OSError("The download did not include both voice files.")
        except Exception as error:  # noqa: BLE001
            self.download_finished.emit(str(error) or type(error).__name__)
            return
        self.download_finished.emit("")

    def _on_download_finished(self, _error: str) -> None:
        """Allow a new download once the worker has finished."""
        self.downloading = False

    def _load_voice(self) -> Any:
        """Load the local voice once for the session.

        Returns:
            Any: The Kokoro voice.
        """
        if self.voice is not None:
            return self.voice
        model = self.cache_dir / MODEL_FILE
        voices = self.cache_dir / VOICES_FILE
        try:
            import onnxruntime  # type: ignore[import-untyped]
            from kokoro_onnx import Kokoro  # Slow import, only when speaking

            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = ONNX_THREADS
            session = onnxruntime.InferenceSession(
                str(model), options, providers=["CPUExecutionProvider"]
            )
            self.voice = Kokoro.from_session(session, str(voices))
        except Exception as error:
            raise RuntimeError(LOAD_FAILED) from error
        return self.voice

    def _finish(self, message: str) -> None:
        """End reading after a failure and tell the user why.

        Args:
            message (str): What went wrong.
        """
        with self.condition:
            self.active = False
        self.failed.emit(message)


class _Output:
    """Play queued clips on the chosen WASAPI output, with silence while paused or waiting."""

    def __init__(self, speaker: Speaker) -> None:
        self.speaker = speaker
        self.lock = threading.Lock()
        self.clips: deque[NDArray[np.float32]] = deque()
        self.position = 0  # Samples of the first clip already played
        with PORTAUDIO_LOCK:
            self.audio = pyaudiowpatch.PyAudio()
            try:
                device = _output_device(
                    self.audio, speaker.settings_store.audio_device("speaker")
                )
                self.rate = int(device["defaultSampleRate"])
                self.channels = int(device["maxOutputChannels"])
                self.stream = self.audio.open(
                    format=pyaudiowpatch.paFloat32,
                    channels=self.channels,
                    rate=self.rate,
                    output=True,
                    output_device_index=int(device["index"]),
                    frames_per_buffer=int(self.rate * BUFFER_SECONDS),
                    stream_callback=self._fill,
                )
            except Exception as error:
                self.audio.terminate()
                raise OSError(DEVICE_FAILED) from error

    def push(self, samples: NDArray[np.float32], pause: float) -> None:
        """Queue one clip followed by its pause.

        Args:
            samples (NDArray[np.float32]): Mono samples at Kokoro's rate.
            pause (float): Seconds of silence after the clip.
        """
        length = round(len(samples) * self.rate / VOICE_RATE)
        resampled = np.interp(
            np.arange(length) * (VOICE_RATE / self.rate),
            np.arange(len(samples)),
            samples,
        ).astype(np.float32)
        clip = np.concatenate(
            [resampled, np.zeros(round(pause * self.rate), np.float32)]
        )
        with self.lock:
            self.clips.append(clip)

    def queued(self) -> int:
        """Count the clips not yet fully played.

        Returns:
            int: The number of queued clips, including the one playing.
        """
        with self.lock:
            return len(self.clips)

    def drained(self) -> bool:
        """Report whether everything queued has played.

        Returns:
            bool: Whether the queue is empty.
        """
        return self.queued() == 0

    def clear(self) -> None:
        """Drop every queued clip so playback falls silent at once."""
        with self.lock:
            self.clips.clear()
            self.position = 0

    def close(self) -> None:
        """Stop the stream and release the device."""
        with PORTAUDIO_LOCK:
            try:
                self.stream.stop_stream()
                self.stream.close()
            finally:
                self.audio.terminate()

    def _fill(
        self, _data: bytes | None, frames: int, _time: Any, _status: int
    ) -> tuple[bytes, int]:
        """Hand Windows the next frames, or silence while paused or waiting for text.

        Args:
            frames (int): The frames Windows asks for.

        Returns:
            tuple[bytes, int]: Interleaved float frames and the continue flag.
        """
        mono = np.zeros(frames, np.float32)
        if not self.speaker.paused:
            filled = 0
            with self.lock:
                while filled < frames and self.clips:
                    clip = self.clips[0]
                    take = min(frames - filled, len(clip) - self.position)
                    mono[filled : filled + take] = clip[
                        self.position : self.position + take
                    ]
                    filled += take
                    self.position += take
                    if self.position >= len(clip):
                        self.clips.popleft()
                        self.position = 0
        frames_out = np.repeat(mono[:, None], self.channels, axis=1)
        return frames_out.tobytes(), int(pyaudiowpatch.paContinue)


def _output_device(audio: Any, name: str) -> dict[str, Any]:
    """Find the saved speech output, or the Windows default output.

    Args:
        audio (Any): The initialized PyAudio host.
        name (str): The saved WASAPI device name, or an empty string for the default.

    Returns:
        dict[str, Any]: The device's PortAudio information.
    """
    if name:
        matches = [
            device
            for device in _wasapi_devices(audio)["speaker"]
            if device["name"] == name
        ]
        if len(matches) != 1:
            raise OSError(DEVICE_FAILED)
        return matches[0]
    wasapi = audio.get_host_api_info_by_type(pyaudiowpatch.paWASAPI)
    return dict(audio.get_device_info_by_index(wasapi["defaultOutputDevice"]))
