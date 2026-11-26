"""Read replies aloud with a local neural voice, following a reply while it streams."""

import contextlib
import ctypes
import queue
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from math import ceil, isfinite
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np
import pyaudiowpatch  # type: ignore[import-untyped]
from numpy.typing import NDArray
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal

from core.settings import SettingsStore
from core.speech_timing import (
    VOICE_RATE,
    AudioCue,
    ReadingPosition,
    load_voice,
    synthesize,
)
from core.transcriber import ONNX_THREADS, PORTAUDIO_LOCK, _wasapi_devices
from ui.chat.speech_text import SpeechUnit, speech_units

VOICE_REPO = "fastrtc/kokoro-onnx"
VOICE_REVISION = "8d07950c9b6c87ce6809e9bba7bd494336217c2a"  # Pinned so the voice never changes under the user
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"
VOICE = "af_heart"
VOICE_SAMPLE = "This is how Que will read your replies aloud."
WAIT_SECONDS = (
    0.05  # How often the speech thread rechecks the stream and the playback queue
)
BUFFER_SECONDS = 0.02  # Audio handed to Windows per callback
# Longest leading pieces of the first sentence read, so sound starts before the whole sentence is made.
# Each is short enough to be synthesized while the one before it plays.
OPENING_LIMITS = (20, 60, 150)
MIN_PIECE_CHARS = 12
# Reused because a thread's first inference is far slower; two, so a replaced read never delays the next
SYNTHESIS_WORKERS = 2
WARM_UP_SECONDS = 10
CLAUSE_END_RE = re.compile(r"[,;:](?=\s)")
MISSING_VOICE_STATUS = "No speech model yet. Download Que's voice in Settings → Voice."
DOWNLOADING_VOICE_STATUS = (
    "Que's voice is still downloading. Start speech once it finishes."
)
LOAD_FAILED = "Could not load Que's voice. Try again, or restart Que."
DEVICE_FAILED = "Could not play speech. Reconnect the speech output device or choose another in Settings → Audio."

WAVE_MAPPER = 0xFFFFFFFF
DRVM_MAPPER_PREFERRED_GET = 0x2015
DRV_QUERYDEVICEINTERFACE = 0x80C
DRV_QUERYDEVICEINTERFACESIZE = 0x80D

_winmm = ctypes.WinDLL("winmm")
_winmm.waveOutMessage.argtypes = [
    ctypes.c_size_t,  # A device number, passed where Windows also accepts an open handle
    ctypes.c_uint,
    ctypes.c_void_p,
    ctypes.c_void_p,
]

