import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import numpy as np
import pyaudiowpatch  # type: ignore[import-untyped]
from numpy.typing import NDArray
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from core.settings import WHISPER_MODELS, SettingsStore

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
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
MISSING_MODEL_STATUS = "No transcription model yet. Download one in Settings."
MISSING_MODEL_MS = 8000  # How long the missing-model notice stays up
SOURCE_QUIET_SECONDS = 5.0

# Read when huggingface_hub is first imported; silences its cache and token notices
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")


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
    download_finished = pyqtSignal(str, str)  # model, error or an empty string
    source_status = pyqtSignal(str, str, object)
    session_failed = pyqtSignal(str, bool, object)

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
        self.recording_model = ""  # Model the current or last recording uses
        self.first_seq = 1  # First seq of the current or last recording
        self.stop_event = threading.Event()
        self.model: Any = None
        self.model_size = ""
        self.model_lock = threading.Lock()
        self.downloading = ""  # Model being downloaded, or an empty string
        self.missing_model = False  # Showing the missing-model notice
        self.source_health: dict[str, str] = {}
        self.notice_timer = QTimer(self)
        self.notice_timer.setSingleShot(True)
        self.notice_timer.setInterval(MISSING_MODEL_MS)
        self.notice_timer.timeout.connect(self._on_notice_timeout)
        self.transcribed.connect(self._on_transcribed)
        self.failed.connect(self._on_failed)
        self.download_finished.connect(self._on_download_finished)
        self.source_status.connect(self._on_source_status)
        self.session_failed.connect(self._on_session_failed)

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
        model = self.active_model()
        if not model:
            self.missing_model = True
            self.status_changed.emit(
                f"The {self.downloading} model is still downloading. Start recording once it finishes."
                if self.downloading
                else MISSING_MODEL_STATUS
            )
            self.notice_timer.start()
            return
        self.notice_timer.stop()
        self.missing_model = False
        self.recording = True
        self.recording_model = model
        self.first_seq = self.next_seq
        self.stop_event = threading.Event()
        self.source_health.clear()
        utterances: queue.Queue[tuple[str, NDArray[np.float32]] | None] = queue.Queue()
        threading.Thread(
            target=self._transcribe_loop,
            args=(utterances, model, self.stop_event),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._capture_sources,
            args=(tuple(SOURCE_LABELS), self.stop_event, utterances),
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

    def downloaded_models(self) -> list[str]:
        """List the transcription models whose files are fully downloaded.

        Returns:
            list[str]: The downloaded faster-whisper model names, smallest first.
        """
        downloaded = []
        for model in WHISPER_MODELS:
            snapshots = (
                self.cache_dir
                / f"models--Systran--faster-whisper-{model}"
                / "snapshots"
            )
            # Hugging Face places a file in a snapshot only once it finishes downloading
            if snapshots.is_dir() and any(
                all((snapshot / name).is_file() for name in MODEL_FILES)
                for snapshot in snapshots.iterdir()
            ):
                downloaded.append(model)
        return downloaded

    def active_model(self) -> str:
        """Return the model recording will use.

        Returns:
            str: The saved model when downloaded, else the first downloaded model, else an empty string.
        """
        downloaded = self.downloaded_models()
        saved = self.settings_store.whisper_model()
        if saved in downloaded:
            return saved
        return downloaded[0] if downloaded else ""

    def download(self, model: str) -> None:
        """Download a transcription model in the background, one at a time.

        Args:
            model (str): The faster-whisper model name.
        """
        if self.downloading:
            return
        self.downloading = model
        threading.Thread(target=self._download, args=(model,), daemon=True).start()

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

    def _on_notice_timeout(self) -> None:
        """Hide the missing-model notice."""
        self.missing_model = False
        self.status_changed.emit("")

    def _on_download_finished(self, _model: str, error: str) -> None:
        """Allow the next download and drop the missing-model notice once a model exists.

        Args:
            error (str): The download error, or an empty string on success.
        """
        self.downloading = ""
        if not error and self.missing_model:
            self.notice_timer.stop()
            self._on_notice_timeout()

    def _on_failed(self, message: str, fatal: bool) -> None:
        """Show a capture or model error and stop when transcription cannot continue.

        Args:
            message (str): The error to show.
            fatal (bool): Whether recording must stop.
        """
        if fatal:
            self.stop()
        self.status_changed.emit(message)

    def _on_source_status(self, source: str, status: str, session: object) -> None:
        """Report source health on the Qt thread, ignoring stopped or stale sessions.

        Args:
            source (str): The affected audio source.
            status (str): The current problem, or an empty string on recovery.
            session (object): The recording session's stop event.
        """
        if session is not self.stop_event or not self.recording:
            return
        self.source_health[source] = status
        self.status_changed.emit(
            " ".join(value for value in self.source_health.values() if value)
        )

    def _on_session_failed(self, message: str, fatal: bool, session: object) -> None:
        """Keep delayed failures from a previous session from stopping a new recording.

        Args:
            message (str): The capture or recognition error.
            fatal (bool): Whether the affected recording must stop.
            session (object): The session whose operation failed.
        """
        if not self.recording or session is self.stop_event:
            self.failed.emit(message, fatal)

    def _capture(
        self,
        source: str,
        stop_event: threading.Event,
        utterances: queue.Queue[tuple[str, NDArray[np.float32]] | None],
    ) -> None:
        """Capture a single source through the same owner-thread path used by recording.

        Args:
            source (str): The source to acquire.
            stop_event (threading.Event): The session stop flag.
            utterances (queue.Queue): The transcription input queue.
        """
        self._capture_sources((source,), stop_event, utterances)

    def _capture_sources(
        self,
        sources: tuple[str, ...],
        stop_event: threading.Event,
        utterances: queue.Queue[tuple[str, NDArray[np.float32]] | None],
    ) -> None:
        """Own both WASAPI streams and their PortAudio host on one daemon thread.

        Args:
            sources (tuple[str, ...]): The sources in acquisition order.
            stop_event (threading.Event): The recording session's stop flag.
            utterances (queue.Queue): The transcription worker's input queue.
        """
        audio: Any = None
        captures: dict[str, _CaptureState] = {}
        finished: set[str] = set()
        try:
            with PORTAUDIO_LOCK:
                audio = pyaudiowpatch.PyAudio()
            for source in sources:
                try:
                    with PORTAUDIO_LOCK:
                        captures[source] = self._open_source(audio, source)
                except Exception as error:  # noqa: BLE001
                    self._report_source_failure(source, error, stop_event)
                    utterances.put(None)
                    finished.add(source)
            while captures and not stop_event.is_set():
                # Wait only when no source has audio queued, so a silent source never paces the others
                idle = all(capture.chunks.empty() for capture in captures.values())
                for index, (source, capture) in enumerate(tuple(captures.items())):
                    try:
                        utterance = self._poll_source(
                            capture,
                            stop_event,
                            CHUNK_SECONDS if idle and not index else 0.0,
                        )
                        if utterance is not None:
                            utterances.put((source, utterance))
                    except Exception as error:  # noqa: BLE001
                        self._report_source_failure(source, error, stop_event)
                        finished.add(source)
                        del captures[source]
                        try:
                            self._close_source(capture, utterances)
                        except Exception as close_error:  # noqa: BLE001
                            self._report_source_failure(source, close_error, stop_event)
        except Exception as error:  # noqa: BLE001
            for source in sources:
                if source not in finished:
                    self._report_source_failure(source, error, stop_event)
        finally:
            for source, capture in captures.items():
                try:
                    self._close_source(capture, utterances)
                except Exception as error:  # noqa: BLE001
                    self._report_source_failure(source, error, stop_event)
                finally:
                    finished.add(source)
            for source in sources:
                if source not in finished:
                    utterances.put(None)
                    self.speaking_changed.emit(source, False)
            if audio is not None:
                with PORTAUDIO_LOCK:
                    audio.terminate()

    def _open_source(self, audio: Any, source: str) -> "_CaptureState":
        """Open a device on the same thread that initialized the PortAudio host.

        Args:
            audio (Any): The session's PyAudio host.
            source (str): The microphone or system-output source.

        Returns:
            _CaptureState: The open stream and its independent segmentation state.
        """
        if source == "them":
            device = audio.get_default_wasapi_loopback()
        else:
            wasapi = audio.get_host_api_info_by_type(pyaudiowpatch.paWASAPI)
            device = audio.get_device_info_by_index(wasapi["defaultInputDevice"])
        capture = _CaptureState(
            source, int(device["maxInputChannels"]), int(device["defaultSampleRate"])
        )

        def on_audio(data: bytes, *_args: Any) -> tuple[None, int]:
            """Queue callback audio without blocking PortAudio.

            Args:
                data (bytes): Interleaved float frames.

            Returns:
                tuple[None, int]: No output audio and the continue flag.
            """
            capture.chunks.put(data)
            return None, int(pyaudiowpatch.paContinue)

        capture.stream = audio.open(
            format=pyaudiowpatch.paFloat32,
            channels=capture.channels,
            rate=capture.rate,
            input=True,
            input_device_index=int(device["index"]),
            frames_per_buffer=int(capture.rate * CHUNK_SECONDS),
            stream_callback=on_audio,
        )
        return capture

    def _poll_source(
        self,
        capture: "_CaptureState",
        stop_event: threading.Event,
        timeout: float = CHUNK_SECONDS,
    ) -> NDArray[np.float32] | None:
        """Poll one source and report a microphone that stopped delivering audio.

        Args:
            capture (_CaptureState): The source's stream, queue, and segmenter.
            stop_event (threading.Event): The session identity for health signals.
            timeout (float): How long to wait for audio when none is queued.

        Returns:
            NDArray[np.float32] | None: A completed utterance, when present.
        """
        if not capture.stream.is_active():
            raise OSError(
                "The audio stream stopped delivering data. Restart recording."
            )
        now = time.monotonic()
        try:
            data = capture.chunks.get(timeout=timeout)
        except queue.Empty:
            # Count the silence that actually elapsed since this source was last polled
            utterance = capture.segmenter.feed(
                np.zeros(0, np.float32), max(timeout, now - capture.last_poll)
            )
        else:
            samples = _to_mono_16k(data, capture.channels, capture.rate)
            if samples.size:
                capture.last_data = now
            utterance = capture.segmenter.feed(samples)
        capture.last_poll = now
        # A working microphone delivers audio even in silence, while system output delivers
        # nothing whenever no sound plays, so only a microphone without data is a definite failure
        status = (
            f"{SOURCE_LABELS[capture.source]}: no audio data for {int(SOURCE_QUIET_SECONDS)} s. Check the microphone."
            if capture.source == "you"
            and now - capture.last_data >= SOURCE_QUIET_SECONDS
            else ""
        )
        if status != capture.health:
            capture.health = status
            self.source_status.emit(capture.source, status, stop_event)
        self._emit_speaking(capture.source, capture.segmenter)
        return utterance

    def _close_source(
        self,
        capture: "_CaptureState",
        utterances: queue.Queue[tuple[str, NDArray[np.float32]] | None],
    ) -> None:
        """Flush and close a source on its owner thread, always sending one completion marker.

        Args:
            capture (_CaptureState): The source to close.
            utterances (queue.Queue): The transcription queue to complete.
        """
        try:
            utterance = capture.segmenter.flush()
            if utterance is not None:
                utterances.put((capture.source, utterance))
            with PORTAUDIO_LOCK:
                try:
                    capture.stream.stop_stream()
                finally:
                    capture.stream.close()
        finally:
            utterances.put(None)
            self.speaking_changed.emit(capture.source, False)

    def _report_source_failure(
        self, source: str, error: Exception, stop_event: threading.Event
    ) -> None:
        """Report a device error without silently abandoning the other source.

        Args:
            source (str): The failed source.
            error (Exception): The device or stream failure.
            stop_event (threading.Event): The session identity for health reporting.
        """
        message = f"{SOURCE_LABELS[source]} audio is unavailable: {error}"
        self.source_status.emit(source, message, stop_event)
        self.session_failed.emit(message, False, stop_event)

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
        stop_event: threading.Event | None = None,
    ) -> None:
        """Transcribe queued utterances in order until both capture threads finish.

        Args:
            utterances (queue.Queue): Utterances from both sources, ending with one None per source.
            model_size (str): The faster-whisper model name.
            stop_event (threading.Event, optional): The session whose recognition health is reported.
        """
        try:
            model = self._load_model(model_size)
        except Exception as exc:  # noqa: BLE001
            self.session_failed.emit(
                f"Could not load the {model_size} transcription model: {exc}",
                True,
                stop_event if stop_event is not None else self.stop_event,
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
                self.session_failed.emit(
                    f"Transcription failed: {exc}",
                    False,
                    stop_event if stop_event is not None else self.stop_event,
                )
                continue
            if text:
                self.transcribed.emit(source, text)

    def _download(self, model: str) -> None:
        """Download a model's files into the cache and report the outcome.

        Args:
            model (str): The faster-whisper model name.
        """
        try:
            from faster_whisper import (  # type: ignore[import-untyped]  # Slow import, only when downloading
                download_model,
            )

            download_model(model, cache_dir=str(self.cache_dir))
        except Exception as exc:  # noqa: BLE001
            self.download_finished.emit(model, str(exc))
            return
        self.download_finished.emit(model, "")

    def _load_model(self, model_size: str) -> Any:
        """Load a downloaded model once from the cache without contacting huggingface.co.

        Args:
            model_size (str): The faster-whisper model name.

        Returns:
            Any: The loaded WhisperModel.
        """
        with self.model_lock:
            if self.model_size != model_size:
                self.status_changed.emit(f"Loading the {model_size} model…")
                from faster_whisper import (  # Slow import, only when recording
                    WhisperModel,
                )

                self.model = WhisperModel(
                    model_size,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(self.cache_dir),
                    local_files_only=True,
                )
                self.model_size = model_size
                self.status_changed.emit("")
            return self.model


class _CaptureState:
    """Keep one source's callback queue and health separate from the other source."""

    def __init__(self, source: str, channels: int, rate: int) -> None:
        self.source = source
        self.channels = channels
        self.rate = rate
        self.stream: Any = None
        self.chunks: queue.Queue[bytes] = queue.Queue()
        self.segmenter = _Segmenter()
        self.last_data = self.last_poll = time.monotonic()
        self.health = ""


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
