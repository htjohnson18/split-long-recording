#!/usr/bin/env python3
"""Split a long WAV recording into songs by detecting the gaps between them."""

import argparse
import re
import subprocess
import sys
from pathlib import Path

# LAME VBR quality levels. Lower number = higher bitrate.
MP3_QUALITY_PRESETS = {
    "high": 0,      # ~245 kbps VBR — archival
    "standard": 2,  # ~190 kbps VBR — transparent for most listening
    "compact": 4,   # ~165 kbps VBR — smaller files for phones and sharing
}
DEFAULT_MP3_QUALITY = "standard"

DEFAULT_NORMALIZE_LUFS = -16.0
DEFAULT_NORMALIZE_LRA = 11.0
DEFAULT_NORMALIZE_TRUE_PEAK = -1.5
DEFAULT_VOCAL_EQ_HIGHPASS = 120.0
DEFAULT_VOCAL_EQ_PRESENCE_FREQ = 2500.0
DEFAULT_VOCAL_EQ_PRESENCE_Q = 1.2
DEFAULT_VOCAL_EQ_PRESENCE_GAIN = 3.0
DEFAULT_VOCAL_EQ_CLARITY_FREQ = 4500.0
DEFAULT_VOCAL_EQ_CLARITY_Q = 1.0
DEFAULT_VOCAL_EQ_CLARITY_GAIN = 2.0


