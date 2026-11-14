import os
import queue
import threading
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import numpy as np
import pyaudiowpatch  # type: ignore[import-untyped]
from numpy.typing import NDArray
from PyQt6.QtCore import QObject, pyqtSignal

from core.settings import SettingsStore

SAMPLE_RATE = 16000
CHUNK_SECONDS = 0.1
SPEECH_RMS = 0.01  # shortcut: fixed energy gate, switch to Silero VAD if noisy rooms trigger false speech
SILENCE_SECONDS = 0.7  # Quiet time that ends an utterance
MAX_UTTERANCE_SECONDS = 15.0  # Flush long monologues so captions keep up
MIN_UTTERANCE_SECONDS = 0.3  # Ignore clicks and pops
PRE_ROLL_CHUNKS = 3  # Keep the quiet start of the first word
SOURCE_LABELS: dict[str, str] = {"you": "You", "them": "Them"}
ECHO_SIMILARITY = 0.75  # shortcut: text match only catches whole-utterance echoes, use AEC if partial echoes slip through
ECHO_WINDOW = 3  # Recent segments checked for speaker echo
PORTAUDIO_LOCK = threading.Lock()  # PortAudio setup and teardown are not thread-safe


@dataclass(frozen=True)
class TranscriptSegment:
    """Store one transcribed utterance and its order in the recording."""

    seq: int
    source: str
    text: str


