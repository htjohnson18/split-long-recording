#!/usr/bin/env python3
"""Tests for the envelope detection and segment shaping logic.

Run with: python3 -m unittest -v

The ffmpeg-dependent tests build a small synthetic WAV with the stdlib `wave`
module, so they need ffmpeg on PATH but no fixture files in the repo.
"""

import argparse
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import split_recording as sr


HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

WINDOW = 0.5


def envelope_from_levels(levels, window=WINDOW):
    """Turn a list of dB values into the (time, dbfs) pairs measure_envelope returns."""
    return [(i * window, db) for i, db in enumerate(levels)]


def write_wav(path, blocks, sample_rate=8000, freq=220.0):
    """Write a mono 16-bit WAV from [(duration_seconds, amplitude)] blocks."""
    frames = bytearray()
    phase = 0
    for duration, amplitude in blocks:
        for _ in range(int(duration * sample_rate)):
            value = int(amplitude * 32767 * math.sin(2 * math.pi * freq * phase / sample_rate))
            frames += struct.pack("<h", max(-32768, min(32767, value)))
            phase += 1
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))


class MeasureEnvelopeParsingTests(unittest.TestCase):
    """measure_envelope reads its numbers out of ffmpeg's stderr, so the parsing
    is worth pinning down against realistic output."""

    STDERR = """\
[Parsed_ametadata_2 @ 0x7f8e1] frame:0    pts:0       pts_time:0
[Parsed_ametadata_2 @ 0x7f8e1] lavfi.astats.Overall.RMS_level=-12.345678
[Parsed_ametadata_2 @ 0x7f8e1] frame:1    pts:22050   pts_time:0.5
[Parsed_ametadata_2 @ 0x7f8e1] lavfi.astats.Overall.RMS_level=-61.2
[Parsed_ametadata_2 @ 0x7f8e1] frame:2    pts:44100   pts_time:1
[Parsed_ametadata_2 @ 0x7f8e1] lavfi.astats.Overall.RMS_level=-inf
"""

    def measure(self, stderr, window=0.5, sample_rate=44100):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=stderr)
        with mock.patch.object(sr, "get_sample_rate", return_value=sample_rate), \
                mock.patch.object(sr.subprocess, "run", return_value=completed) as run:
            envelope = sr.measure_envelope("fake.wav", window)
        return envelope, run

    def test_parses_time_and_level_pairs(self):
        envelope, _ = self.measure(self.STDERR)
        self.assertEqual(envelope[0], (0.0, -12.345678))
        self.assertEqual(envelope[1], (0.5, -61.2))

    def test_infinite_level_becomes_floor_not_a_crash(self):
        """Digital silence reports -inf; float('-inf') would poison later maths."""
        envelope, _ = self.measure(self.STDERR)
        self.assertEqual(envelope[2], (1.0, -99.0))

    def test_frame_size_follows_window_and_sample_rate(self):
        _, run = self.measure(self.STDERR, window=0.25, sample_rate=48000)
        filter_arg = run.call_args[0][0][run.call_args[0][0].index("-af") + 1]
        self.assertIn("asetnsamples=n=12000", filter_arg)

    def test_level_without_a_preceding_timestamp_is_ignored(self):
        envelope, _ = self.measure(
            "[Parsed_ametadata_2 @ 0x1] lavfi.astats.Overall.RMS_level=-20.0\n"
        )
        self.assertEqual(envelope, [])

    def test_empty_output_yields_empty_envelope(self):
        envelope, _ = self.measure("")
        self.assertEqual(envelope, [])