type _PlaybackEvent = tuple[float, ReadingPosition | None]


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
    reading_changed = pyqtSignal(object, str, object)  # Reply, phase, current position
    _playback_events = pyqtSignal(
        int, int, object
    )  # Generation, pause revision, timed positions
    _reading_ended = pyqtSignal(int)

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
        self.reply: object | None = None  # Request identity, Qt thread only
        # Shared with the speech thread under the condition
        self.text = ""
        self.complete = True
        self.cursor = 0
        self.active = False
        self.paused = False
        self.generation = 0
        self.playback_revision = 0
        self.output: _Output | None = None
        self.voice: Any = (
            None  # The loaded Kokoro voice, kept for the rest of the session
        )
        self.speech_thread: threading.Thread | None = None
        self.synthesis_jobs: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self.synthesis_threads: list[threading.Thread] = []
        self.synthesis_lock = threading.Lock()
        self.voice_lock = threading.Lock()
        self.device_lock = (
            threading.Lock()
        )  # Guards opening, parking and closing output
        self.playback_events: deque[_PlaybackEvent] = deque()
        self.reading_position: ReadingPosition | None = None
        self.reading_open = False
        self.playback_timer = QTimer(self)
        self.playback_timer.setSingleShot(True)
        self.playback_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.playback_timer.timeout.connect(self._advance_reading_position)
        self._playback_events.connect(self._on_playback_events)
        self._reading_ended.connect(self._on_reading_ended)

    def model_downloaded(self) -> bool:
        """Check whether both voice files are in the local cache.

        Returns:
            bool: Whether the model and its voices are available.
        """
        return (self.cache_dir / MODEL_FILE).is_file() and (
            self.cache_dir / VOICES_FILE
        ).is_file()

    def preload(self) -> None:
        """Load a downloaded voice in the background so the first read need not wait for it."""
        if self.voice is None and self.model_downloaded():
            threading.Thread(target=self._preload, daemon=True).start()

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
        self._reset_reading_position()
        self.reading_open = True
        self.reading_changed.emit(reply, "start", None)
        if self.speech_thread is None or not self.speech_thread.is_alive():
            self.speech_thread = threading.Thread(target=self._run, daemon=True)
            self.speech_thread.start()

    def speak_sample(self) -> None:
        """Read a sample sentence using the current speech settings."""
        self.speak(object(), VOICE_SAMPLE, True)

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
            if not self.active:
                return
            self.paused = not self.paused
            self.playback_revision += 1
            paused = self.paused
            self.condition.notify_all()
        self.playback_timer.stop()
        self.playback_events.clear()
        self.reading_changed.emit(
            self.reply, "pause" if paused else "resume", self.reading_position
        )

    def stop(self) -> None:
        """Stop reading at once."""
        self.reply = None
        with self.condition:
            self.generation += 1
            self.active = False
            self.paused = False
            if self.output is not None:
                self.output.clear()
            self.condition.notify_all()
        self._reset_reading_position()
        self.reading_open = False
        self.reading_changed.emit(None, "finish", None)

    def release_idle_output(self) -> None:
        """Release the output held between reads, so PortAudio can list the devices afresh."""
        with self.device_lock:
            output = self.output
            if output is None or output.claimed:
                return
            with self.condition:
                self.output = None
            output.close()

    def close(self) -> None:
        """Stop reading and release the output device."""
        self.stop()
        with self.device_lock:
            with self.condition:
                output, self.output = self.output, None
            if output is not None:
                output.close()

    def _run(self) -> None:
        """Read whatever the Qt thread asks for, for the rest of the session."""
        while True:
            with self.condition:
                while not self.active:
                    self.condition.wait()
                generation = self.generation
            try:
                voice = self._load_voice()
            except Exception as error:  # noqa: BLE001  # A load failure only stops speech
                self._finish(generation, str(error))
                continue
            ended = None
            try:
                ended = self._read(voice)
            finally:
                self._park_output()
            if ended is not None:
                self._reading_ended.emit(ended)

    def _read(self, voice: Any) -> int | None:
        """Synthesize and play sentences until the reply is read or reading stops.

        Args:
            voice (Any): The loaded Kokoro voice.

        Returns:
            int | None: The generation that finished naturally, if it reached the end.
        """
        opened = (
            None  # The reading whose first sentence has already been cut into pieces
        )
        while True:
            with self.condition:
                if not self.active:
                    return None
                generation = self.generation
                units = speech_units(
                    self.text, self.complete, self.cursor, first_only=True
                )
                drained = self.output is None or self.output.drained()
                if not units:
                    if self.complete and drained:
                        self.active = False
                        return generation
                    self.condition.wait(WAIT_SECONDS)
                    continue
                unit = units[0]
                if unit.open and not drained:
                    # Finish what is playing before reading a sentence the stream may still extend
                    self.condition.wait(WAIT_SECONDS)
                    continue
            pieces = [unit] if opened == generation else _opening_pieces(unit)
            opened = generation
            for number, piece in enumerate(pieces):
                result = self._start_synthesis(voice, piece, generation)
                # Opened while the sentence is made, and outside the condition so a slow
                # device never blocks the Qt thread
                try:
                    output = self._claim_output()
                except OSError:
                    self._finish(generation, DEVICE_FAILED)
                    return None
                with self.condition:
                    # A restart or stop abandons this sentence without waiting for it
                    while not result and generation == self.generation and self.active:
                        self.condition.wait(WAIT_SECONDS)
                    if generation != self.generation or not self.active:
                        break
                    # Queued under the condition so a restart cannot be followed by stale audio
                    samples, cues = result[0]
                    if number == len(pieces) - 1:
                        self.cursor = unit.end
                    if samples.size:
                        output.push(samples, piece.pause, cues, generation)
                if samples.size and not number:
                    self.spoken.emit(unit.text)
                with self.condition:
                    # Stay one sentence ahead of the one playing
                    while (
                        self.active
                        and generation == self.generation
                        and output.queued() > 1
                    ):
                        self.condition.wait(WAIT_SECONDS)

    def _start_synthesis(
        self, voice: Any, unit: SpeechUnit, generation: int
    ) -> list[tuple[NDArray[np.float32], tuple[AudioCue, ...]]]:
        """Queue a sentence for a synthesis worker.

        Args:
            voice (Any): The loaded Kokoro voice.
            unit (SpeechUnit): The sentence.
            generation (int): The reading that wants it.

        Returns:
            list[tuple[NDArray[np.float32], tuple[AudioCue, ...]]]: Receives the audio when ready.
        """
        result: list[tuple[NDArray[np.float32], tuple[AudioCue, ...]]] = []

        def job() -> None:
            """Deliver the audio and wake the speech thread, unless the reading was replaced."""
            if generation != self.generation:
                return
            audio = self._synthesize(voice, unit)
            with self.condition:
                result.append(audio)
                self.condition.notify_all()

        self._submit_synthesis(job)
        return result

    def _submit_synthesis(self, job: Callable[[], None]) -> None:
        """Hand a job to the synthesis workers, starting them on first use.

        Args:
            job (Callable[[], None]): The work to run.
        """
        with self.synthesis_lock:
            if not self.synthesis_threads:
                self.synthesis_threads = [
                    threading.Thread(target=self._work, daemon=True)
                    for _ in range(SYNTHESIS_WORKERS)
                ]
                for thread in self.synthesis_threads:
                    thread.start()
        self.synthesis_jobs.put(job)

    def _work(self) -> None:
        """Run synthesis jobs for the rest of the session."""
        while True:
            self.synthesis_jobs.get()()

    def _claim_output(self) -> "_Output":
        """Return a running output on the current device, reusing the one held between reads.

        Returns:
            _Output: The output, which stays claimed until reading ends.
        """
        with self.device_lock:
            output = self.output
            if output is not None:
                if output.claimed:
                    return output
                try:
                    if output.key == _device_key(
                        self.settings_store.audio_device("speaker")
                    ):
                        output.stream.start_stream()
                        output.claimed = True
                        return output
                except OSError:
                    pass  # The device went away while held; open the current one
                with self.condition:
                    self.output = None
                output.close()
            output = _Output(self)
            try:
                output.stream.start_stream()
            except OSError:
                output.close()
                raise
            with self.condition:
                self.output = output
            return output

    def _park_output(self) -> None:
        """Stop the output between reads but keep the device open for the next one."""
        with self.device_lock:
            output = self.output
            if output is None:
                return
            try:
                output.stream.stop_stream()
            except OSError:
                with self.condition:
                    self.output = None
                output.close()
                return
            output.clear()
            output.claimed = False

    def _synthesize(
        self, voice: Any, unit: SpeechUnit
    ) -> tuple[NDArray[np.float32], tuple[AudioCue, ...]]:
        """Turn one sentence into audio, or silence if the voice cannot say it.

        Args:
            voice (Any): The loaded Kokoro voice.
            unit (SpeechUnit): The sentence.

        Returns:
            tuple[NDArray[np.float32], tuple[AudioCue, ...]]: Mono samples and positions.
        """
        try:
            return synthesize(
                voice, unit, VOICE, self.settings_store.speech_speed() / 100
            )
        except Exception:  # noqa: BLE001  # A sentence the phonemizer rejects is skipped, not fatal
            return np.zeros(0, np.float32), ()

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
        with self.voice_lock:
            if self.voice is not None:
                return self.voice
            model = self.cache_dir / MODEL_FILE
            voices = self.cache_dir / VOICES_FILE
            try:
                self.voice = load_voice(model, voices, ONNX_THREADS)
            except Exception as error:
                raise RuntimeError(LOAD_FAILED) from error
            return self.voice

    def _preload(self) -> None:
        """Load the voice and have each synthesis worker make one unheard word."""
        try:
            voice = self._load_voice()
        except RuntimeError:
            return  # The next read tries again and reports the failure
        # Holds each worker until all have a warm-up job, so none takes two
        barrier = threading.Barrier(SYNTHESIS_WORKERS)

        def warm_up() -> None:
            """Pay this thread's slow first inference before the user asks for speech."""
            self._synthesize(voice, SpeechUnit("Ready.", 0, 0, 0.0, False))
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait(WARM_UP_SECONDS)

        for _ in range(SYNTHESIS_WORKERS):
            self._submit_synthesis(warm_up)

    def _finish(self, generation: int, message: str) -> None:
        """End reading after a failure and tell the user why.

        Args:
            generation (int): The reading that encountered the failure.
            message (str): What went wrong.
        """
        with self.condition:
            if generation != self.generation:
                return
            self.active = False
        self._reading_ended.emit(generation)
        self.failed.emit(message)

    def _reset_reading_position(self) -> None:
        """Discard pending positions and clear the current word on the Qt thread."""
        self.playback_timer.stop()
        self.playback_events.clear()
        self.reading_position = None

    def _on_reading_ended(self, generation: int) -> None:
        """Clear the display after the output for the current reading has closed."""
        if generation == self.generation:
            self._reset_reading_position()
            self.reading_open = False
            self.reading_changed.emit(self.reply, "finish", None)

    def _on_playback_events(
        self, generation: int, revision: int, events: tuple[_PlaybackEvent, ...]
    ) -> None:
        """Accept positions from the current unpaused playback only."""
        if (
            not self.reading_open
            or generation != self.generation
            or revision != self.playback_revision
            or self.paused
        ):
            return
        self.playback_events.extend(events)
        self._advance_reading_position()

    def _advance_reading_position(self) -> None:
        """Display the newest position that has reached the output device."""
        now = time.monotonic()
        position = self.reading_position
        while self.playback_events and self.playback_events[0][0] <= now:
            _, position = self.playback_events.popleft()
        if position != self.reading_position:
            self.reading_position = position
            self.reading_changed.emit(self.reply, "position", position)
        if self.playback_events:
            self.playback_timer.start(
                max(1, ceil((self.playback_events[0][0] - now) * 1000))
            )


