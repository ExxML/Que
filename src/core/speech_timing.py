"""Recover word positions from the same Kokoro inference that produces the audio."""

from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, NamedTuple, Protocol, cast

import numpy as np
from numpy.typing import NDArray

from ui.chat.source_text import SourceSpan
from ui.chat.speech_text import SpeechUnit

VOICE_RATE = 24000
_DURATION_TENSOR = (
    "/encoder/Gather_output_0"  # Frame counts used by the pinned Kokoro export
)
_WORD_GAP = 0.15  # Hold the tint across brief gaps between words
_STRESS_MARKS = frozenset("ˈˌːˑ")


class ReadingPosition(NamedTuple):
    """Identify the portion of a reply being heard."""

    unit: SourceSpan
    word: SourceSpan | None


type AudioCue = tuple[int, ReadingPosition | None]


def load_voice(model: Path, voices: Path, threads: int) -> Any:
    """Load the cached Kokoro model, exposing its frame durations if available.

    Args:
        model (Path): The cached ONNX model.
        voices (Path): The cached Kokoro voice styles.
        threads (int): The CPU inference thread count.

    Returns:
        Any: The loaded Kokoro voice.
    """
    import onnxruntime  # type: ignore[import-untyped]
    from kokoro_onnx import Kokoro

    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = threads
    providers = ["CPUExecutionProvider"]
    try:
        session = onnxruntime.InferenceSession(
            _add_duration_output(model), options, providers=providers
        )
        # Kokoro validates the path of the original model. The CPU session already owns
        # its decoded graph, so release the temporary serialization.
        session.disable_fallback()
        session._model_path = str(model)  # pylint: disable=protected-access
        session._model_bytes = None  # pylint: disable=protected-access
    except Exception:  # noqa: BLE001  # Timing support is optional for playback
        session = onnxruntime.InferenceSession(str(model), options, providers=providers)
    return Kokoro.from_session(session, str(voices))


def synthesize(
    voice: Any, unit: SpeechUnit, voice_name: str, speed: float
) -> tuple[NDArray[np.float32], tuple[AudioCue, ...]]:
    """Return the original sentence audio and any positions it can provide.

    Args:
        voice (Any): The loaded Kokoro voice.
        unit (SpeechUnit): The sentence and its source spans.
        voice_name (str): The Kokoro voice style.
        speed (float): The configured speech speed multiplier.

    Returns:
        tuple[NDArray[np.float32], tuple[AudioCue, ...]]: Mono samples and their cues.
    """
    if not unit.words or getattr(voice, "has_timings", False) is not True:
        samples, _rate = voice.create(
            unit.text, voice=voice_name, speed=speed, lang="en-us"
        )
        audio = np.asarray(samples, np.float32).ravel()
        return audio, _unit_cues(unit, len(audio))

    # Use the same batch preparation and trimming as Kokoro.create. Its public
    # create_timed also inserts extra punctuation pauses when it has durations.
    style, _phonemes, batches = voice._prepare(
        unit.text, voice_name, speed, "en-us", False, 0.25, 0.1
    )
    samples, timings = voice._create_batches(batches, style, speed, True)
    audio = np.asarray(samples, np.float32).ravel()
    try:
        lengths = [len(voice.tokenizer.known(batch)) for batch, _pause in batches]
        heard = _phoneme_runs(cast(list[_Phoneme], timings), lengths)
        return audio, _word_cues(unit, heard, voice, len(audio))
    except Exception:  # noqa: BLE001  # A failed alignment must still play the sentence
        return audio, _unit_cues(unit, len(audio))


class _Phoneme(Protocol):
    """Describe a Kokoro phoneme and its bounds within the synthesized clip."""

    phoneme: str
    start: float
    end: float


class _Run(NamedTuple):
    """Identify one timed pronunciation run between phoneme separators."""

    key: str
    start: float
    end: float


