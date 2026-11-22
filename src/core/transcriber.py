import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Final

import numpy as np
import pyaudiowpatch  # type: ignore[import-untyped]
from numpy.typing import NDArray
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from core.settings import WHISPER_MODELS, SettingsStore

SAMPLE_RATE: Final = 16000
CHUNK_SECONDS = 0.1
SPEECH_RMS = 0.01  # Energy gate used only when the Silero VAD is not downloaded
SPEECH_START = 0.5  # Speech probability that starts an utterance
# A lower probability keeps an utterance going, so soft word endings stay inside it
SPEECH_CONTINUE = 0.35
# (utterance length below which, pause that ends it): the longer someone talks, the shorter the pause needed
END_PAUSES: tuple[tuple[float, float], ...] = (
    (3.0, 0.5),
    (6.0, 0.3),
    (float("inf"), 0.1),
)
MAX_UTTERANCE_SECONDS = 10.0  # Split unbroken speech so captions keep up
SPLIT_SEARCH_CHUNKS = (
    15  # A forced split happens at the least speech-like chunk of the last 1.5 s
)
MIN_UTTERANCE_SECONDS = 0.3  # Ignore clicks and pops
PRE_ROLL_CHUNKS = 3  # Keep the quiet start of the first word
VAD_FRAME = 512  # Silero VAD frame at 16 kHz
VAD_CONTEXT = 64  # Samples of the previous frame Silero VAD expects before each frame
FILTER_TAPS_PER_RATIO = 20  # Low-pass length per multiple of 16 kHz; longer filters cut aliasing more sharply
SOURCE_LABELS: dict[str, str] = {"you": "You", "them": "Them"}
ECHO_SIMILARITY = 0.75  # shortcut: text match only catches whole-utterance echoes, use AEC if partial echoes slip through
ECHO_WINDOW = 3  # Recent segments checked for speaker echo
PORTAUDIO_LOCK = threading.Lock()  # PortAudio setup and teardown are not thread-safe
MODEL_FILES = (
    "config.json",
    "vocab.txt",
    "encoder-model.int8.onnx",
    "decoder_joint-model.int8.onnx",
)
VAD_REPO = "istupakov/silero-vad-onnx"
VAD_DIR = "silero-vad"
VAD_FILE = "silero_vad.onnx"
# More threads spill onto efficiency cores and decode about 3x slower on hybrid CPUs
ONNX_THREADS = min(4, os.cpu_count() or 1)
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
    """Record microphone and system audio and transcribe speech locally with Parakeet.

    Capture and transcription run on daemon threads. Transcript state lives on the Qt
    thread and only changes through queued signals, so it needs no locks.
    """

    changed = pyqtSignal()  # Segments or the sent marker changed
    recording_changed = pyqtSignal(bool)
    speaking_changed = pyqtSignal(str, bool)  # source, speaking
    status_changed = pyqtSignal(str)  # Loading or error text, or an empty string
    transcribed = pyqtSignal(str, str, object)  # source, text, recording stop event
    failed = pyqtSignal(str, bool)  # message, whether recording must stop
    download_finished = pyqtSignal(str, str)  # model, error or an empty string
    audio_devices_loaded = pyqtSignal(
        object, str
    )  # device names by source, error or an empty string
    source_status = pyqtSignal(str, str, object)
    session_failed = pyqtSignal(str, bool, object)

    def __init__(self, settings_store: SettingsStore) -> None:
        super().__init__()
        self.settings_store = settings_store
        self.cache_dir = (
            Path(__file__).resolve().parent.parent / "data" / "cache" / "transcription"
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
        self.vad_session: Any = (
            None  # Shared Silero VAD; each source keeps its own state
        )
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
        devices = {
            source: self.settings_store.audio_device(source) for source in SOURCE_LABELS
        }
        threading.Thread(
            target=self._transcribe_loop,
            args=(utterances, model, self.stop_event),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._capture_sources,
            args=(tuple(SOURCE_LABELS), self.stop_event, utterances, devices),
            daemon=True,
        ).start()
        self.status_changed.emit("")
        self.recording_changed.emit(True)

    def stop(self) -> None:
        """Stop capturing and discard any transcript results still pending."""
        if not self.recording:
            return
        self.recording = False
        self.stop_event.set()
        for source in SOURCE_LABELS:
            self.speaking_changed.emit(source, False)
        self.recording_changed.emit(False)

    def refresh_audio_devices(self) -> None:
        """List Windows audio devices on a worker without starting any streams."""
        threading.Thread(target=self._scan_audio_devices, daemon=True).start()

    def downloaded_models(self) -> list[str]:
        """List the transcription models whose files are fully downloaded.

        Returns:
            list[str]: The downloaded transcription model names.
        """
        # Hugging Face moves a file into place only once it finishes downloading
        if not (self.cache_dir / VAD_DIR / VAD_FILE).is_file():
            return []
        return [
            model
            for model in WHISPER_MODELS
            if all((self.cache_dir / model / name).is_file() for name in MODEL_FILES)
        ]

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
            model (str): The transcription model name.
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

    def _on_transcribed(self, source: str, text: str, session: object) -> None:
        """Append a finished utterance for the active recording on the Qt thread.

        Args:
            source (str): The audio source, "you" or "them".
            text (str): The transcribed text.
            session (object): The stop event identifying the recording.
        """
        if not self.recording or session is not self.stop_event:
            return
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

    def _scan_audio_devices(self) -> None:
        """Enumerate the available devices and deliver their names to the Qt thread."""
        try:
            with PORTAUDIO_LOCK, pyaudiowpatch.PyAudio() as audio:
                devices = _wasapi_devices(audio)
        except (OSError, ValueError):
            self.audio_devices_loaded.emit(
                {}, "Could not refresh audio devices. Try again."
            )
            return
        self.audio_devices_loaded.emit(
            {
                source: [str(device["name"]) for device in matches]
                for source, matches in devices.items()
            },
            "",
        )

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
        devices: dict[str, str] | None = None,
    ) -> None:
        """Own both WASAPI streams and their PortAudio host on one daemon thread.

        Args:
            sources (tuple[str, ...]): The sources in acquisition order.
            stop_event (threading.Event): The recording session's stop flag.
            utterances (queue.Queue): The transcription worker's input queue.
            devices (dict[str, str], optional): The recording's selected device names by source.
        """
        devices = devices or {}
        audio: Any = None
        captures: dict[str, _CaptureState] = {}
        finished: set[str] = set()
        try:
            with PORTAUDIO_LOCK:
                audio = pyaudiowpatch.PyAudio()
            for source in sources:
                try:
                    with PORTAUDIO_LOCK:
                        captures[source] = self._open_source(
                            audio, source, devices.get(source, "")
                        )
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

    def _open_source(self, audio: Any, source: str, name: str) -> "_CaptureState":
        """Open a device on the same thread that initialized the PortAudio host.

        Args:
            audio (Any): The session's PyAudio host.
            source (str): The microphone or system-output source.
            name (str): The selected WASAPI device name, or an empty string for the default.

        Returns:
            _CaptureState: The open stream and its independent segmentation state.
        """
        if name:
            matches = [
                device
                for device in _wasapi_devices(audio)[source]
                if device["name"] == name
            ]
            if len(matches) != 1:
                raise OSError(
                    "Reconnect the selected device or choose an available one in Settings → Audio."
                )
            device = matches[0]
        elif source == "them":
            device = audio.get_default_wasapi_loopback()
        else:
            wasapi = audio.get_host_api_info_by_type(pyaudiowpatch.paWASAPI)
            device = audio.get_device_info_by_index(wasapi["defaultInputDevice"])
        capture = _CaptureState(
            source,
            int(device["maxInputChannels"]),
            int(device["defaultSampleRate"]),
            self._vad(),
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
            samples = _to_mono_16k(data, capture.channels, capture.resampler)
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
        stop_event: threading.Event,
    ) -> None:
        """Transcribe queued utterances until recording stops or both sources finish.

        Args:
            utterances (queue.Queue): Utterances from both sources, ending with one None per source.
            model_size (str): The transcription model name.
            stop_event (threading.Event): The session stop flag and identity for queued results.
        """
        if stop_event.is_set():
            return
        try:
            model = self._load_model(model_size)
        except Exception as exc:  # noqa: BLE001
            if not stop_event.is_set():
                self.session_failed.emit(
                    f"Could not load the {model_size} transcription model: {exc}",
                    True,
                    stop_event,
                )
            return
        finished = 0
        while finished < len(SOURCE_LABELS) and not stop_event.is_set():
            item = utterances.get()
            if stop_event.is_set():
                return
            if item is None:
                finished += 1
                continue
            source, audio = item
            try:
                text = str(model.recognize(audio, sample_rate=SAMPLE_RATE)).strip()
            except Exception as exc:  # noqa: BLE001
                if stop_event.is_set():
                    return
                self.session_failed.emit(
                    f"Transcription failed: {exc}",
                    False,
                    stop_event,
                )
                continue
            if text and not stop_event.is_set():
                self.transcribed.emit(source, text, stop_event)

    def _download(self, model: str) -> None:
        """Download a model's files into the cache and report the outcome.

        Args:
            model (str): The transcription model name.
        """
        try:
            from huggingface_hub import (  # Slow import, only when downloading
                snapshot_download,
            )

            snapshot_download(
                VAD_REPO,
                local_dir=str(self.cache_dir / VAD_DIR),
                allow_patterns=[VAD_FILE],
            )
            # Only the int8 files: the full-precision weights add 2.4 GB for no audible gain
            snapshot_download(
                f"istupakov/{model}-onnx",
                local_dir=str(self.cache_dir / model),
                allow_patterns=list(MODEL_FILES),
            )
        except Exception as exc:  # noqa: BLE001
            self.download_finished.emit(model, str(exc))
            return
        self.download_finished.emit(model, "")

    def _load_model(self, model_size: str) -> Any:
        """Load a downloaded model once from the cache without contacting huggingface.co.

        Args:
            model_size (str): The transcription model name.

        Returns:
            Any: The loaded onnx-asr model.
        """
        with self.model_lock:
            if self.model_size != model_size:
                self.status_changed.emit(f"Loading the {model_size} model…")
                path = self.cache_dir / model_size
                # onnx-asr loads an existing directory offline but downloads into a missing one
                if not path.is_dir():
                    raise FileNotFoundError(
                        f"{path} is missing. Download it in Settings."
                    )
                import onnx_asr  # Slow import, only when recording
                import onnxruntime  # type: ignore[import-untyped]

                options = onnxruntime.SessionOptions()
                options.intra_op_num_threads = ONNX_THREADS
                model = onnx_asr.load_model(
                    f"nemo-{model_size}",
                    path,
                    quantization="int8",
                    sess_options=options,
                )
                # The first run sets up the graph and takes seconds; spend that before speech arrives
                model.recognize(
                    np.zeros(SAMPLE_RATE, np.float32), sample_rate=SAMPLE_RATE
                )
                self.model = model
                self.model_size = model_size
                self.status_changed.emit("")
            return self.model

    def _vad(self) -> "_SileroVad | None":
        """Create a source's speech detector on the capture thread.

        Returns:
            _SileroVad | None: A Silero VAD with its own state, or None to use the energy gate
                when the VAD is not downloaded.
        """
        path = self.cache_dir / VAD_DIR / VAD_FILE
        if self.vad_session is None and path.is_file():
            import onnxruntime

            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = (
                1  # Tiny model; more threads only add overhead
            )
            options.inter_op_num_threads = 1
            self.vad_session = onnxruntime.InferenceSession(
                str(path), options, providers=["CPUExecutionProvider"]
            )
        return None if self.vad_session is None else _SileroVad(self.vad_session)


