"""Phase 3 audio metadata extraction, via ffprobe and ffmpeg subprocesses.

Every function here takes a path and returns values. Nothing touches the
database and nothing writes a file, so each rule can be tested on a real
audio file on its own.

The governing rule: **a field that cannot be determined comes back as None
and is named in `problems`. No value is ever guessed.** A wrong number in
a metadata table is worse than an absent one, because an absent one is
visibly absent.

Why ffprobe and ffmpeg rather than pydub or librosa: these two produce all
four required values plus the noise estimate. pydub would only wrap the
same subprocess calls, and librosa would pull in numpy and numba to
recompute what `astats` already reports in one pass.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: No single ffmpeg call on a submitted clip should take this long. A cap
#: stops a malformed file from hanging a web request forever.
TIMEOUT_SECONDS = 60

#: Quality buckets for the noise estimate, in dB. See the caveat block
#: above `estimate_noise` before trusting these.
CLEAN_SNR_DB = 40.0
FAIR_SNR_DB = 15.0

#: A digitally silent window reports as -inf dB, which cannot be stored or
#: subtracted. 16-bit audio cannot represent anything below about -96 dB,
#: so -120 stands in for "the quietest value the format can express".
SILENCE_FLOOR_DB = -120.0


@dataclass(frozen=True)
class AudioMetadata:
    """Everything extracted from one audio file."""

    byte_size: int

    duration_seconds: float | None = None
    sample_rate_hz: int | None = None
    bitrate_bps: int | None = None
    bitrate_is_derived: bool = False
    bitrate_note: str | None = None

    loudness_lufs: float | None = None
    rms_level_db: float | None = None

    noise_snr_db: float | None = None
    quality_estimate: str | None = None

    #: Human-readable notes about anything that could not be determined.
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def sample_rate_khz(self) -> float | None:
        """The assignment asks for kHz; Hz is what ffprobe actually gives."""
        if self.sample_rate_hz is None:
            return None
        return self.sample_rate_hz / 1000


# --------------------------------------------------------------------------
# Subprocess plumbing
# --------------------------------------------------------------------------


def _run(command: list[str]) -> subprocess.CompletedProcess:
    """Run one ffmpeg-family command and capture its output.

    ffmpeg writes its analysis to stderr, not stdout, so both are captured.
    `errors="replace"` because a corrupt file can make ffmpeg emit bytes
    that are not valid UTF-8, and losing a character beats raising.
    """
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
    )


def _to_float(raw: str) -> float | None:
    """Read a number ffmpeg printed, tolerating `inf`, `-inf` and `nan`."""
    try:
        value = float(raw)
    except ValueError:
        return None
    if value != value:  # NaN is the only value not equal to itself.
        return None
    return value


# --------------------------------------------------------------------------
# Container metadata
# --------------------------------------------------------------------------


def probe_container(path: Path) -> dict | None:
    """Read the header with ffprobe. Returns None if the file is unreadable.

        ffprobe -v error -print_format json -show_format -show_streams \
                -select_streams a:0 <file>

    For a browser MediaRecorder file this returns far less than it looks
    like it should -- see `extract` for what is actually missing.
    """
    result = _run(
        [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-select_streams", "a:0",
            str(path),
        ]
    )
    if result.returncode != 0:
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def duration_by_decoding(path: Path) -> float | None:
    """Get duration by decoding the file, when the header does not carry it.

        ffmpeg -hide_banner -nostats -i <file> -f null -

    The last `time=HH:MM:SS.ss` ffmpeg prints is how much audio it decoded.
    This is slower than reading a header because it walks the whole file,
    but it is the only thing that works on a stream written with no known
    duration, which is exactly what MediaRecorder produces.
    """
    result = _run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-f", "null", "-"]
    )
    stamps = re.findall(r"time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", result.stderr)
    if not stamps:
        return None

    hours, minutes, seconds = stamps[-1]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


# --------------------------------------------------------------------------
# Loudness and levels
# --------------------------------------------------------------------------


def integrated_loudness(path: Path) -> float | None:
    """EBU R128 integrated loudness in LUFS.

        ffmpeg -hide_banner -nostats -i <file> -filter_complex ebur128 -f null -

    Reads the `I:  -21.8 LUFS` line from the summary block. LUFS is
    perceptually weighted, which is what "loudness" properly means; the
    plain dB figure the assignment asks for comes from `overall_levels`.
    """
    result = _run(
        [
            "ffmpeg", "-hide_banner", "-nostats",
            "-i", str(path),
            "-filter_complex", "ebur128",
            "-f", "null", "-",
        ]
    )
    matches = re.findall(r"I:\s+(-?[\d.]+|-?inf)\s+LUFS", result.stderr)
    if not matches:
        return None
    # The summary block prints last, after any per-frame output.
    return _to_float(matches[-1])


def overall_levels(path: Path) -> tuple[float | None, float | None]:
    """Return (RMS level dB, RMS trough dB) from the Overall block.

        ffmpeg -hide_banner -nostats -i <file> -af astats=metadata=1 -f null -

    astats prints one block per channel and then an `Overall` block. Only
    the Overall block is used -- taking the first match would silently
    report channel 1 of a stereo file as if it were the whole recording.

    ffmpeg spells the field `RMS through dB`, not `trough`.
    """
    result = _run(
        [
            "ffmpeg", "-hide_banner", "-nostats",
            "-i", str(path),
            "-af", "astats=metadata=1",
            "-f", "null", "-",
        ]
    )

    _, separator, overall = result.stderr.partition("Overall")
    if not separator:
        return None, None

    level = re.search(r"RMS level dB:\s*(-?[\d.]+|-?inf)", overall)
    trough = re.search(r"RMS through dB:\s*(-?[\d.]+|-?inf)", overall)

    return (
        _to_float(level.group(1)) if level else None,
        _to_float(trough.group(1)) if trough else None,
    )


# --------------------------------------------------------------------------
# Noise estimate
# --------------------------------------------------------------------------
#
# The estimate is `RMS level dB - RMS trough dB`: overall loudness minus the
# quietest window. In real speech the quietest window is a pause, so the gap
# approximates signal-against-room-tone.
#
# Measured on files generated with ffmpeg during design:
#
#     speech-like, quiet room    RMS -24.07   trough -107.57   -> 83.5 dB
#     speech-like, noise added   RMS -29.98   trough  -45.85   -> 15.9 dB
#     continuous tone, clean     RMS -21.08   trough  -23.06   ->  2.0 dB
#
# WHAT THIS IS NOT. It is not true SNR. There is no voice activity detection
# and no separation of speech from background. Specifically:
#
#   * It assumes the recording contains pauses. The third row above is a
#     CLEAN signal scoring 2.0 dB, which these buckets label "noisy".
#     Someone who talks continuously with no gaps is mislabelled. This is a
#     real false positive, not a hypothetical one.
#   * It cannot distinguish hum from speech from music. Background chatter
#     during a pause reads as noise; a steady tone reads as signal.
#   * Opus gates near-silence aggressively, so the same room recorded to WAV
#     and to WebM will not score identically.
#   * The thresholds are eyeballed from a handful of synthetic files, not
#     calibrated against real submissions.
#
# REJECTED ALTERNATIVE. astats has its own `Noise floor dB` field, which is
# not used here. It reported -18 dB for a pure sine wave -- measuring the
# signal itself as noise -- and -inf for digital silence. Neither is usable
# as a stored number.


def estimate_noise(
    rms_level_db: float | None, rms_trough_db: float | None
) -> tuple[float | None, str | None]:
    """Return (estimate in dB, quality bucket). Read the block above first."""
    if rms_level_db is None or rms_trough_db is None:
        return None, None

    # True digital silence reports as -inf, which cannot be subtracted.
    floor = SILENCE_FLOOR_DB if rms_trough_db == float("-inf") else rms_trough_db
    if rms_level_db == float("-inf"):
        # The whole file is silent. There is no signal to judge.
        return None, None

    snr = rms_level_db - floor

    if snr >= CLEAN_SNR_DB:
        return snr, "clean"
    if snr >= FAIR_SNR_DB:
        return snr, "fair"
    return snr, "noisy"


# --------------------------------------------------------------------------
# Bitrate
# --------------------------------------------------------------------------


def resolve_bitrate(
    stream: dict, container: dict, byte_size: int, duration: float | None
) -> tuple[int | None, bool, str | None]:
    """Return (bits per second, whether it was derived, a note explaining it).

    Resolution order, because a browser recording carries almost nothing:

      1. the audio stream's own bit_rate
      2. the container's bit_rate
      3. derived as byte_size * 8 / duration
      4. give up and return None

    Written to a pipe -- which is what MediaRecorder does, since it streams
    with no known final size -- ffprobe reports NO stream bit_rate, NO
    container bit_rate and NO duration at all. Only sample rate and file
    size survive, so step 3 is the usual path for recordings.

    A derived figure is CONTAINER bitrate including WebM overhead, not the
    codec bitrate: a 64 kbit/s Opus recording measures about 81 kbit/s this
    way. That is why it is flagged rather than stored as equivalent.
    """
    stream_bitrate = stream.get("bit_rate")
    if stream_bitrate is not None:
        return int(float(stream_bitrate)), False, None

    container_bitrate = container.get("bit_rate")
    if container_bitrate is not None:
        return (
            int(float(container_bitrate)),
            False,
            "Taken from the container header; the audio stream did not "
            "declare its own bitrate.",
        )

    if duration and duration > 0:
        derived = int(byte_size * 8 / duration)
        return (
            derived,
            True,
            f"Derived as byte_size * 8 / duration = {byte_size} * 8 / "
            f"{duration:.3f}s. Neither the stream nor the container declared "
            "a bitrate. This is container bitrate including overhead, not "
            "the codec bitrate.",
        )

    return None, False, None


# --------------------------------------------------------------------------
# The one function callers use
# --------------------------------------------------------------------------


def extract(path: Path) -> AudioMetadata:
    """Extract everything from one audio file.

    Never raises for a bad file. An unreadable or non-audio file comes back
    with every field None and the reason named in `problems`.
    """
    path = Path(path)
    byte_size = path.stat().st_size if path.exists() else 0
    problems: list[str] = []

    if not path.exists():
        return AudioMetadata(byte_size=0, problems=("File does not exist.",))

    probed = probe_container(path)
    if probed is None or not probed.get("streams"):
        return AudioMetadata(
            byte_size=byte_size,
            problems=("ffprobe found no audio stream; the file is not usable audio.",),
        )

    stream = probed["streams"][0]
    container = probed.get("format", {})

    # ---- sample rate ----
    sample_rate = stream.get("sample_rate")
    sample_rate_hz = int(sample_rate) if sample_rate else None
    if sample_rate_hz is None:
        problems.append("Sample rate absent from the stream header.")

    # ---- duration: header first, then decode ----
    duration = _to_float(container.get("duration", "")) if container.get("duration") else None
    if duration is None:
        duration = duration_by_decoding(path)
        if duration is None:
            problems.append(
                "Duration absent from the header and could not be recovered "
                "by decoding."
            )

    # ---- bitrate ----
    bitrate, derived, note = resolve_bitrate(stream, container, byte_size, duration)
    if bitrate is None:
        problems.append(
            "Bitrate absent from stream and container, and no duration was "
            "available to derive it from."
        )

    # ---- loudness and levels ----
    loudness = integrated_loudness(path)
    if loudness is None:
        problems.append("EBU R128 loudness could not be measured.")

    rms_level, rms_trough = overall_levels(path)
    if rms_level is None:
        problems.append("RMS level could not be measured.")

    snr, quality = estimate_noise(rms_level, rms_trough)

    return AudioMetadata(
        byte_size=byte_size,
        duration_seconds=duration,
        sample_rate_hz=sample_rate_hz,
        bitrate_bps=bitrate,
        bitrate_is_derived=derived,
        bitrate_note=note,
        loudness_lufs=loudness,
        rms_level_db=rms_level,
        noise_snr_db=snr,
        quality_estimate=quality,
        problems=tuple(problems),
    )