class _Clip(NamedTuple):
    """Store device-rate audio and the reading it belongs to."""

    samples: NDArray[np.float32]
    cues: tuple[AudioCue, ...]
    generation: int


class _Output:
    """Play queued clips on the chosen WASAPI output, with silence while paused or waiting."""

    def __init__(self, speaker: Speaker) -> None:
        self.speaker = speaker
        self.claimed = True  # False while held, stopped, between reads
        self.lock = threading.Lock()
        self.clips: deque[_Clip] = deque()
        self.position = 0  # Samples of the first clip already played
        self.cue_index = 0
        self.cue_revision = -1
        self.latency = 0.0
        with PORTAUDIO_LOCK:
            self.audio = pyaudiowpatch.PyAudio()
            try:
                name = speaker.settings_store.audio_device("speaker")
                self.key = _device_key(name)
                device = _output_device(self.audio, name)
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
                    start=False,
                )
                self.latency = float(
                    getattr(self.stream, "get_output_latency", lambda: 0.0)()
                )
            except Exception as error:
                self.audio.terminate()
                raise OSError(DEVICE_FAILED) from error

    def push(
        self,
        samples: NDArray[np.float32],
        pause: float,
        cues: tuple[AudioCue, ...] = (),
        generation: int = 0,
    ) -> None:
        """Queue one clip followed by its pause.

        Args:
            samples (NDArray[np.float32]): Mono samples at Kokoro's rate.
            pause (float): Seconds of silence after the clip.
            cues (tuple[AudioCue, ...]): Positions indexed by Kokoro's sample rate.
            generation (int): The reading that produced this clip.
        """
        length = round(len(samples) * self.rate / VOICE_RATE)
        resampled = np.interp(
            np.arange(length) * (VOICE_RATE / self.rate),
            np.arange(len(samples)),
            samples,
        ).astype(np.float32)
        audio = np.concatenate(
            [resampled, np.zeros(round(pause * self.rate), np.float32)]
        )
        device_cues = tuple(
            (min(len(audio) - 1, round(sample * self.rate / VOICE_RATE)), position)
            for sample, position in cues
        )
        with self.lock:
            self.clips.append(_Clip(audio, device_cues, generation))

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
            self.cue_index = 0
            self.cue_revision = -1

    def close(self) -> None:
        """Stop the stream and release the device."""
        with PORTAUDIO_LOCK:
            try:
                self.stream.stop_stream()
                self.stream.close()
            finally:
                self.audio.terminate()

    def _fill(
        self, _data: bytes | None, frames: int, time_info: Any, _status: int
    ) -> tuple[bytes, int]:
        """Hand Windows the next frames, or silence while paused or waiting for text.

        Args:
            frames (int): The frames Windows asks for.
            time_info (Any): The clocks PortAudio supplies for this callback.

        Returns:
            tuple[bytes, int]: Interleaved float frames and the continue flag.
        """
        mono = np.zeros(frames, np.float32)
        events: list[AudioCue] = []
        with self.lock:
            generation = self.speaker.generation
            revision = self.speaker.playback_revision
            if not self.speaker.paused:
                filled = 0
                while filled < frames and self.clips:
                    clip = self.clips[0]
                    take = min(frames - filled, len(clip.samples) - self.position)
                    end = self.position + take
                    mono[filled : filled + take] = clip.samples[self.position : end]
                    if clip.generation == generation:
                        self._collect_cues(clip, filled, end, revision, events)
                    filled += take
                    self.position = end
                    if self.position >= len(clip.samples):
                        self.clips.popleft()
                        self.position = 0
                        self.cue_index = 0
        if events:
            start = self._output_time(time_info)
            self.speaker._playback_events.emit(  # pylint: disable=protected-access
                generation,
                revision,
                tuple(
                    (start + sample / self.rate, position)
                    for sample, position in events
                ),
            )
        mono *= self.speaker.settings_store.speech_volume() / 100
        frames_out = np.repeat(mono[:, None], self.channels, axis=1)
        return frames_out.tobytes(), int(pyaudiowpatch.paContinue)

    def _collect_cues(
        self, clip: _Clip, filled: int, end: int, revision: int, events: list[AudioCue]
    ) -> None:
        """Collect word changes within the portion of a clip sent to the device."""
        if revision != self.cue_revision:
            self.cue_revision = revision
            if self.cue_index:
                events.append((filled, clip.cues[self.cue_index - 1][1]))
        while self.cue_index < len(clip.cues):
            sample, position = clip.cues[self.cue_index]
            if sample >= end:
                break
            events.append((filled + sample - self.position, position))
            self.cue_index += 1

    def _output_time(self, time_info: Any) -> float:
        """Express the buffer's first audible frame on Python's monotonic clock."""
        latency = self.latency
        if isinstance(time_info, dict):
            current = float(time_info.get("current_time", 0.0))
            output = float(time_info.get("output_buffer_dac_time", 0.0))
            if output > 0.0 and isfinite(output - current):
                latency = max(0.0, output - current)
        return time.monotonic() + latency