def get_duration(input_file):
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(input_file),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def get_audio_codec(input_file):
    cmd = [
        "ffprobe", "-v", "quiet",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(input_file),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def detect_silences(input_file, silence_db, silence_duration):
    """Return list of (start, end) silence intervals in seconds."""
    cmd = [
        "ffmpeg", "-i", str(input_file),
        "-af", f"silencedetect=noise={silence_db}dB:d={silence_duration}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    silences = []
    silence_start = None
    for line in result.stderr.splitlines():
        if "silence_start" in line:
            m = re.search(r"silence_start: ([\d.]+)", line)
            if m:
                silence_start = float(m.group(1))
        elif "silence_end" in line and silence_start is not None:
            m = re.search(r"silence_end: ([\d.]+)", line)
            if m:
                silences.append((silence_start, float(m.group(1))))
                silence_start = None
    return silences


def get_sample_rate(input_file):
    cmd = [
        "ffprobe", "-v", "quiet",
        "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(input_file),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return int(result.stdout.strip())


def measure_envelope(input_file, window):
    """Return [(time, rms_dbfs)] sampled every `window` seconds."""
    sample_rate = get_sample_rate(input_file)
    frame = max(1, int(round(window * sample_rate)))
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(input_file),
        "-af",
        f"asetnsamples=n={frame},astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    envelope = []
    timestamp = None
    for line in result.stderr.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            timestamp = float(m.group(1))
            continue
        m = re.search(r"RMS_level=(-?[\d.]+|-?inf)", line)
        if m and timestamp is not None:
            value = m.group(1)
            envelope.append((timestamp, -99.0 if "inf" in value else float(value)))
            timestamp = None
    return envelope


def detect_quiet_regions(input_file, threshold_db, bridge, min_gap, window=0.5):
    """Return (start, end) quiet regions found in the RMS envelope.

    Unlike raw silencedetect, a brief transient — a stick click, a cough, one
    clap — does not break a gap in two: a loud stretch shorter than `bridge`
    seconds is absorbed into the surrounding quiet. Live recordings are full of
    those, and they are what makes plain silence detection miss real boundaries.
    """
    envelope = measure_envelope(input_file, window)
    if not envelope:
        return []

    quiet = [db < threshold_db for _, db in envelope]
    total = len(quiet)
    regions = []
    i = 0
    while i < total:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < total:
            if quiet[j]:
                j += 1
                continue
            k = j
            while k < total and not quiet[k]:
                k += 1
            if k < total and (k - j) * window <= bridge:
                j = k  # short blip inside the gap — keep going
            else:
                break
        start = envelope[i][0]
        end = envelope[j][0] if j < total else envelope[-1][0] + window
        if end - start >= min_gap:
            regions.append((start, end))
        i = max(j, i + 1)
    return regions


def get_mean_volume(input_file, start, end):
    """Return mean volume in dBFS for a segment, or None if ffmpeg doesn't report it."""
    duration = end - start
    if duration <= 0:
        return None

    cmd = [
        "ffmpeg", "-hide_banner",
        "-ss", str(start), "-i", str(input_file),
        "-t", str(duration),
        "-af", "volumedetect",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    for line in result.stderr.splitlines():
        m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", line)
        if m:
            return float(m.group(1))
    return None


def parse_timestamp(ts):
    """Parse HH:MM:SS, MM:SS, or bare seconds into a float."""
    ts = ts.strip()
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        else:
            return float(ts)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid timestamp: {ts!r}  (use HH:MM:SS, MM:SS, or seconds)")


def silences_to_segments(silences, total_duration, forced_splits):
    """Convert silence intervals + forced split points to (start, end) song segments."""
    # Build a sorted list of split points from silence gaps and forced timestamps
    split_points = sorted(set(forced_splits))

    segments = []
    cursor = 0.0
    silence_iter = iter(silences)
    sil = next(silence_iter, None)

    for split in split_points:
        # Advance through silences that end before this forced split
        while sil and sil[1] <= split:
            if sil[0] > cursor:
                segments.append((cursor, sil[0]))
            cursor = sil[1]
            sil = next(silence_iter, None)

        # Emit segment up to the forced split (skip if cursor already past it)
        if split > cursor:
            segments.append((cursor, split))
            cursor = split
        # If cursor is inside a silence at the forced split, skip past it
        if sil and sil[0] <= cursor and sil[1] > cursor:
            cursor = sil[1]
            sil = next(silence_iter, None)

    # Drain remaining silences
    while sil:
        if sil[0] > cursor:
            segments.append((cursor, sil[0]))
        cursor = sil[1]
        sil = next(silence_iter, None)

    if cursor < total_duration:
        segments.append((cursor, total_duration))

    return segments


def merge_short_segments(segments, min_duration, pinned):
    """Merge segments shorter than min_duration into the following segment
    (or the previous one if it's the last segment).
    Never merges across a pinned boundary (i.e. a forced split point)."""
    if not segments:
        return segments
    changed = True
    while changed:
        changed = False
        result = []
        i = 0
        while i < len(segments):
            start, end = segments[i]
            duration = end - start
            if duration < min_duration:
                # Can we absorb into next? Only if the shared boundary isn't pinned.
                if i + 1 < len(segments) and end not in pinned:
                    segments[i + 1] = (start, segments[i + 1][1])
                    changed = True
                    i += 1
                    continue
                # Can we absorb into previous? Only if the shared boundary isn't pinned.
                elif result and start not in pinned:
                    result[-1] = (result[-1][0], end)
                    changed = True
                    i += 1
                    continue
            result.append((start, end))
            i += 1
        segments = result
    return segments


def drop_leading_quiet_segments(input_file, segments, threshold_db):
    """Drop leading segments whose mean volume stays below threshold_db."""
    kept = list(segments)
    dropped = []
    while kept:
        start, end = kept[0]
        mean_volume = get_mean_volume(input_file, start, end)
        if mean_volume is None or mean_volume >= threshold_db:
            break
        dropped.append((start, end, mean_volume))
        kept.pop(0)
    return kept, dropped


def drop_short_segments(segments, min_duration, pinned):
    """Drop segments shorter than min_duration instead of merging them.

    The counterpart to merge_short_segments, for envelope detection: what sits
    between songs in a live set is banter and tuning, not part of the next
    track. Segments on a forced split boundary are always kept.
    """
    kept = []
    dropped = []
    for start, end in segments:
        if end - start < min_duration and start not in pinned and end not in pinned:
            dropped.append((start, end))
        else:
            kept.append((start, end))
    return kept, dropped


def drop_quiet_segments(input_file, segments, threshold_db):
    """Drop every segment whose mean volume stays below threshold_db.

    Catches talk that ran long enough to survive the length filter: a band
    playing sits far above the room talking between songs.
    """
    kept = []
    dropped = []
    for start, end in segments:
        mean_volume = get_mean_volume(input_file, start, end)
        if mean_volume is not None and mean_volume < threshold_db:
            dropped.append((start, end, mean_volume))
        else:
            kept.append((start, end))
    return kept, dropped


def pad_segments(segments, pad_start, pad_end, total_duration):
    """Widen each segment into the surrounding quiet so the first note and the
    final ring-out survive. Never crosses the midpoint of the gap to a
    neighbour, so padded segments cannot overlap."""
    if not (pad_start or pad_end):
        return segments
    padded = []
    for i, (start, end) in enumerate(segments):
        new_start = max(0.0, start - pad_start)
        new_end = min(total_duration, end + pad_end)
        if i > 0:
            new_start = max(new_start, (segments[i - 1][1] + start) / 2)
        if i + 1 < len(segments):
            new_end = min(new_end, (end + segments[i + 1][0]) / 2)
        padded.append((new_start, new_end))
    return padded


def fmt(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:05.2f}"
    return f"{m:02d}:{s:05.2f}"


def build_filter_chain(args):
    filters = []
    if args.vocal_eq:
        filters.append(
            "highpass=f={highpass},"
            "equalizer=f={presence_freq}:t=q:w={presence_q}:g={presence_gain},"
            "equalizer=f={clarity_freq}:t=q:w={clarity_q}:g={clarity_gain}".format(
                highpass=args.vocal_eq_highpass,
                presence_freq=args.vocal_eq_presence_freq,
                presence_q=DEFAULT_VOCAL_EQ_PRESENCE_Q,
                presence_gain=args.vocal_eq_presence_gain,
                clarity_freq=args.vocal_eq_clarity_freq,
                clarity_q=DEFAULT_VOCAL_EQ_CLARITY_Q,
                clarity_gain=args.vocal_eq_clarity_gain,
            )
        )
    if args.normalize:
        filters.append(
            "loudnorm=I={integrated}:LRA={lra}:TP={true_peak}".format(
                integrated=args.normalize_lufs,
                lra=args.normalize_lra,
                true_peak=args.normalize_true_peak,
            )
        )
    return ",".join(filters)


def split_and_encode(input_file, segments, output_dir, fmt_arg, audio_filter=None, wav_codec="pcm_s16le",
                     mp3_quality=MP3_QUALITY_PRESETS[DEFAULT_MP3_QUALITY]):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(input_file).stem
    created = []

    for i, (start, end) in enumerate(segments, 1):
        duration = end - start
        base = output_dir / f"{stem}_{i:02d}"

        if fmt_arg == "wav" and not audio_filter:
            wav_path = base.with_suffix(".wav")
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start), "-i", str(input_file),
                "-t", str(duration),
                "-c", "copy", str(wav_path),
            ]
            subprocess.run(cmd, capture_output=True, check=True)
            created.append(wav_path)
            continue

        outputs = []
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start), "-i", str(input_file),
            "-t", str(duration),
        ]

        if audio_filter:
            cmd.extend(["-af", audio_filter])

        if fmt_arg in ("wav", "both"):
            wav_path = base.with_suffix(".wav")
            cmd.extend([
                "-map", "0:a:0",
                "-c:a", wav_codec,
                str(wav_path),
            ])
            outputs.append(wav_path)

        if fmt_arg in ("mp3", "both"):
            mp3_path = base.with_suffix(".mp3")
            cmd.extend([
                "-map", "0:a:0",
                "-c:a", "libmp3lame",
                "-q:a", str(mp3_quality),
                str(mp3_path),
            ])
            outputs.append(mp3_path)

        subprocess.run(cmd, capture_output=True, check=True)
        created.extend(outputs)

    return created