class DetectQuietRegionsTests(unittest.TestCase):
    """The bridging logic is the reason envelope detection exists, so most of
    these cases are about what does and does not break a gap in two."""

    def detect(self, levels, threshold_db=-38, bridge=2.0, min_gap=5.0):
        envelope = envelope_from_levels(levels)
        with mock.patch.object(sr, "measure_envelope", return_value=envelope):
            return sr.detect_quiet_regions("fake.wav", threshold_db, bridge, min_gap, WINDOW)

    def test_finds_a_sustained_gap_between_two_loud_stretches(self):
        levels = [-20] * 10 + [-60] * 20 + [-20] * 10  # 5s loud, 10s quiet, 5s loud
        self.assertEqual(self.detect(levels), [(5.0, 15.0)])

    def test_gap_shorter_than_min_gap_is_not_a_boundary(self):
        levels = [-20] * 10 + [-60] * 8 + [-20] * 10  # 4s of quiet
        self.assertEqual(self.detect(levels), [])

    def test_gap_exactly_min_gap_counts(self):
        levels = [-20] * 10 + [-60] * 10 + [-20] * 10  # 5.0s of quiet
        self.assertEqual(self.detect(levels), [(5.0, 10.0)])

    def test_brief_transient_does_not_break_a_gap(self):
        """A stick click or a cough in an otherwise quiet room: 1.5s of noise
        inside a 12s gap must stay one gap, not become two short ones."""
        levels = [-20] * 10 + [-60] * 8 + [-15] * 3 + [-60] * 13 + [-20] * 10
        self.assertEqual(self.detect(levels), [(5.0, 17.0)])

    def test_noise_longer_than_bridge_does_break_a_gap(self):
        levels = [-20] * 10 + [-60] * 20 + [-15] * 6 + [-60] * 20 + [-20] * 10
        self.assertEqual(self.detect(levels), [(5.0, 15.0), (18.0, 28.0)])

    def test_bridged_fragments_are_measured_as_one_gap_against_min_gap(self):
        """Neither half clears min_gap alone; bridged they do. This is exactly
        what plain silencedetect cannot express."""
        levels = [-20] * 10 + [-60] * 6 + [-15] * 2 + [-60] * 6 + [-20] * 10
        self.assertEqual(self.detect(levels, min_gap=5.0), [(5.0, 12.0)])
        self.assertEqual(self.detect(levels, bridge=0.5, min_gap=5.0), [])

    def test_quiet_running_to_the_end_extends_one_window_past_the_last_sample(self):
        levels = [-20] * 10 + [-60] * 12
        self.assertEqual(self.detect(levels), [(5.0, 11.0)])

    def test_quiet_from_the_very_start(self):
        levels = [-60] * 12 + [-20] * 10
        self.assertEqual(self.detect(levels), [(0.0, 6.0)])

    def test_threshold_is_strict_so_a_level_on_the_line_is_loud(self):
        self.assertEqual(self.detect([-38.0] * 40, threshold_db=-38), [])
        self.assertEqual(len(self.detect([-38.001] * 40, threshold_db=-38)), 1)

    def test_empty_envelope_returns_no_regions(self):
        with mock.patch.object(sr, "measure_envelope", return_value=[]):
            self.assertEqual(sr.detect_quiet_regions("fake.wav", -38, 2.0, 5.0), [])

    def test_all_quiet_is_a_single_region(self):
        self.assertEqual(self.detect([-60] * 20), [(0.0, 10.0)])

    def test_all_loud_has_no_regions(self):
        self.assertEqual(self.detect([-20] * 20), [])


class PadSegmentsTests(unittest.TestCase):
    def test_no_padding_is_a_no_op(self):
        segments = [(10.0, 20.0), (30.0, 40.0)]
        self.assertEqual(sr.pad_segments(segments, 0.0, 0.0, 100.0), segments)

    def test_padding_widens_both_ends(self):
        self.assertEqual(
            sr.pad_segments([(10.0, 20.0)], 1.0, 2.0, 100.0),
            [(9.0, 22.0)],
        )

    def test_padding_is_clamped_to_the_recording(self):
        self.assertEqual(
            sr.pad_segments([(0.5, 99.0)], 5.0, 5.0, 100.0),
            [(0.0, 100.0)],
        )

    def test_padding_never_crosses_the_midpoint_of_a_gap(self):
        # 10s gap between the two segments: each may claim at most 5s of it.
        padded = sr.pad_segments([(0.0, 20.0), (30.0, 50.0)], 30.0, 30.0, 100.0)
        self.assertEqual(padded, [(0.0, 25.0), (25.0, 80.0)])

    def test_padded_segments_never_overlap(self):
        segments = [(9.5, 160.5), (170.0, 370.0), (395.0, 596.0), (621.5, 772.0)]
        padded = sr.pad_segments(segments, 60.0, 60.0, 1000.0)
        for (_, end), (next_start, _) in zip(padded, padded[1:]):
            self.assertLessEqual(end, next_start)

    def test_asymmetric_padding_only_moves_the_requested_edge(self):
        self.assertEqual(sr.pad_segments([(10.0, 20.0)], 0.0, 3.0, 100.0), [(10.0, 23.0)])
        self.assertEqual(sr.pad_segments([(10.0, 20.0)], 3.0, 0.0, 100.0), [(7.0, 20.0)])

    def test_empty_input(self):
        self.assertEqual(sr.pad_segments([], 1.0, 1.0, 100.0), [])