def _opening_pieces(unit: SpeechUnit) -> list[SpeechUnit]:
    """Cut the first sentence read into short pieces, preferring to cut after a clause.

    Args:
        unit (SpeechUnit): The sentence.

    Returns:
        list[SpeechUnit]: The pieces in order, all highlighting within the whole sentence.
    """
    cuts = [0]
    for limit in OPENING_LIMITS:
        rest = unit.text[cuts[-1] :]
        if len(rest) <= limit + MIN_PIECE_CHARS:
            break
        window = rest[: limit + 1]
        clauses = [
            match.end()
            for match in CLAUSE_END_RE.finditer(window)
            if match.end() >= MIN_PIECE_CHARS
        ]
        if clauses:
            # The opening takes the earliest clause to start sooner; the next takes the latest
            cut = clauses[0] if len(cuts) == 1 else clauses[-1]
        else:
            cut = window.rfind(" ")
        if cut < MIN_PIECE_CHARS:
            break
        cuts.append(cuts[-1] + cut)
    if len(cuts) == 1:
        return [unit]
    # Each word belongs to the piece that holds its place in the sentence
    placed = []
    position = 0
    for word in unit.words:
        position = unit.text.index(word.text, position)
        placed.append((position, word))
        position += len(word.text)
    ends = [*cuts[1:], len(unit.text)]
    return [
        unit._replace(
            text=unit.text[begin:end].strip(),
            pause=unit.pause if end == len(unit.text) else 0.0,
            words=tuple(word for place, word in placed if begin <= place < end),
        )
        for begin, end in zip(cuts, ends, strict=True)
    ]


def _device_key(name: str) -> tuple[str, int, str]:
    """Describe the outputs Windows offers now, which PortAudio cannot see while a host is open.

    Args:
        name (str): The saved speech output, or an empty string for the Windows default.

    Returns:
        tuple[str, int, str]: The saved name, the output count and the default output's identity.
    """
    send = _winmm.waveOutMessage
    default, flags, size = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_ulong()
    send(
        WAVE_MAPPER,
        DRVM_MAPPER_PREFERRED_GET,
        ctypes.byref(default),
        ctypes.byref(flags),
    )
    send(default.value, DRV_QUERYDEVICEINTERFACESIZE, ctypes.byref(size), None)
    identity = ctypes.create_unicode_buffer(size.value // 2 + 1)
    send(default.value, DRV_QUERYDEVICEINTERFACE, identity, size.value)
    return name, int(_winmm.waveOutGetNumDevs()), identity.value


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