class Transcriber(QObject):
    """Record microphone and system audio and transcribe speech locally with faster-whisper.

    Capture and transcription run on daemon threads. Transcript state lives on the Qt
    thread and only changes through queued signals, so it needs no locks.
    """

    changed = pyqtSignal()  # Segments or the sent marker changed
    recording_changed = pyqtSignal(bool)
    speaking_changed = pyqtSignal(str, bool)  # source, speaking
    status_changed = pyqtSignal(str)  # Loading or error text, or an empty string
    transcribed = pyqtSignal(str, str)  # source, text (worker thread to Qt thread)
    failed = pyqtSignal(str, bool)  # message, whether recording must stop

    def __init__(self, settings_store: SettingsStore) -> None:
        super().__init__()
        self.settings_store = settings_store
        self.cache_dir = (
            Path(__file__).resolve().parent.parent / "data" / "cache" / "whisper"
        )
        self.segments: list[TranscriptSegment] = []
        self.sent_seq = 0  # Segments up to this seq reached the AI in a committed turn
        self.next_seq = 1
        self.recording = False
        self.stop_event = threading.Event()
        self.model: Any = None
        self.model_size = ""
        self.model_lock = threading.Lock()
        self.transcribed.connect(self._on_transcribed)
        self.failed.connect(self._on_failed)

    def toggle(self) -> None:
        """Start recording when stopped, otherwise stop."""
        if self.recording:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        """Start capturing both audio sources and transcribing their speech."""
        if self.recording:
            return
        self.recording = True
        self.stop_event = threading.Event()
        utterances: queue.Queue[tuple[str, NDArray[np.float32]] | None] = queue.Queue()
        threading.Thread(
            target=self._transcribe_loop,
            args=(utterances, self.settings_store.whisper_model()),
            daemon=True,
        ).start()
        for source in SOURCE_LABELS:
            threading.Thread(
                target=self._capture,
                args=(source, self.stop_event, utterances),
                daemon=True,
            ).start()
        self.status_changed.emit("")
        self.recording_changed.emit(True)

    def stop(self) -> None:
        """Stop capturing; speech already captured still finishes transcribing."""
        if not self.recording:
            return
        self.recording = False
        self.stop_event.set()
        for source in SOURCE_LABELS:
            self.speaking_changed.emit(source, False)
        self.recording_changed.emit(False)

    def pending(self) -> tuple[str, int]:
        """Return the transcript not yet sent in a committed turn.

        Returns:
            tuple[str, int]: The labeled transcript lines and the last included seq.
        """
        rows = [segment for segment in self.segments if segment.seq > self.sent_seq]
        text = "\n".join(
            f"[{SOURCE_LABELS[segment.source]}] {segment.text}" for segment in rows
        )
        return text, rows[-1].seq if rows else self.sent_seq

    def mark_sent(self, seq: int) -> None:
        """Mark segments up to a seq as delivered so they are never sent again.

        Args:
            seq (int): The last seq included in the committed turn.
        """
        if seq > self.sent_seq:
            self.sent_seq = seq
            self.changed.emit()

    def clear(self) -> None:
        """Drop the transcript, including unsent lines, while recording continues."""
        self.segments.clear()
        self.sent_seq = self.next_seq - 1
        self.changed.emit()

    def _on_transcribed(self, source: str, text: str) -> None:
        """Append a finished utterance on the Qt thread.

        Args:
            source (str): The audio source, "you" or "them".
            text (str): The transcribed text.
        """
        # Speakers echo into the microphone; keep one line, attributed to the computer audio
        for index in range(
            len(self.segments) - 1, len(self.segments) - 1 - ECHO_WINDOW, -1
        ):
            if index < 0:
                break
            segment = self.segments[index]
            if (
                segment.source != source
                and SequenceMatcher(None, segment.text.lower(), text.lower()).ratio()
                >= ECHO_SIMILARITY
            ):
                if segment.source == "you" and segment.seq > self.sent_seq:
                    self.segments[index] = TranscriptSegment(segment.seq, "them", text)
                    self.changed.emit()
                return
        self.segments.append(TranscriptSegment(self.next_seq, source, text))
        self.next_seq += 1
        self.changed.emit()

    def _on_failed(self, message: str, fatal: bool) -> None:
        """Show a capture or model error and stop when transcription cannot continue.

        Args:
            message (str): The error to show.
            fatal (bool): Whether recording must stop.
        """
        if fatal:
            self.stop()
        self.status_changed.emit(message)

    def _capture(
        self,
        source: str,
        stop_event: threading.Event,
        utterances: queue.Queue[tuple[str, NDArray[np.float32]] | None],
    ) -> None:
        """Read one WASAPI source until stopped and queue its utterances.

        Args:
            source (str): "you" for the default microphone, "them" for system output.
            stop_event (threading.Event): The recording session's stop flag.
            utterances (queue.Queue): The transcription worker's input queue.
        """
        audio: Any = None
        stream: Any = None
        segmenter = _Segmenter()
        chunks: queue.Queue[bytes] = queue.Queue()
        try:

            def on_audio(data: bytes, *_args: Any) -> tuple[None, int]:
                """Hand audio to the capture thread without blocking PortAudio.

                Args:
                    data (bytes): Interleaved 32-bit float frames.

                Returns:
                    tuple[None, int]: No output data and the continue flag.
                """
                chunks.put(data)
                return None, int(pyaudiowpatch.paContinue)

            with PORTAUDIO_LOCK:
                audio = pyaudiowpatch.PyAudio()
                if source == "them":
                    device = audio.get_default_wasapi_loopback()
                else:
                    wasapi = audio.get_host_api_info_by_type(pyaudiowpatch.paWASAPI)
                    device = audio.get_device_info_by_index(
                        wasapi["defaultInputDevice"]
                    )
                channels = int(device["maxInputChannels"])
                rate = int(device["defaultSampleRate"])
                # Callback mode because a silent loopback device delivers no data and would block a read
                stream = audio.open(
                    format=pyaudiowpatch.paFloat32,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=int(device["index"]),
                    frames_per_buffer=int(rate * CHUNK_SECONDS),
                    stream_callback=on_audio,
                )
            while not stop_event.is_set():
                try:
                    data = chunks.get(timeout=CHUNK_SECONDS)
                except queue.Empty:
                    utterance = segmenter.feed(np.zeros(0, np.float32), CHUNK_SECONDS)
                else:
                    utterance = segmenter.feed(_to_mono_16k(data, channels, rate))
                self._emit_speaking(source, segmenter)
                if utterance is not None:
                    utterances.put((source, utterance))
            utterance = segmenter.flush()
            if utterance is not None:
                utterances.put((source, utterance))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(
                f"{SOURCE_LABELS[source]} audio is unavailable: {exc}", False
            )
        finally:
            try:
                with PORTAUDIO_LOCK:
                    if stream is not None:
                        stream.stop_stream()
                        stream.close()
                    if audio is not None:
                        audio.terminate()
            finally:
                utterances.put(None)
                self.speaking_changed.emit(source, False)

    def _emit_speaking(self, source: str, segmenter: "_Segmenter") -> None:
        """Signal when a source starts or stops speaking.

        Args:
            source (str): The audio source.
            segmenter (_Segmenter): The source's segmenter.
        """
        if segmenter.speaking != segmenter.reported:
            segmenter.reported = segmenter.speaking
            self.speaking_changed.emit(source, segmenter.speaking)

    def _transcribe_loop(
        self,
        utterances: queue.Queue[tuple[str, NDArray[np.float32]] | None],
        model_size: str,
    ) -> None:
        """Transcribe queued utterances in order until both capture threads finish.

        Args:
            utterances (queue.Queue): Utterances from both sources, ending with one None per source.
            model_size (str): The faster-whisper model name.
        """
        try:
            model = self._load_model(model_size)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(
                f"Could not load the {model_size} transcription model: {exc}", True
            )
            return
        finished = 0
        while finished < len(SOURCE_LABELS):
            item = utterances.get()
            if item is None:
                finished += 1
                continue
            source, audio = item
            try:
                segments, _info = model.transcribe(
                    audio,
                    language="en",
                    beam_size=1,
                    vad_filter=True,  # Drops non-speech that Whisper would hallucinate words for
                    condition_on_previous_text=False,
                )
                text = " ".join(segment.text.strip() for segment in segments).strip()
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(f"Transcription failed: {exc}", False)
                continue
            if text:
                self.transcribed.emit(source, text)

    def _load_model(self, model_size: str) -> Any:
        """Load a model once from the cache, downloading it only when missing.

        Args:
            model_size (str): The faster-whisper model name.

        Returns:
            Any: The loaded WhisperModel.
        """
        with self.model_lock:
            if self.model_size != model_size:
                self.status_changed.emit(
                    f"Loading the {model_size} model. The first use downloads it…"
                )
                # Read when huggingface_hub is first imported; silences its cache and token notices
                os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
                os.environ.setdefault("HF_HUB_VERBOSITY", "error")
                from faster_whisper import (  # type: ignore[import-untyped]  # Slow import, only when recording
                    WhisperModel,
                )
                from huggingface_hub.errors import LocalEntryNotFoundError

                options: dict[str, Any] = {
                    "device": "cpu",
                    "compute_type": "int8",
                    "download_root": str(self.cache_dir),
                }
                try:
                    # Cached models load without contacting huggingface.co
                    self.model = WhisperModel(
                        model_size, local_files_only=True, **options
                    )
                except LocalEntryNotFoundError:
                    self.model = WhisperModel(model_size, **options)
                self.model_size = model_size
                self.status_changed.emit("")
            return self.model