class NonnegativeFloatTests(unittest.TestCase):
    def test_accepts_zero_and_positive_values(self):
        self.assertEqual(sr.nonnegative_float("0"), 0.0)
        self.assertEqual(sr.nonnegative_float("2.5"), 2.5)

    def test_rejects_negative_values(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            sr.nonnegative_float("-0.1")

    def test_rejects_non_numbers(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            sr.nonnegative_float("later")

    def test_cli_refuses_negative_padding(self):
        """Regression: negative padding used to reach pad_segments and propose
        segments that ran backwards past their own end."""
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("split_recording.py")),
             "input.wav", "--pad-start", "-1000"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--pad-start", result.stderr)


class DropShortSegmentsTests(unittest.TestCase):
    def test_short_segments_are_dropped_not_merged(self):
        segments = [(0.0, 150.0), (160.0, 190.0), (200.0, 350.0)]
        kept, dropped = sr.drop_short_segments(segments, 60.0, set())
        self.assertEqual(kept, [(0.0, 150.0), (200.0, 350.0)])
        self.assertEqual(dropped, [(160.0, 190.0)])

    def test_segment_exactly_at_min_duration_is_kept(self):
        kept, dropped = sr.drop_short_segments([(0.0, 60.0)], 60.0, set())
        self.assertEqual(kept, [(0.0, 60.0)])
        self.assertEqual(dropped, [])

    def test_forced_split_boundaries_are_never_dropped(self):
        segments = [(0.0, 10.0), (10.0, 20.0)]
        kept, dropped = sr.drop_short_segments(segments, 60.0, {10.0})
        self.assertEqual(kept, segments)
        self.assertEqual(dropped, [])


class DropQuietSegmentsTests(unittest.TestCase):
    def drop(self, volumes, threshold_db=-32.0):
        segments = [(float(i), float(i) + 10.0) for i in range(len(volumes))]
        with mock.patch.object(sr, "get_mean_volume", side_effect=volumes):
            return sr.drop_quiet_segments("fake.wav", segments, threshold_db)

    def test_quiet_segments_are_dropped_with_their_measurement(self):
        kept, dropped = self.drop([-26.0, -45.0])
        self.assertEqual(kept, [(0.0, 10.0)])
        self.assertEqual(dropped, [(1.0, 11.0, -45.0)])

    def test_unmeasurable_segments_are_kept(self):
        kept, dropped = self.drop([None])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe are required")
class EnvelopeIntegrationTests(unittest.TestCase):
    """End to end over real ffmpeg: synthesise tone / room tone / tone and check
    that the gap comes back where it was written."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls._tmp.name) / "synthetic.wav"
        write_wav(cls.path, [
            (4.0, 0.5),     # 0-4s   music
            (3.8, 0.002),   # 4-7.8s room tone
            (0.4, 0.5),     # 7.8-8.2s a clap in the quiet
            (3.8, 0.002),   # 8.2-12s room tone
            (4.0, 0.5),     # 12-16s music
        ])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_sample_rate_and_duration_round_trip(self):
        self.assertEqual(sr.get_sample_rate(self.path), 8000)
        self.assertAlmostEqual(sr.get_duration(self.path), 16.0, places=2)

    def test_envelope_covers_the_file_and_separates_loud_from_quiet(self):
        envelope = sr.measure_envelope(self.path, WINDOW)
        self.assertGreaterEqual(len(envelope), 31)
        levels = dict(envelope)
        self.assertGreater(levels[1.0], -20)   # inside the first tone
        self.assertLess(levels[6.0], -38)      # inside the room tone

    def test_the_clap_does_not_break_the_gap(self):
        regions = sr.detect_quiet_regions(self.path, -38, 2.0, 5.0, WINDOW)
        self.assertEqual(len(regions), 1, f"expected one bridged gap, got {regions}")
        start, end = regions[0]
        self.assertAlmostEqual(start, 4.0, delta=0.6)
        self.assertAlmostEqual(end, 12.0, delta=0.6)

    def test_without_bridging_the_clap_splits_the_gap(self):
        regions = sr.detect_quiet_regions(self.path, -38, 0.0, 3.0, WINDOW)
        self.assertEqual(len(regions), 2, f"expected the clap to split the gap, got {regions}")

    def test_detected_gaps_become_the_expected_segments(self):
        regions = sr.detect_quiet_regions(self.path, -38, 2.0, 5.0, WINDOW)
        segments = sr.silences_to_segments(regions, sr.get_duration(self.path), [])
        self.assertEqual(len(segments), 2)
        self.assertAlmostEqual(segments[0][0], 0.0, places=2)
        self.assertAlmostEqual(segments[1][1], 16.0, delta=0.05)


if __name__ == "__main__":
    unittest.main()
