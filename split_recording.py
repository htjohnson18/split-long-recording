#!/usr/bin/env python3
"""Split a long WAV recording into songs based on silence detection."""

import argparse
import re
import subprocess
import sys
from pathlib import Path


def get_duration(input_file):
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(input_file),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def detect_silences(input_file, silence_db, silence_duration):
    """Return list of (start, end) silence intervals in seconds."""
    cmd = [
        "ffmpeg", "-i", str(input_file),
        "-af", f"silencedetect=noise={silence_db}dB:d={silence_duration}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
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


def fmt(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:05.2f}"
    return f"{m:02d}:{s:05.2f}"


def split_and_encode(input_file, segments, output_dir, fmt_arg):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(input_file).stem
    created = []

    for i, (start, end) in enumerate(segments, 1):
        duration = end - start
        base = output_dir / f"{stem}_{i:02d}"

        wav_path = base.with_suffix(".wav")
        mp3_path = base.with_suffix(".mp3")

        if fmt_arg in ("wav", "both"):
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start), "-i", str(input_file),
                "-t", str(duration),
                "-c", "copy", str(wav_path),
            ]
            subprocess.run(cmd, capture_output=True, check=True)
            created.append(wav_path)

        if fmt_arg == "mp3":
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start), "-i", str(input_file),
                "-t", str(duration),
                "-q:a", "0", str(mp3_path),
            ]
            subprocess.run(cmd, capture_output=True, check=True)
            created.append(mp3_path)
        elif fmt_arg == "both":
            cmd = ["ffmpeg", "-y", "-i", str(wav_path), "-q:a", "0", str(mp3_path)]
            subprocess.run(cmd, capture_output=True, check=True)
            created.append(mp3_path)

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
    parser.add_argument("--silence-db", type=float, default=-40,
                        help="Silence threshold in dB (default: -40)")
    parser.add_argument("--silence-duration", type=float, default=7.0,
                        help="Minimum silence duration in seconds (default: 7.0)")
    parser.add_argument("--min-segment", type=float, default=120.0,
                        help="Minimum segment duration in seconds; shorter segments are merged "
                             "(default: 120.0)")
    parser.add_argument("--split-at", metavar="TIME", type=parse_timestamp, action="append",
                        default=[],
                        help="Force a split at this timestamp (HH:MM:SS, MM:SS, or seconds). "
                             "Can be repeated.")
    parser.add_argument("--format", choices=["wav", "mp3", "both"], default="mp3",
                        help="Output format (default: mp3)")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    input_file = Path(args.input)
    if not input_file.exists():
        print(f"error: {input_file} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Analyzing {input_file.name} ...")
    total_duration = get_duration(input_file)
    print(f"  Total duration : {fmt(total_duration)}")

    print(f"  Detecting silences (>{args.silence_duration}s below {args.silence_db}dB) ...")
    silences = detect_silences(input_file, args.silence_db, args.silence_duration)
    print(f"  Found {len(silences)} silence gap(s)")

    if args.split_at:
        labels = ", ".join(fmt(t) for t in sorted(args.split_at))
        print(f"  Forced split(s) at: {labels}")

    pinned = set(args.split_at)
    segments = silences_to_segments(silences, total_duration, args.split_at)
    before = len(segments)
    segments = merge_short_segments(segments, args.min_segment, pinned)
    merged = before - len(segments)
    if merged:
        print(f"  Merged {merged} short segment(s) below {args.min_segment}s into adjacent tracks")

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
    created = split_and_encode(input_file, segments, args.output_dir, args.format)
    print(f"Done — {len(created)} file(s) created.")
    for f in created:
        print(f"  {f}")


if __name__ == "__main__":
    main()
