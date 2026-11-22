---
name: agent-daw
description: Technical reference for AgentDAW — an offline multi-track DAW engine and Kontakt 8 VST3 MCP server. Use when querying local Kontakt NKI/NKM/VST3 instruments, creating or editing JSON audio/MIDI projects, configuring tracks/buses/effects/clips, inspecting audio or MIDI files, or rendering 24-bit WAV outputs.
---

# AgentDAW Technical Reference

AgentDAW is an offline multi-track audio/MIDI workstation engine hosted via MCP (`agent-daw`) and CLI (`studio.cmd`). Audio synthesis runs through local Kontakt 8 VST3 worker processes; mixing, audio clip editing, DSP/VST3 effects, buses, automation, and 24-bit stereo WAV export run in the local Python/Pedalboard engine.

All MCP file paths (`project_path`, `output_path`, `output_project_path`, `audio_path`, `midi_path`, `output_midi_path`, `stems_dir`) **must be absolute paths**. Paths referenced *inside* a project JSON (such as `instrument`, `midi_file`, `clips[].file`, `impulse_response_filename`) may be absolute, relative to the project JSON directory, or (for instruments) library-relative.

## MCP Tools

| Tool | Purpose | Key Arguments |
| :--- | :--- | :--- |
| `studio_doctor` | Verify Kontakt 8 VST3 path, `drop.dll` bridge, preset cache, library roots, patch counts, and discovered VST3 plugins. | `{}` |
| `list_instruments` | Search local `.nki`, `.nkm`, `.vstpreset` patches. Whitespace-separated terms use AND matching across path and filename. | `query`, `kind` (`nki`/`nkm`/`vstpreset`), `limit`, `offset` |
| `project_schema` | Return the authoritative Draft-07 JSON Schema for project files. | `{}` |
| `save_project` | Validate and atomically write a full project JSON. | `project_path`, `project`, `overwrite` (default `false`) |
| `load_project` | Read a project JSON and structural summary. Set `summary_only: true` to inspect track metadata without loading full note arrays. | `project_path`, `summary_only` |
| `edit_project` | Apply surgical DAW edits to an existing project JSON, saving in place or to `output_project_path`. | `project_path`, `operations`, `output_project_path`, `overwrite` |
| `validate_project` | Validate schema, tempo map, bus routing, MIDI events, and file references without rendering. | `project_path` |
| `render_project` | Render full mix or `[start_beat, end_beat]` region to 24-bit stereo WAV with peak/RMS metering. | `project_path`, `output_path`, `fresh`, `start_beat`, `end_beat`, `only_tracks`, `export_stems`, `stems_dir` |
| `inspect_audio` | Inspect full file or `[start_seconds, end_seconds]` slice: sample rate, channels, duration, peak/RMS/crest dBFS, clipping count, active audible span, stereo balance/correlation/width, 6-band spectral energy, and loudness profile. | `audio_path`, `segment_seconds`, `max_segments`, `start_seconds`, `end_seconds` |
| `audition_audio` | Export a compact listening preview (`mp3` or 16-bit `wav`, full file or time slice) plus full objective acoustic/spectral/stereo diagnostics; pass returned `preview_path` to `read_file` to listen natively. | `audio_path`, `start_seconds`, `end_seconds`, `output_preview_path`, `format`, `sample_rate`, `segment_seconds` |
| `audition_project` | Render a project (or `[start_beat, end_beat]` region / `only_tracks` solo subset), compute objective acoustic/spectral/stereo diagnostics, and return a compact `preview_path` for native listening via `read_file`. | `project_path`, `output_path`, `start_beat`, `end_beat`, `only_tracks`, `fresh`, `format`, `sample_rate`, `segment_seconds` |
| `inspect_midi` | Inspect a `.mid` file's tracks, channels, tempo map, note counts, pitch ranges, and beat spans. | `midi_path` |
| `export_midi` | Export MIDI tracks and tempo map from a project JSON to a Type-1 `.mid` file. | `project_path`, `output_midi_path`, `ticks_per_beat` |
| `configure_studio` | Read or update `settings.json` (`libraries`, `plugin`, `vst3_dirs`). | `add_libraries`, `remove_libraries`, `set_plugin`, `add_vst3_dirs`, `remove_vst3_dirs` |