class _CaptureState:
    """Keep one source's callback queue and health separate from the other source."""

    def __init__(
        self, source: str, channels: int, rate: int, vad: "_SileroVad | None" = None
    ) -> None:
        self.source = source
        self.channels = channels
        self.rate = rate
        self.stream: Any = None
        self.chunks: queue.Queue[bytes] = queue.Queue()
        self.resampler = _Resampler(rate)
        self.segmenter = _Segmenter(vad)
        self.last_data = self.last_poll = time.monotonic()
        self.health = ""


class _Segmenter:
    """Split a 16 kHz mono stream into utterances at the pauses between speech."""

    def __init__(self, vad: "_SileroVad | None" = None) -> None:
        self.vad = vad
        self.speaking = False
        self.reported = False
        self.parts: list[NDArray[np.float32]] = []
        self.scores: list[float] = []  # Speech probability of each part
        self.pre_roll: deque[tuple[NDArray[np.float32], float]] = deque(
            maxlen=PRE_ROLL_CHUNKS
        )
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
        score = self._score(samples)
        if not self.speaking:
            if score < SPEECH_START:
                if samples.size:
                    self.pre_roll.append((samples, score))
                return None
            self.speaking = True
            self.parts = [part for part, _score in self.pre_roll]
            self.scores = [part_score for _part, part_score in self.pre_roll]
            self.pre_roll.clear()
            self.quiet = 0.0
            self.length = 0.0
        if samples.size:
            self.parts.append(samples)
            self.scores.append(score)
        self.length += duration
        self.quiet = 0.0 if score >= SPEECH_CONTINUE else self.quiet + duration
        pause = next(pause for limit, pause in END_PAUSES if self.length < limit)
        if self.quiet >= pause:
            return self.flush()
        if self.length >= MAX_UTTERANCE_SECONDS:
            return self._split()
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
        self.scores = []
        return audio if audio.size >= SAMPLE_RATE * MIN_UTTERANCE_SECONDS else None

    def _score(self, samples: NDArray[np.float32]) -> float:
        """Rate how likely new audio is speech.

        Args:
            samples (NDArray[np.float32]): New samples, or an empty array when no audio arrived.

        Returns:
            float: The speech probability, from 0 to 1.
        """
        if not samples.size:
            return 0.0
        if self.vad is not None:
            return self.vad.score(samples)
        return 1.0 if float(np.sqrt(np.mean(samples**2))) >= SPEECH_RMS else 0.0

    def _split(self) -> NDArray[np.float32] | None:
        """End unbroken speech at its least speech-like recent chunk and keep the rest going.

        Returns:
            NDArray[np.float32] | None: The audio up to the split, or None when it is too short.
        """
        window = min(len(self.scores), SPLIT_SEARCH_CHUNKS)
        cut = len(self.scores) - window + int(np.argmin(self.scores[-window:])) + 1
        audio = np.concatenate(self.parts[:cut])
        self.parts = self.parts[cut:]
        self.scores = self.scores[cut:]
        self.length = sum(part.size for part in self.parts) / SAMPLE_RATE
        return audio if audio.size >= SAMPLE_RATE * MIN_UTTERANCE_SECONDS else None


