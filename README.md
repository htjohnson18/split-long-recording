# split-long-recording

Splits a long WAV recording (e.g. a band rehearsal) into individual songs using silence detection. Outputs MP3s by default.

## Requirements

- Python 3
- ffmpeg / ffprobe

## Usage

```
python3 split_recording.py <input.wav> [options]
```

The script shows a proposed split table and asks for confirmation before writing any files.

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `-o`, `--output-dir` | `output` | Directory to write output files |
| `--silence-db` | `-40` | Noise floor threshold in dB |
| `--silence-duration` | `7.0` | Minimum silence length (seconds) to treat as a song boundary |
| `--min-segment` | `120.0` | Segments shorter than this (seconds) are merged into the next track — helps discard false starts |
| `--split-at TIME` | — | Force a split at a specific timestamp (`MM:SS`, `HH:MM:SS`, or seconds). Can be repeated. |
| `--format` | `mp3` | Output format: `wav`, `mp3`, or `both` |
| `-y`, `--yes` | — | Skip confirmation prompt |

## Examples

Basic run with defaults:
```
python3 split_recording.py rehearsal.wav
```

Force a split at a known boundary (e.g. where two songs run together with no silence between them):
```
python3 split_recording.py rehearsal.wav --split-at 21:39
```

Multiple forced splits, output WAV + MP3:
```
python3 split_recording.py rehearsal.wav --split-at 21:39 --split-at 1:02:15 --format both
```

## Tips

- **Too many splits** (catching pauses within songs): raise `--silence-duration` or lower `--silence-db` (e.g. `-35`)
- **Too few splits** (missing boundaries): lower `--silence-duration` or raise `--silence-db`
- **Short false starts still showing up**: raise `--min-segment`
- **Transition with no silence** (band went straight into talking): use `--split-at` with the known timestamp
- A brief pause (5+ seconds of quiet) between songs makes auto-detection much more reliable