## Project JSON Specification (`version: 1`)

### Top-Level Fields
- `version` (required): integer `1`.
- `tracks` (required, non-empty array): instrument tracks or audio clip tracks.
- `bpm`: base tempo in quarter notes per minute (`1..1000`, default `120`).
- `tempo_changes`: array of `[beat, bpm]` or `[beat, bpm, "step" | "linear"]`. `"step"` switches tempo immediately at `beat`; `"linear"` ramps linearly in BPM from the previous point to `beat`.
- `sample_rate`: `44100`, `48000` (default), `88200`, or `96000`.
- `length_beats`: optional minimum timeline length in beats.
- `tail_seconds`: extra tail duration in seconds after the last event (`0..120`, default `2`).
- `master_gain_db`: master bus gain in dB (`-120..36`, default `0`).
- `ceiling_db`: master peak limiter ceiling in dBFS (`-60..0`, default `-1`). If peak exceeds ceiling, the mix is scaled uniformly so peak equals `ceiling_db` (`ceiling_gain_db` reports the attenuation).
- `buses`: optional array of send/return bus objects (`name`, `gain_db`, `pan`, `mute`, `effects`, `volume_automation`, `pan_automation`).
- `master_effects`: ordered effect chain processed on the stereo mix bus.

### Track Fields
Every track requires `name` and must be **either** an instrument track (`instrument` + at least one of `notes` or `midi_file`) **or** an audio clip track (`clips`).

Common track fields:
- `gain_db` (`-120..36`, default `0`), `pan` (`-1..1`, default `0`: `-1` hard left, `0` original stereo, `+1` hard right).
- `mute` (bool), `solo` (bool). If any track has `solo: true`, only soloed non-muted tracks render. Muted/inactive tracks do not spawn Kontakt.
- `effects`: ordered array of effect objects applied before the track fader.
- `sends`: array of `{"bus": "<bus_name>", "gain_db": <number>, "pre_fader": <bool>}`.
- `volume_automation`: array of `[beat, gain_db]` breakpoints (piecewise-linear per sample, added to `gain_db`).
- `pan_automation`: array of `[beat, pan]` breakpoints (`-1..1`, piecewise-linear per sample, overrides static `pan`).

Instrument track fields:
- `instrument`: exact patch path returned by `list_instruments` (`.nki`, `.nkm`, or `.vstpreset`).
- `channel`: MIDI channel `1..16` (default `1`). For `.nkm` multis, match the slot's MIDI channel.
- `notes`: array of `[start_beat, duration_beats, midi_pitch(0..127), velocity(1..127)]`. Beats are quarter notes.
- `cc`: array of `[beat, controller(0..127), value(0..127)]`.
- `pitch_bend`: array of `[beat, value(-8192..8191)]` (`0` = center).
- `program_change`: array of `[beat, program(0..127)]`.
- `channel_pressure`: array of `[beat, pressure(0..127)]`.
- `midi_file`: path to a Type 0/1 `.mid` file. Optional filters: `midi_track` (0-based track index), `midi_channel` (`1..16`), `midi_offset_beats` (timeline offset).
- `allow_silent`: boolean (default `false`). By default, a completely silent Kontakt track raises an error.

Audio clip track fields:
- `clips`: array of clip objects:
  - `file` (required): path to audio file (mono or stereo; resampled automatically to project `sample_rate`).
  - `at`: placement position on timeline in beats (default `0`).
  - `in_seconds`: source start offset in seconds (default `0`).
  - `length_seconds`: source segment duration in seconds (default: remainder of source file).
  - `fade_in`, `fade_out`: fade durations in seconds.
  - `fade_curve`: `"linear"` (default) or `"equal_power"`.
  - `gain_db`, `pan` (`-1..1`), `reverse` (bool).
  - `speed`: time-stretch speed multiplier (`0.25..4.0`; `2.0` = double speed / half duration, pitch preserved).
  - `pitch_semitones`: pitch shift in semitones (`-24..24`, duration preserved).