class _Segmenter:
    """Split a 16 kHz mono stream into utterances with an energy gate."""

    def __init__(self) -> None:
        self.speaking = False
        self.reported = False
        self.parts: list[NDArray[np.float32]] = []
        self.pre_roll: deque[NDArray[np.float32]] = deque(maxlen=PRE_ROLL_CHUNKS)
        self.quiet = 0.0
        self.length = 0.0

    def feed(
        self, samples: NDArray[np.float32], quiet_seconds: float = 0.0
    ) -> NDArray[np.float32] | None:
        """Add audio and return an utterance once its speech ends.

        Args:
            samples (NDArray[np.float32]): New samples, or an empty array when no audio arrived.
            quiet_seconds (float): Silence to count when no audio arrived.

        Returns:
            NDArray[np.float32] | None: The finished utterance, or None.
        """
        duration = samples.size / SAMPLE_RATE if samples.size else quiet_seconds
        loud = bool(samples.size) and float(np.sqrt(np.mean(samples**2))) >= SPEECH_RMS
        if not self.speaking:
            if not loud:
                if samples.size:
                    self.pre_roll.append(samples)
                return None
            self.speaking = True
            self.parts = list(self.pre_roll)
            self.pre_roll.clear()
            self.quiet = 0.0
            self.length = 0.0
        if samples.size:
            self.parts.append(samples)
        self.length += duration
        self.quiet = 0.0 if loud else self.quiet + duration
        if self.quiet >= SILENCE_SECONDS or self.length >= MAX_UTTERANCE_SECONDS:
            return self.flush()
        return None

    def flush(self) -> NDArray[np.float32] | None:
        """End the current utterance.

        Returns:
            NDArray[np.float32] | None: The utterance, or None when it is too short or absent.
        """
        if not self.speaking:
            return None
        self.speaking = False
        audio = np.concatenate(self.parts) if self.parts else np.zeros(0, np.float32)
        self.parts = []
        return audio if audio.size >= SAMPLE_RATE * MIN_UTTERANCE_SECONDS else None


def _to_mono_16k(data: bytes, channels: int, rate: int) -> NDArray[np.float32]:
    """Convert interleaved float frames to 16 kHz mono for Whisper.

    Args:
        data (bytes): Interleaved 32-bit float frames.
        channels (int): The channel count.
        rate (int): The device sample rate.

    Returns:
        NDArray[np.float32]: The mono 16 kHz samples.
    """
    mono: NDArray[np.float32] = (
        np.frombuffer(data, np.float32)
        .reshape(-1, channels)
        .mean(axis=1, dtype=np.float32)
    )
    if rate == SAMPLE_RATE:
        return mono
    # Linear resampling is plenty for speech recognition
    positions = np.arange(0, mono.size, rate / SAMPLE_RATE)
    resampled: NDArray[np.float32] = np.interp(
        positions, np.arange(mono.size), mono
    ).astype(np.float32)
    return resampled