class _SileroVad:
    """Score speech with Silero VAD, carrying its recurrent state across chunks."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.state = np.zeros((2, 1, 128), np.float32)
        self.context: NDArray[np.float32] = np.zeros(VAD_CONTEXT, np.float32)
        self.pending: NDArray[np.float32] = np.zeros(0, np.float32)
        self.probability = 0.0

    def score(self, samples: NDArray[np.float32]) -> float:
        """Run every whole frame the new samples complete.

        Args:
            samples (NDArray[np.float32]): New 16 kHz samples.

        Returns:
            float: The highest speech probability among the completed frames, or the last
                probability when no frame completed.
        """
        self.pending = np.concatenate((self.pending, samples))
        frames = self.pending.size // VAD_FRAME
        if frames:
            probabilities = []
            for index in range(frames):
                frame = self.pending[index * VAD_FRAME : (index + 1) * VAD_FRAME]
                output, self.state = self.session.run(
                    ["output", "stateN"],
                    {
                        "input": np.concatenate((self.context, frame))[None],
                        "state": self.state,
                        "sr": np.array(SAMPLE_RATE, np.int64),
                    },
                )
                self.context = frame[-VAD_CONTEXT:]
                probabilities.append(float(output[0, 0]))
            self.pending = self.pending[frames * VAD_FRAME :]
            self.probability = max(probabilities)
        return self.probability


class _Resampler:
    """Downsample one source to 16 kHz without folding high frequencies into the speech band."""

    def __init__(self, rate: int) -> None:
        self.rate = rate
        ratio = rate / SAMPLE_RATE
        count = int(FILTER_TAPS_PER_RATIO * ratio) + 1
        offsets = np.arange(count) - (count - 1) / 2
        cutoff = (
            0.45 / ratio
        )  # Just under the 8 kHz limit, as a fraction of the device rate
        taps = cutoff * np.sinc(cutoff * offsets) * np.hamming(count)
        self.taps = (taps / taps.sum()).astype(np.float32)
        # The previous chunk's tail, so filtering never clicks at chunk edges
        self.history: NDArray[np.float32] = np.zeros(count - 1, np.float32)

    def resample(self, mono: NDArray[np.float32]) -> NDArray[np.float32]:
        """Low-pass filter and downsample one chunk.

        Args:
            mono (NDArray[np.float32]): Mono samples at the device rate.

        Returns:
            NDArray[np.float32]: The 16 kHz samples.
        """
        if self.rate == SAMPLE_RATE:
            return mono
        filtered: NDArray[np.float32] = mono
        if self.rate > SAMPLE_RATE:  # Slower devices have nothing above 8 kHz to remove
            padded = np.concatenate((self.history, mono))
            self.history = padded[padded.size - self.history.size :]
            filtered = np.convolve(padded, self.taps, mode="valid").astype(np.float32)
        positions = np.arange(0, filtered.size, self.rate / SAMPLE_RATE)
        resampled: NDArray[np.float32] = np.interp(
            positions, np.arange(filtered.size), filtered
        ).astype(np.float32)
        return resampled


def _wasapi_devices(audio: Any) -> dict[str, list[dict[str, Any]]]:
    """Group WASAPI microphones and playback loopbacks by transcript source.

    Args:
        audio (Any): The initialized PyAudio host.

    Returns:
        dict[str, list[dict[str, Any]]]: Devices with capture channels for each source.
    """
    devices: dict[str, list[dict[str, Any]]] = {source: [] for source in SOURCE_LABELS}
    for device in audio.get_device_info_generator_by_host_api(
        host_api_type=pyaudiowpatch.paWASAPI
    ):
        if device["maxInputChannels"] > 0:
            source = "them" if device["isLoopbackDevice"] else "you"
            devices[source].append(device)
    return devices


def _to_mono_16k(
    data: bytes, channels: int, resampler: _Resampler
) -> NDArray[np.float32]:
    """Convert interleaved float frames to 16 kHz mono for speech recognition.

    Args:
        data (bytes): Interleaved 32-bit float frames.
        channels (int): The channel count.
        resampler (_Resampler): The source's resampler, which keeps filter state between chunks.

    Returns:
        NDArray[np.float32]: The mono 16 kHz samples.
    """
    mono: NDArray[np.float32] = (
        np.frombuffer(data, np.float32)
        .reshape(-1, channels)
        .mean(axis=1, dtype=np.float32)
    )
    return resampler.resample(mono)