### Supported Effects (`effects`, `buses[].effects`, `master_effects`)
Query `project_schema` for exact parameter bounds. Supported `type` values:
- Filters / EQ: `highpass`, `lowpass`, `peak`, `low_shelf`, `high_shelf`, `ladder_filter` (`LPF12`/`LPF24`/`HPF12`/`HPF24`/`BPF12`/`BPF24`).
- Dynamics: `compressor`, `limiter`, `brickwall_limiter`, `noise_gate`.
- Time / Space: `reverb`, `delay`, `convolution` (`impulse_response_filename`, `mix`).
- Modulation / Color / Pitch / Utility: `chorus`, `phaser`, `distortion`, `clipping`, `bitcrush`, `pitch_shift`, `gain`, `invert`.
- External VST3 FX: `vst3` (`plugin`, optional `preset`, optional `parameters` dict).

## Surgical Editing (`edit_project`)

`operations` is an ordered list executed atomically and validated before writing:
- `{"op": "set_global", "fields": {...}}` (pass `null` for a field to remove it).
- `{"op": "add_track", "track": {...}, "index": <optional int>}`
- `{"op": "remove_track", "track": "<name>" | <index>}`
- `{"op": "duplicate_track", "track": "<name>" | <index>, "new_name": "<optional>"}`
- `{"op": "update_track", "track": "<name>" | <index>, "fields": {...}}` (pass `null` to delete an optional track field).
- `{"op": "edit_notes", "track": ..., "action": "replace_all" | "append" | "delete_range" | "replace_range" | "transpose" | "shift_time" | "scale_velocity" | "quantize", ...}`
  - Range filters: optional `start_beat` (inclusive) and `end_beat` (exclusive).
  - Action params: `notes` (`replace_all`/`append`/`replace_range`), `semitones` (`transpose`), `delta_beats` (`shift_time`), `factor` and `offset` (`scale_velocity`), `grid_beats` and optional `strength` (`quantize`).
- `{"op": "edit_cc", "track": ..., "action": "replace_all" | "append" | "delete_range", "cc": [...], "start_beat": ..., "end_beat": ..., "controller": ...}`
- `{"op": "edit_clips", "track": ..., "action": "replace_all" | "append" | "remove_index" | "update_index", "clips": [...], "index": ..., "fields": {...}}`

## Operational Workflow & Diagnostics

1. Call `studio_doctor` once if environment status is unknown.
2. Call `list_instruments` to retrieve exact `.nki`/`.nkm`/`.vstpreset` paths — never guess filenames.
3. Write the project via `save_project` (or modify an existing one via `edit_project`), verify with `validate_project` if needed, and render with `render_project`.
4. First load of an uncached `.nki`/`.nkm` briefly opens a Kontakt editor window on the Windows desktop to inject the file via OLE drop and caches the resulting `.vstpreset`. Subsequent renders restore from cache headlessly. Pass `fresh: true` to reload from disk and refresh the cache.
5. Inspect `render_project` telemetry (`seconds`, `peak_dbfs`, `rms_dbfs`, `ceiling_gain_db`, and each track's `instrument_peak_dbfs`, `mixed_peak_dbfs`, `rms_dbfs`). If a track errors as silent, check its MIDI channel, instrument playable key range, required CC controllers, or library sample paths.
6. **Self-listening & iterative refinement loop**: Call `audition_project` (for a full project, a `[start_beat, end_beat]` region, or `only_tracks` solo subset) or `audition_audio` (for an existing WAV/audio file or `[start_seconds, end_seconds]` slice), then call `read_file` on the returned `preview_path` to listen natively to the rendered audio. Combine what you hear directly with the objective acoustic telemetry (`peak_dbfs`, `rms_dbfs`, `crest_factor_db`, `clipped_samples`, `active_span_seconds`, `stereo`, `spectrum_bands`, `segments`), apply targeted edits with `edit_project`, and re-render/re-audition to verify.
7. For custom multi-instrument racks, capture a preset interactively via CLI: `studio.cmd capture "<path>.vstpreset"`.