def main():
    parser = argparse.ArgumentParser(
        description="Split a long WAV recording into songs based on silence detection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n  %(prog)s session.wav --split-at 21:39 --split-at 1:02:15",
    )
    parser.add_argument("input", help="Input WAV file")
    parser.add_argument("-o", "--output-dir", default="output",
                        help="Output directory (default: output)")
    parser.add_argument("--detect", choices=["silence", "envelope"], default="silence",
                        help="Boundary detection method. 'silence' uses ffmpeg silencedetect; "
                             "'envelope' uses an RMS envelope that ignores brief transients "
                             "(recommended for live recordings with an audience)")
    parser.add_argument("--envelope-db", type=float, default=-38,
                        help="RMS threshold in dB below which a window counts as quiet, for "
                             "--detect envelope (default: -38)")
    parser.add_argument("--envelope-bridge", type=float, default=2.0,
                        help="Loud blips shorter than this many seconds do not break a gap, for "
                             "--detect envelope (default: 2.0)")
    parser.add_argument("--envelope-min-gap", type=float, default=5.0,
                        help="Minimum sustained quiet in seconds to count as a boundary, for "
                             "--detect envelope (default: 5.0)")
    parser.add_argument("--silence-db", type=float, default=-40,
                        help="Silence threshold in dB (default: -40)")
    parser.add_argument("--silence-duration", type=float, default=7.0,
                        help="Minimum silence duration in seconds (default: 7.0)")
    parser.add_argument("--min-segment", type=float, default=120.0,
                        help="Minimum segment duration in seconds; shorter segments are merged "
                             "(default: 120.0)")
    parser.add_argument("--drop-short", action="store_true",
                        help="Drop segments shorter than --min-segment instead of merging them "
                             "into a neighbour (discards between-song banter and tuning)")
    parser.add_argument("--pad-start", type=float, default=0.0,
                        help="Seconds of lead-in to keep before each segment (default: 0)")
    parser.add_argument("--pad-end", type=float, default=0.0,
                        help="Seconds of tail to keep after each segment, for ring-outs "
                             "(default: 0)")
    parser.add_argument("--drop-quiet-db", type=float,
                        help="Drop any segment whose mean volume stays below this dBFS threshold "
                             "(not just leading ones) — catches long stretches of talk")
    parser.add_argument("--drop-leading-quiet-db", type=float,
                        help="Drop leading merged segments whose mean volume stays below this dBFS "
                             "threshold (useful for dead air / setup noise before a practice)")
    parser.add_argument("--split-at", metavar="TIME", type=parse_timestamp, action="append",
                        default=[],
                        help="Force a split at this timestamp (HH:MM:SS, MM:SS, or seconds). "
                             "Can be repeated.")
    parser.add_argument("--format", choices=["wav", "mp3", "both"], default="mp3",
                        help="Output format (default: mp3)")
    parser.add_argument("--mp3-quality", choices=sorted(MP3_QUALITY_PRESETS), default=DEFAULT_MP3_QUALITY,
                        help="MP3 VBR quality preset: high (~245 kbps), standard (~190 kbps), "
                             f"compact (~165 kbps) (default: {DEFAULT_MP3_QUALITY})")
    parser.add_argument("--normalize", action="store_true",
                        help="Apply one-pass loudness normalization before encoding")
    parser.add_argument("--normalize-lufs", type=float, default=DEFAULT_NORMALIZE_LUFS,
                        help=f"Integrated loudness target for --normalize (default: {DEFAULT_NORMALIZE_LUFS})")
    parser.add_argument("--normalize-lra", type=float, default=DEFAULT_NORMALIZE_LRA,
                        help=f"Loudness range target for --normalize (default: {DEFAULT_NORMALIZE_LRA})")
    parser.add_argument("--normalize-true-peak", type=float, default=DEFAULT_NORMALIZE_TRUE_PEAK,
                        help=f"True peak ceiling for --normalize in dBTP (default: {DEFAULT_NORMALIZE_TRUE_PEAK})")
    parser.add_argument("--vocal-eq", action="store_true",
                        help="Apply a mild EQ curve to bring vocals forward before encoding")
    parser.add_argument("--vocal-eq-highpass", type=float, default=DEFAULT_VOCAL_EQ_HIGHPASS,
                        help=f"High-pass cutoff for --vocal-eq in Hz (default: {DEFAULT_VOCAL_EQ_HIGHPASS})")
    parser.add_argument("--vocal-eq-presence-freq", type=float, default=DEFAULT_VOCAL_EQ_PRESENCE_FREQ,
                        help=f"Presence boost center for --vocal-eq in Hz (default: {DEFAULT_VOCAL_EQ_PRESENCE_FREQ})")
    parser.add_argument("--vocal-eq-presence-gain", type=float, default=DEFAULT_VOCAL_EQ_PRESENCE_GAIN,
                        help=f"Presence boost gain for --vocal-eq in dB (default: {DEFAULT_VOCAL_EQ_PRESENCE_GAIN})")
    parser.add_argument("--vocal-eq-clarity-freq", type=float, default=DEFAULT_VOCAL_EQ_CLARITY_FREQ,
                        help=f"Clarity boost center for --vocal-eq in Hz (default: {DEFAULT_VOCAL_EQ_CLARITY_FREQ})")
    parser.add_argument("--vocal-eq-clarity-gain", type=float, default=DEFAULT_VOCAL_EQ_CLARITY_GAIN,
                        help=f"Clarity boost gain for --vocal-eq in dB (default: {DEFAULT_VOCAL_EQ_CLARITY_GAIN})")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    input_file = Path(args.input)
    if not input_file.exists():
        print(f"error: {input_file} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Analyzing {input_file.name} ...")
    total_duration = get_duration(input_file)
    wav_codec = get_audio_codec(input_file)
    print(f"  Total duration : {fmt(total_duration)}")

    out_of_range = [t for t in args.split_at if t <= 0 or t >= total_duration]
    if out_of_range:
        labels = ", ".join(fmt(t) for t in sorted(out_of_range))
        print(
            f"error: split points must be inside the file duration ({fmt(total_duration)}): {labels}",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.detect == "envelope":
        print(f"  Detecting gaps from RMS envelope (>{args.envelope_min_gap}s below "
              f"{args.envelope_db}dB, bridging blips up to {args.envelope_bridge}s) ...")
    else:
        print(f"  Detecting silences (>{args.silence_duration}s below {args.silence_db}dB) ...")
    try:
        if args.detect == "envelope":
            silences = detect_quiet_regions(
                input_file, args.envelope_db, args.envelope_bridge, args.envelope_min_gap
            )
        else:
            silences = detect_silences(input_file, args.silence_db, args.silence_duration)
    except subprocess.CalledProcessError as exc:
        print("error: ffmpeg gap detection failed", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        sys.exit(exc.returncode or 1)
    print(f"  Found {len(silences)} gap(s)")

    if args.split_at:
        labels = ", ".join(fmt(t) for t in sorted(args.split_at))
        print(f"  Forced split(s) at: {labels}")

    pinned = set(args.split_at)
    segments = silences_to_segments(silences, total_duration, args.split_at)
    if args.drop_short:
        segments, dropped_short = drop_short_segments(segments, args.min_segment, pinned)
        for start, end in dropped_short:
            print(f"  Dropped short segment {fmt(start)}-{fmt(end)} "
                  f"({fmt(end - start)} < {args.min_segment:g}s)")
    else:
        before = len(segments)
        segments = merge_short_segments(segments, args.min_segment, pinned)
        merged = before - len(segments)
        if merged:
            print(f"  Merged {merged} short segment(s) below {args.min_segment}s into adjacent tracks")

    if args.drop_leading_quiet_db is not None:
        try:
            segments, dropped = drop_leading_quiet_segments(input_file, segments, args.drop_leading_quiet_db)
        except subprocess.CalledProcessError as exc:
            print("error: ffmpeg volume analysis failed", file=sys.stderr)
            if exc.stderr:
                print(exc.stderr.strip(), file=sys.stderr)
            sys.exit(exc.returncode or 1)
        for start, end, mean_volume in dropped:
            print(
                "  Dropped leading quiet segment "
                f"{fmt(start)}-{fmt(end)} (mean {mean_volume:.1f} dBFS < {args.drop_leading_quiet_db:g} dBFS)"
            )

    if args.drop_quiet_db is not None:
        try:
            segments, dropped = drop_quiet_segments(input_file, segments, args.drop_quiet_db)
        except subprocess.CalledProcessError as exc:
            print("error: ffmpeg volume analysis failed", file=sys.stderr)
            if exc.stderr:
                print(exc.stderr.strip(), file=sys.stderr)
            sys.exit(exc.returncode or 1)
        for start, end, mean_volume in dropped:
            print(
                "  Dropped quiet segment "
                f"{fmt(start)}-{fmt(end)} (mean {mean_volume:.1f} dBFS < {args.drop_quiet_db:g} dBFS)"
            )

    segments = pad_segments(segments, args.pad_start, args.pad_end, total_duration)

    if args.format in ("mp3", "both"):
        print(f"  Encoding MP3 at {args.mp3_quality} quality (LAME V{MP3_QUALITY_PRESETS[args.mp3_quality]})")

    audio_filter = build_filter_chain(args)
    if args.vocal_eq:
        print(
            "  Applying vocal EQ during export "
            f"(HPF {args.vocal_eq_highpass:g}Hz, +{args.vocal_eq_presence_gain:g}dB @ "
            f"{args.vocal_eq_presence_freq:g}Hz, +{args.vocal_eq_clarity_gain:g}dB @ "
            f"{args.vocal_eq_clarity_freq:g}Hz)"
        )
    if args.normalize:
        print(
            "  Applying one-pass loudness normalization during export "
            f"(I={args.normalize_lufs:g}, LRA={args.normalize_lra:g}, TP={args.normalize_true_peak:g})"
        )

    print(f"\nProposed splits — {len(segments)} track(s):\n")
    print(f"  {'#':>3}  {'Start':>8}  {'End':>8}  {'Duration':>8}")
    print(f"  {'—'*3}  {'—'*8}  {'—'*8}  {'—'*8}")
    for i, (start, end) in enumerate(segments, 1):
        print(f"  {i:>3}  {fmt(start):>8}  {fmt(end):>8}  {fmt(end - start):>8}")

    if not args.yes:
        try:
            answer = input("\nProceed with splitting? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    print(f"\nWriting to {args.output_dir}/ ...")
    if not wav_codec.startswith("pcm_"):
        wav_codec = "pcm_s16le"
    mp3_quality = MP3_QUALITY_PRESETS[args.mp3_quality]
    created = split_and_encode(input_file, segments, args.output_dir, args.format, audio_filter,
                               wav_codec, mp3_quality)
    print(f"Done — {len(created)} file(s) created.")
    for f in created:
        print(f"  {f}")


if __name__ == "__main__":
    main()
