"""Tests for the Phase 3 audio metadata extraction.

Every audio file here is generated with ffmpeg at test time and then read
back through the real ffprobe and ffmpeg binaries. Nothing is mocked: the
point of this module is that the subprocess parsing works against actual
tool output, and a mock would only assert that the parser agrees with my
guess about what ffmpeg prints.

The browser case is reproduced faithfully. `streamed_webm` is written to a
pipe rather than to a file, which is what MediaRecorder does, and that is
what strips the duration and bitrate out of the header.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from audio_meta import (
    AudioMetadata,
    estimate_noise,
    extract,
    integrated_loudness,
    overall_levels,
    probe_container,
    resolve_bitrate,
)

# ffmpeg's lavfi source needs no input files, so every fixture is
# self-contained and the suite has no binary assets checked in.
SINE = "sine=frequency=440:duration=3:sample_rate={rate}"

# Two bursts of tone with silence between them, which is the shape a spoken
# sentence has: signal, pause, signal, pause.
SPEECH_LIKE = (
    "sine=frequency=300:duration=4:sample_rate=48000",
    "volume=enable='between(t,1,2)+between(t,3,4)':volume=0",
)


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="module")
def wav_file(tmp_path_factory) -> Path:
    """A plain WAV. Its header declares everything, so nothing is derived."""
    path = tmp_path_factory.mktemp("audio") / "upload.wav"
    _ffmpeg(["-f", "lavfi", "-i", SINE.format(rate=44100), "-c:a", "pcm_s16le", str(path)])
    return path


@pytest.fixture(scope="module")
def streamed_webm(tmp_path_factory) -> Path:
    """Opus in WebM written to a pipe -- what MediaRecorder actually produces.

    Written this way the container has no known final size, so ffmpeg omits
    duration and bitrate from the header entirely.
    """
    path = tmp_path_factory.mktemp("audio") / "recording.webm"
    with path.open("wb") as handle:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", SINE.format(rate=48000),
                "-c:a", "libopus", "-b:a", "64k",
                "-f", "webm", "-",
            ],
            check=True,
            stdout=handle,
            stderr=subprocess.PIPE,
        )
    return path


@pytest.fixture(scope="module")
def quiet_speech(tmp_path_factory) -> Path:
    """Tone bursts with true silence between them."""
    path = tmp_path_factory.mktemp("audio") / "quiet.wav"
    _ffmpeg(["-f", "lavfi", "-i", SPEECH_LIKE[0], "-af", SPEECH_LIKE[1],
             "-c:a", "pcm_s16le", str(path)])
    return path


@pytest.fixture(scope="module")
def noisy_speech(tmp_path_factory) -> Path:
    """The same bursts with white noise mixed under them."""
    path = tmp_path_factory.mktemp("audio") / "noisy.wav"
    _ffmpeg([
        "-f", "lavfi", "-i", SPEECH_LIKE[0],
        "-f", "lavfi", "-i", "anoisesrc=duration=4:color=white:amplitude=0.05:sample_rate=48000",
        "-filter_complex", f"[0]{SPEECH_LIKE[1]}[v];[v][1]amix=inputs=2",
        "-c:a", "pcm_s16le", str(path),
    ])
    return path


@pytest.fixture(scope="module")
def stereo_file(tmp_path_factory) -> Path:
    """Two different tones, one per channel, so channel 1 != Overall."""
    path = tmp_path_factory.mktemp("audio") / "stereo.wav"
    _ffmpeg([
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=48000",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=2:sample_rate=48000",
        "-filter_complex", "[0][1]join=inputs=2:channel_layout=stereo",
        "-c:a", "pcm_s16le", str(path),
    ])
    return path


# ==========================================================================
# The four required values, from a file whose header declares them
# ==========================================================================


def test_wav_yields_all_four_required_values(wav_file):
    meta = extract(wav_file)
    assert meta.problems == ()
    assert meta.duration_seconds == pytest.approx(3.0, abs=0.05)
    assert meta.sample_rate_hz == 44100
    assert meta.bitrate_bps is not None
    assert meta.loudness_lufs is not None
    assert meta.rms_level_db is not None


def test_sample_rate_is_reported_in_khz_for_display(wav_file):
    """The assignment asks for kHz; Hz is what ffprobe gives."""
    meta = extract(wav_file)
    assert meta.sample_rate_khz == 44.1


def test_a_declared_bitrate_is_not_marked_derived(wav_file):
    meta = extract(wav_file)
    assert meta.bitrate_is_derived is False
    assert meta.bitrate_note is None


# ==========================================================================
# The browser case
# ==========================================================================


def test_streamed_webm_header_carries_neither_duration_nor_bitrate(streamed_webm):
    """The premise of the fallback logic. If this fails, the rest is moot."""
    probed = probe_container(streamed_webm)
    stream = probed["streams"][0]
    container = probed["format"]

    assert "bit_rate" not in stream
    assert "duration" not in stream
    assert "bit_rate" not in container
    assert "duration" not in container
    # Only these two survive.
    assert stream["sample_rate"] == "48000"
    assert int(container["size"]) > 0


def test_duration_is_recovered_by_decoding_when_the_header_lacks_it(streamed_webm):
    meta = extract(streamed_webm)
    assert meta.duration_seconds == pytest.approx(3.0, abs=0.15)


def test_bitrate_is_derived_and_says_so_for_a_recording(streamed_webm):
    meta = extract(streamed_webm)
    assert meta.bitrate_bps is not None
    assert meta.bitrate_is_derived is True
    assert "byte_size * 8 / duration" in meta.bitrate_note
    assert "not the codec bitrate" in meta.bitrate_note


def test_a_recording_still_produces_every_required_value(streamed_webm):
    """Nothing is missing just because the container was uninformative."""
    meta = extract(streamed_webm)
    assert meta.problems == ()
    assert meta.duration_seconds is not None
    assert meta.sample_rate_hz == 48000
    assert meta.bitrate_bps is not None
    assert meta.loudness_lufs is not None


def test_derived_bitrate_exceeds_the_codec_bitrate_because_of_overhead(streamed_webm):
    """64 kbit/s of Opus measures higher once WebM framing is counted.

    This is the reason the derived figure is flagged rather than presented
    as equivalent to a codec bitrate.
    """
    meta = extract(streamed_webm)
    assert meta.bitrate_bps > 64_000


# ==========================================================================
# Loudness and levels
# ==========================================================================


def test_loudness_is_negative_lufs_in_a_plausible_range(wav_file):
    loudness = integrated_loudness(wav_file)
    assert loudness is not None
    assert -70.0 < loudness < 0.0


def test_overall_levels_reads_the_overall_block_not_channel_one(stereo_file):
    """astats prints per-channel blocks before Overall.

    Taking the first match would report channel 1 of a stereo file as if it
    were the whole recording.
    """
    level, trough = overall_levels(stereo_file)
    assert level is not None and trough is not None
    assert trough <= level


# ==========================================================================
# The noise estimate
# ==========================================================================


def test_a_noisy_recording_scores_lower_than_a_quiet_one(quiet_speech, noisy_speech):
    """The estimate must at minimum order these two correctly."""
    quiet = extract(quiet_speech)
    noisy = extract(noisy_speech)
    assert quiet.noise_snr_db > noisy.noise_snr_db


def test_a_quiet_recording_with_pauses_is_called_clean(quiet_speech):
    meta = extract(quiet_speech)
    assert meta.quality_estimate == "clean"


def test_buckets_map_to_the_documented_thresholds():
    assert estimate_noise(-20.0, -70.0) == (50.0, "clean")
    assert estimate_noise(-20.0, -45.0) == (25.0, "fair")
    assert estimate_noise(-20.0, -30.0) == (10.0, "noisy")


def test_digital_silence_is_floored_rather_than_producing_infinity():
    """A -inf trough cannot be subtracted, so it is clamped to -120 dB."""
    snr, quality = estimate_noise(-24.0, float("-inf"))
    assert snr == pytest.approx(96.0)
    assert quality == "clean"


def test_a_wholly_silent_file_gets_no_estimate():
    """No signal means there is nothing to judge, so no number is invented."""
    assert estimate_noise(float("-inf"), float("-inf")) == (None, None)


def test_missing_levels_produce_no_estimate():
    assert estimate_noise(None, -70.0) == (None, None)
    assert estimate_noise(-20.0, None) == (None, None)


def test_the_documented_false_positive_is_real(wav_file):
    """A clean continuous tone is labelled noisy. This is a known limitation.

    The caveat block above `estimate_noise` says a recording with no pauses
    scores badly. This asserts that claim is honest rather than hedging:
    the file is a pure clean sine and the estimate calls it noisy.
    """
    meta = extract(wav_file)
    assert meta.quality_estimate == "noisy"


# ==========================================================================
# Bitrate resolution, as a pure function
# ==========================================================================


def test_stream_bitrate_wins_over_container():
    bitrate, derived, note = resolve_bitrate(
        {"bit_rate": "128000"}, {"bit_rate": "132000"}, 1000, 10.0
    )
    assert (bitrate, derived, note) == (128000, False, None)


def test_container_bitrate_is_used_when_the_stream_has_none():
    bitrate, derived, note = resolve_bitrate({}, {"bit_rate": "81446"}, 1000, 10.0)
    assert bitrate == 81446
    assert derived is False
    assert "container header" in note


def test_bitrate_is_derived_only_as_a_last_resort():
    bitrate, derived, note = resolve_bitrate({}, {}, 30580, 3.008)
    assert bitrate == int(30580 * 8 / 3.008)
    assert derived is True
    assert "byte_size * 8 / duration" in note


def test_no_duration_means_no_bitrate_rather_than_a_guess():
    assert resolve_bitrate({}, {}, 30580, None) == (None, False, None)
    assert resolve_bitrate({}, {}, 30580, 0.0) == (None, False, None)


# ==========================================================================
# Failure handling -- extract must never raise
# ==========================================================================


def test_a_text_file_is_reported_as_unusable_not_crashed_on(tmp_path):
    path = tmp_path / "not_audio.txt"
    path.write_text("this is plainly not audio", encoding="utf-8")

    meta = extract(path)
    assert isinstance(meta, AudioMetadata)
    assert meta.duration_seconds is None
    assert meta.sample_rate_hz is None
    assert meta.problems
    assert "no audio stream" in meta.problems[0]


def test_a_missing_file_is_reported_not_crashed_on(tmp_path):
    meta = extract(tmp_path / "nope.wav")
    assert meta.problems == ("File does not exist.",)
    assert meta.byte_size == 0


def test_an_empty_file_is_reported_not_crashed_on(tmp_path):
    path = tmp_path / "empty.wav"
    path.write_bytes(b"")

    meta = extract(path)
    assert meta.problems
    assert meta.duration_seconds is None