def _add_duration_output(model: Path) -> bytes:
    """Expose the frame counts from the cached graph without changing its waveform."""
    import onnx

    graph = onnx.load(str(model))
    if any(output.name == "duration" for output in graph.graph.output):
        return bytes(graph.SerializeToString())
    if not any(_DURATION_TENSOR in node.output for node in graph.graph.node):
        raise ValueError("This voice model has no recognized duration tensor.")
    graph.graph.node.append(
        onnx.helper.make_node("Identity", [_DURATION_TENSOR], ["duration"])
    )
    graph.graph.output.append(
        onnx.helper.make_tensor_value_info(
            "duration", onnx.TensorProto.INT64, ["sequence_length"]
        )
    )
    return bytes(graph.SerializeToString())


def _phonetic_key(run: str) -> str:
    """Ignore stress and separator punctuation when matching a pronunciation."""
    return "".join(char for char in run if char.isalpha() and char not in _STRESS_MARKS)


def _phoneme_runs(timings: list[_Phoneme], batch_lengths: list[int]) -> list[_Run]:
    """Gather audible phonemes into ordered runs and keep their original timing."""
    runs = []
    offset = 0
    for length in batch_lengths:
        batch = timings[offset : offset + length]
        offset += length
        word: list[tuple[str, _Phoneme]] = []
        for phoneme in batch:
            if phoneme.phoneme.isspace() and word:
                runs.append(
                    _Run(
                        "".join(key for key, _ in word),
                        word[0][1].start,
                        word[-1][1].end,
                    )
                )
                word.clear()
            if key := _phonetic_key(phoneme.phoneme):
                word.append((key, phoneme))
        if word:
            runs.append(
                _Run("".join(key for key, _ in word), word[0][1].start, word[-1][1].end)
            )
    return runs


def _word_cues(
    unit: SpeechUnit, heard: list[_Run], voice: Any, sample_count: int
) -> tuple[AudioCue, ...]:
    """Align full-sentence pronunciation to the words that provided its source spans."""
    expected: list[tuple[str, SourceSpan]] = []
    pronunciations: dict[str, tuple[str, ...]] = {}
    for word in unit.words:
        keys = pronunciations.get(word.text)
        if keys is None:
            phonemes = str(voice.tokenizer.phonemize(word.text, "en-us"))
            keys = tuple(key for run in phonemes.split() if (key := _phonetic_key(run)))
            pronunciations[word.text] = keys
        expected.extend((key, word.source) for key in keys)
    if not expected or not heard:
        return _unit_cues(unit, sample_count)

    sources: list[SourceSpan | None] = [None] * len(heard)
    matcher = SequenceMatcher(
        None, [key for key, _ in expected], [run.key for run in heard], autojunk=False
    )
    for operation, left, right, start, end in matcher.get_opcodes():
        if operation == "equal" or (
            operation == "replace" and right - left == end - start
        ):
            # Words such as "a" and "the" change sound in context. The order of
            # equally sized runs between matched words identifies them.
            sources[start:end] = [source for _, source in expected[left:right]]
        elif (
            operation == "replace"
            and len({source for _, source in expected[left:right]}) == 1
        ):
            sources[start:end] = [expected[left][1]] * (end - start)

    positions: list[AudioCue] = []
    anchor = (unit.start, unit.end)
    for index, (run, source) in enumerate(zip(heard, sources, strict=True)):
        end_time = run.end
        if index + 1 < len(heard):
            next_run = heard[index + 1]
            if (
                source is not None and source == sources[index + 1]
            ) or next_run.start - run.end < _WORD_GAP:
                end_time = next_run.start
        start_sample = max(0, round(run.start * VOICE_RATE))
        end_sample = min(sample_count, round(end_time * VOICE_RATE))
        if end_sample <= start_sample:
            continue
        position = ReadingPosition(anchor, source)
        if positions and positions[-1][0] == start_sample:
            positions.pop()
        if not positions or positions[-1][1] != position:
            positions.append((start_sample, position))
        positions.append((end_sample, None))
    return tuple(positions) or _unit_cues(unit, sample_count)


def _unit_cues(unit: SpeechUnit, sample_count: int) -> tuple[AudioCue, ...]:
    """Keep a sentence in view if only its audio extent is known."""
    if not sample_count:
        return ()
    return ((0, ReadingPosition((unit.start, unit.end), None)), (sample_count, None))
