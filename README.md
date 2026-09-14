# split-long-recording

Splits a long WAV recording (e.g. a band rehearsal or a live set) into individual songs by detecting the gaps between them. Outputs MP3s by default.

Two detection methods are available:

- `--detect silence` (default) — ffmpeg's `silencedetect`. Fine for a quiet rehearsal room.
- `--detect envelope` — measures an RMS envelope and tolerates brief transients. **Use this for live recordings.** A single stick click or cough in an otherwise silent gap splits that gap in two as far as `silencedetect` is concerned, and neither half is then long enough to count as a boundary — so real song breaks get missed no matter how you tune `--silence-duration`. The envelope method bridges those blips.

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
| `--detect` | `silence` | Boundary detection: `silence` or `envelope` |
| `--envelope-db` | `-38` | RMS level below which a window counts as quiet (`--detect envelope`) |
| `--envelope-bridge` | `2.0` | Loud blips shorter than this (seconds) don't break a gap (`--detect envelope`) |
| `--envelope-min-gap` | `5.0` | Minimum sustained quiet (seconds) to count as a boundary (`--detect envelope`) |
| `--silence-db` | `-40` | Noise floor threshold in dB |
| `--silence-duration` | `7.0` | Minimum silence length (seconds) to treat as a song boundary |
| `--min-segment` | `120.0` | Segments shorter than this (seconds) are merged into the next track — helps discard false starts |
| `--drop-short` | — | Drop segments shorter than `--min-segment` instead of merging them into a neighbour — discards between-song banter and tuning |
| `--drop-quiet-db` | — | Drop *any* segment whose mean volume stays below this dBFS threshold — catches talk that ran long enough to survive the length filter |
| `--drop-leading-quiet-db` | — | Drop leading merged segments whose mean volume stays below this dBFS threshold — useful for long dead-air/setup intros before practice really starts |
| `--pad-start` | `0` | Seconds of lead-in kept before each segment, so onsets aren't clipped |
| `--pad-end` | `0` | Seconds of tail kept after each segment, for ring-outs and decay |
| `--split-at TIME` | — | Force a split at a specific timestamp (`MM:SS`, `HH:MM:SS`, or seconds). Can be repeated. |
| `--format` | `mp3` | Output format: `wav`, `mp3`, or `both` |
| `--normalize` | — | Apply one-pass loudness normalization during export |
| `--normalize-lufs` | `-16.0` | Integrated loudness target for `--normalize` |
| `--normalize-lra` | `11.0` | Loudness range target for `--normalize` |
| `--normalize-true-peak` | `-1.5` | True peak ceiling for `--normalize` in dBTP |
| `--vocal-eq` | — | Apply a mild EQ curve to bring vocals forward during export |
| `--vocal-eq-highpass` | `120.0` | High-pass cutoff for `--vocal-eq` in Hz |
| `--vocal-eq-presence-freq` | `2500.0` | Presence boost center for `--vocal-eq` in Hz |
| `--vocal-eq-presence-gain` | `3.0` | Presence boost gain for `--vocal-eq` in dB |
| `--vocal-eq-clarity-freq` | `4500.0` | Clarity boost center for `--vocal-eq` in Hz |
| `--vocal-eq-clarity-gain` | `2.0` | Clarity boost gain for `--vocal-eq` in dB |
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

Add normalization and a light vocal-presence EQ in the same ffmpeg pass:
```
python3 split_recording.py rehearsal.wav --normalize --vocal-eq

Live set with an audience — envelope detection, banter discarded, padded so nothing is clipped, normalized to a consistent level:
```
python3 split_recording.py liveset.wav --detect envelope --min-segment 60 --drop-short \
    --drop-quiet-db -32 --pad-start 1 --pad-end 2 --format wav --normalize
```

Typical practice with dead air at the start:
```
python3 split_recording.py rehearsal.wav --silence-db -22 --silence-duration 15 --min-segment 360 --drop-leading-quiet-db -31
```
```

Push vocals a bit harder and aim for a slightly louder result:
```
python3 split_recording.py rehearsal.wav --normalize --normalize-lufs -14 --vocal-eq --vocal-eq-presence-gain 4.5 --vocal-eq-clarity-gain 3
```

## Tips

- **Too many splits** (catching pauses within songs): raise `--silence-duration` or lower `--silence-db` (e.g. `-35`)
- **Too few splits** (missing boundaries): lower `--silence-duration` or raise `--silence-db`
- **Short false starts still showing up**: raise `--min-segment`
- **Long dead-air/setup intro still being kept**: try `--drop-leading-quiet-db` around `-31` to `-33`
- **Transition with no silence** (band went straight into talking): use `--split-at` with the known timestamp
- `--normalize` uses ffmpeg's one-pass `loudnorm`; `--normalize-lufs`, `--normalize-lra`, and `--normalize-true-peak` let you tune the target
- `--vocal-eq` adds a high-pass filter plus gentle presence boosts; the `--vocal-eq-*` flags let you tune the cutoff, boost centers, and gains
- A brief pause (5+ seconds of quiet) between songs makes auto-detection much more reliable
- **Live recording, boundaries missed at every setting**: switch to `--detect envelope`. Applause, stray clicks and chatter defeat `silencedetect`'s all-or-nothing threshold
- **Banter showing up as its own track**: add `--drop-short` (songs are minutes long, banter is seconds) and set `--min-segment` between the two — check the proposed table before confirming
- **Banter merged onto the front of a song**: that's `--min-segment` merging rather than dropping; `--drop-short` changes that
- With `--drop-short`, `--min-segment` must sit *below* your shortest real song or that song gets discarded — the confirmation table shows exactly what would be dropped
