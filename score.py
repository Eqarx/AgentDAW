"""Project grammar, tempo mapping, instrument discovery, and timeline compilation."""

import json
import math
import os
from pathlib import Path

import fastjsonschema
import mido
from pedalboard.io import AudioFile

ROOT = Path(__file__).resolve().parent


def obj(fields, required=()):
    return {
        "type": "object",
        "properties": fields,
        "required": list(required),
        "additionalProperties": False,
    }


def number(low, high):
    return {"type": "number", "minimum": low, "maximum": high}


def integer(low, high):
    return dict(number(low, high), type="integer")


def array(item):
    return {"type": "array", "items": item}


def row(*fields):
    return {
        "type": "array",
        "items": list(fields),
        "minItems": len(fields),
        "maxItems": len(fields),
    }


TEXT = {"type": "string", "minLength": 1}
TIME = number(0, 1000000)
MIDI = integer(0, 127)
GAIN = number(-120, 36)
BOOL = {"type": "boolean"}

FX = {
    "reverb": {
        "room_size": number(0, 1),
        "wet_level": number(0, 1),
        "dry_level": number(0, 1),
        "damping": number(0, 1),
        "width": number(0, 1),
        "freeze_mode": number(0, 1),
    },
    "delay": {
        "delay_seconds": number(0.001, 30),
        "feedback": number(0, 0.99),
        "mix": number(0, 1),
    },
    "lowpass": {"cutoff_frequency_hz": number(10, 22000)},
    "highpass": {"cutoff_frequency_hz": number(10, 22000)},
    "peak": {
        "cutoff_frequency_hz": number(10, 22000),
        "gain_db": number(-48, 48),
        "q": number(0.05, 50),
    },
    "low_shelf": {
        "cutoff_frequency_hz": number(10, 22000),
        "gain_db": number(-48, 48),
        "q": number(0.05, 50),
    },
    "high_shelf": {
        "cutoff_frequency_hz": number(10, 22000),
        "gain_db": number(-48, 48),
        "q": number(0.05, 50),
    },
    "ladder_filter": {
        "mode": {"enum": ["LPF12", "LPF24", "HPF12", "HPF24", "BPF12", "BPF24"]},
        "cutoff_hz": number(10, 22000),
        "resonance": number(0, 1),
        "drive": number(1, 50),
    },
    "compressor": {
        "threshold_db": number(-100, 0),
        "ratio": number(1, 100),
        "attack_ms": number(0.1, 1000),
        "release_ms": number(1, 5000),
    },
    "limiter": {
        "threshold_db": number(-60, 0),
        "release_ms": number(1, 5000),
    },
    "brickwall_limiter": {
        "ceiling_db": number(-60, 0),
        "release_ms": number(1, 5000),
        "lookahead_ms": number(0, 50),
        "true_peak": BOOL,
    },
    "noise_gate": {
        "threshold_db": number(-120, 0),
        "ratio": number(1, 100),
        "attack_ms": number(0.1, 1000),
        "release_ms": number(1, 5000),
    },
    "chorus": {
        "rate_hz": number(0, 100),
        "depth": number(0, 1),
        "centre_delay_ms": number(0.1, 100),
        "feedback": number(-0.99, 0.99),
        "mix": number(0, 1),
    },
    "phaser": {
        "rate_hz": number(0, 100),
        "depth": number(0, 1),
        "centre_frequency_hz": number(20, 20000),
        "feedback": number(-0.99, 0.99),
        "mix": number(0, 1),
    },
    "distortion": {"drive_db": number(0, 100)},
    "clipping": {"threshold_db": number(-60, 0)},
    "bitcrush": {"bit_depth": number(1, 32)},
    "pitch_shift": {"semitones": number(-24, 24)},
    "gain": {"gain_db": GAIN},
    "invert": {},
    "convolution": {
        "impulse_response_filename": TEXT,
        "mix": number(0, 1),
    },
    "vst3": {
        "plugin": TEXT,
        "preset": TEXT,
        "parameters": {"type": "object"},
    },
}

FX_REQUIRED = {
    "convolution": ("type", "impulse_response_filename"),
    "vst3": ("type", "plugin"),
}

FX_CHAIN = array(
    {
        "oneOf": [
            obj({"type": {"const": name}, **params}, FX_REQUIRED.get(name, ("type",)))
            for name, params in FX.items()
        ]
    }
)

TEMPO_POINT = {
    "oneOf": [
        row(TIME, number(1, 1000)),
        row(TIME, number(1, 1000), {"enum": ["step", "linear"]}),
    ]
}

AUTOMATION_DB = array(row(TIME, GAIN))
AUTOMATION_PAN = array(row(TIME, number(-1, 1)))

SEND = obj(
    {
        "bus": TEXT,
        "gain_db": GAIN,
        "pre_fader": BOOL,
    },
    ("bus",),
)

BUS = obj(
    {
        "name": TEXT,
        "gain_db": GAIN,
        "pan": number(-1, 1),
        "mute": BOOL,
        "effects": FX_CHAIN,
        "volume_automation": AUTOMATION_DB,
        "pan_automation": AUTOMATION_PAN,
    },
    ("name",),
)

CLIP = obj(
    {
        "file": TEXT,
        "at": TIME,
        "in_seconds": TIME,
        "length_seconds": number(0.00001, 1000000),
        "fade_in": TIME,
        "fade_out": TIME,
        "fade_curve": {"enum": ["linear", "equal_power"]},
        "gain_db": GAIN,
        "pan": number(-1, 1),
        "reverse": BOOL,
        "speed": number(0.25, 4.0),
        "pitch_semitones": number(-24, 24),
    },
    ("file",),
)

TRACK = obj(
    {
        "name": TEXT,
        "instrument": TEXT,
        "channel": integer(1, 16),
        "notes": array(row(TIME, number(0.00001, 1000000), MIDI, integer(1, 127))),
        "cc": array(row(TIME, MIDI, MIDI)),
        "pitch_bend": array(row(TIME, integer(-8192, 8191))),
        "program_change": array(row(TIME, MIDI)),
        "channel_pressure": array(row(TIME, MIDI)),
        "midi_file": TEXT,
        "midi_track": integer(0, 1024),
        "midi_channel": integer(1, 16),
        "midi_offset_beats": TIME,
        "clips": array(CLIP),
        "gain_db": GAIN,
        "pan": number(-1, 1),
        "mute": BOOL,
        "solo": BOOL,
        "allow_silent": BOOL,
        "effects": FX_CHAIN,
        "sends": array(SEND),
        "volume_automation": AUTOMATION_DB,
        "pan_automation": AUTOMATION_PAN,
    },
    ("name",),
)

TRACK["oneOf"] = [
    {
        "required": ["instrument"],
        "not": {"required": ["clips"]},
        "anyOf": [{"required": ["notes"]}, {"required": ["midi_file"]}],
    },
    {
        "required": ["clips"],
        "not": {
            "anyOf": [
                {"required": [k]}
                for k in (
                    "instrument",
                    "notes",
                    "cc",
                    "pitch_bend",
                    "program_change",
                    "channel_pressure",
                    "channel",
                    "midi_file",
                    "midi_track",
                    "midi_channel",
                    "midi_offset_beats",
                )
            ]
        },
    },
]

SCHEMA = dict(
    obj(
        {
            "version": {"type": "integer", "const": 1},
            "bpm": number(1, 1000),
            "tempo_changes": array(TEMPO_POINT),
            "sample_rate": {"enum": [44100, 48000, 88200, 96000]},
            "tail_seconds": number(0, 120),
            "length_beats": number(0.00001, 1000000),
            "master_gain_db": GAIN,
            "ceiling_db": number(-60, 0),
            "master_effects": FX_CHAIN,
            "buses": array(BUS),
            "tracks": dict(array(TRACK), minItems=1),
        },
        ("version", "tracks"),
    )
)
SCHEMA["$schema"] = "http://json-schema.org/draft-07/schema#"
validate_shape = fastjsonschema.compile(SCHEMA)


class TempoMap:
    """Maps musical beats to seconds with support for step and linear tempo changes."""

    def __init__(self, base_bpm=120.0, changes=None):
        points = [(0.0, float(base_bpm), "step")]
        for item in changes or []:
            beat = float(item[0])
            bpm = float(item[1])
            curve = item[2] if len(item) > 2 else "step"
            points.append((beat, bpm, curve))
        # Deduplicate same beat by keeping the last entry; sort by beat.
        by_beat = {}
        for beat, bpm, curve in points:
            by_beat[beat] = (beat, bpm, curve)
        ordered = [by_beat[b] for b in sorted(by_beat)]
        self.segments = []
        sec = 0.0
        for idx, (b0, t0, _curve) in enumerate(ordered):
            if idx + 1 < len(ordered):
                b1, t1, next_curve = ordered[idx + 1]
                ramp = next_curve == "linear"
                dt = self._segment_duration(b1 - b0, t0, t1 if ramp else t0)
                self.segments.append((b0, b1, sec, t0, t1 if ramp else t0))
                sec += dt
            else:
                self.segments.append((b0, math.inf, sec, t0, t0))

    @staticmethod
    def _segment_duration(db, t0, t1):
        if db <= 0:
            return 0.0
        if abs(t1 - t0) < 1e-9:
            return db * 60.0 / t0
        return 60.0 * db / (t1 - t0) * math.log(t1 / t0)

    def beat_to_seconds(self, beat):
        b = float(beat)
        if b <= 0:
            return 0.0
        for b0, b1, s0, t0, t1 in self.segments:
            if b <= b1:
                db = b - b0
                if abs(t1 - t0) < 1e-9 or not math.isfinite(b1):
                    return s0 + db * 60.0 / t0
                frac = db / (b1 - b0)
                tb = t0 + (t1 - t0) * frac
                return s0 + 60.0 * (b1 - b0) / (t1 - t0) * math.log(tb / t0)
        b0, _b1, s0, t0, _t1 = self.segments[-1]
        return s0 + (b - b0) * 60.0 / t0


def read_json(path):
    def invalid(value):
        raise ValueError(f"Non-finite number: {value}")

    return json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_constant=invalid)


def settings_path():
    override = os.environ.get("KONTAKT_STUDIO_SETTINGS")
    return Path(override).expanduser().resolve() if override else (ROOT / "settings.json")


def settings():
    config = settings_path()
    return read_json(config) if config.exists() else {}


def libraries():
    override = os.environ.get("KONTAKT_LIBRARY_ROOTS")
    return [
        Path(p).expanduser().resolve()
        for p in (override.split(os.pathsep) if override else settings().get("libraries", []))
        if p
    ]


def vst3_directories():
    dirs = []
    common = Path(os.environ.get("CommonProgramFiles", "C:/Program Files/Common Files")) / "VST3"
    if common.is_dir():
        dirs.append(common.resolve())
    for extra in settings().get("vst3_dirs", []):
        p = Path(extra).expanduser().resolve()
        if p.is_dir() and p not in dirs:
            dirs.append(p)
    return dirs


def list_vst3_plugins():
    found = []
    seen = set()
    for base in vst3_directories():
        for p in sorted(base.rglob("*.vst3")):
            # Skip internal x86_64-win binary inside a .vst3 bundle directory
            if any(parent.suffix.lower() == ".vst3" for parent in p.parents):
                continue
            rp = str(p.resolve())
            if rp.casefold() not in seen:
                seen.add(rp.casefold())
                found.append({"name": p.stem, "path": rp})
    return found


def plugin_path():
    configured = os.environ.get("KONTAKT_VST3") or settings().get("plugin")
    if configured:
        path = Path(configured)
        if not path.exists():
            raise ValueError(f"Kontakt VST3 not found: {path}")
        return path.resolve()
    base = Path(os.environ.get("CommonProgramFiles", "C:/Program Files/Common Files")) / "VST3"
    for mask in ("Kontakt 8.vst3", "*Kontakt*/*Kontakt*8*.vst3", "Kontakt*8*.vst3"):
        for path in sorted(base.glob(mask)):
            return path.resolve()
    raise ValueError("Set KONTAKT_VST3 or the plugin path in settings.json.")


def catalog(query="", kind="", limit=None, offset=0):
    tokens = [tok.casefold() for tok in (query or "").split() if tok]
    kind_filter = (kind or "").strip().lower().lstrip(".")
    found = []
    for root in libraries():
        if not root.is_dir():
            raise ValueError(f"Library directory missing: {root}")
        for p in root.rglob("*"):
            ext = p.suffix.lower()
            if ext in (".nki", ".nkm", ".vstpreset") and p.is_file():
                item_kind = ext[1:]
                if kind_filter and item_kind != kind_filter:
                    continue
                name = p.relative_to(root).as_posix()
                haystack = (root.name + "/" + name).casefold()
                if all(tok in haystack for tok in tokens):
                    found.append({"instrument": name, "path": str(p), "kind": item_kind})
    found.sort(key=lambda x: x["path"].casefold())
    if offset and offset > 0:
        found = found[offset:]
    if limit is not None and limit >= 0:
        found = found[:limit]
    return found


def source(name, base, instrument=False):
    p = Path(name)
    if p.is_absolute():
        choices = [p]
    else:
        local = base / p
        choices = (
            [local]
            if local.is_file()
            else [root / p for root in libraries()]
            if instrument
            else []
        )
    hits = {p.resolve() for p in choices if p.is_file()}
    if len(hits) != 1:
        raise ValueError(f"Expected one existing source for {name!r}; found {len(hits)}.")
    path = hits.pop()
    if instrument and path.suffix.lower() not in (".nki", ".nkm", ".vstpreset"):
        raise ValueError(f"Use an NKI, NKM or VST3 preset: {path}")
    return str(path)


def _resolve_fx_paths(fx_list, base):
    resolved = []
    for spec in fx_list or []:
        item = dict(spec)
        if item["type"] == "convolution":
            item["impulse_response_filename"] = source(item["impulse_response_filename"], base)
        elif item["type"] == "vst3":
            plugin_candidate = Path(item["plugin"])
            if not plugin_candidate.is_absolute():
                local = base / plugin_candidate
                if local.exists():
                    plugin_candidate = local
                else:
                    for vdir in vst3_directories():
                        cand = vdir / plugin_candidate
                        if cand.exists():
                            plugin_candidate = cand
                            break
            if not plugin_candidate.exists():
                raise ValueError(f"VST3 effect plugin not found: {item['plugin']!r}")
            item["plugin"] = str(plugin_candidate.resolve())
            if "preset" in item:
                item["preset"] = source(item["preset"], base)
        resolved.append(item)
    return resolved


def _event_priority(msg):
    status = msg[0] & 0xF0
    if status == 0x80 or (status == 0x90 and msg[2] == 0):
        return 0
    if status == 0xC0:
        return 1
    if status == 0x90:
        return 3
    return 2


def _import_midi_events(midi_path, track_index=None, channel_filter=None, offset_sec=0.0):
    mf = mido.MidiFile(midi_path)
    out = []
    if track_index is not None:
        if track_index < 0 or track_index >= len(mf.tracks):
            raise ValueError(
                f"midi_track {track_index} out of range for {midi_path} (tracks: {len(mf.tracks)})."
            )
        # Merge tempo meta messages from all tracks with channel messages from target track.
        tempo_events = []
        for trk in mf.tracks:
            abs_tick = 0
            for msg in trk:
                abs_tick += msg.time
                if msg.is_meta and msg.type == "set_tempo":
                    tempo_events.append((abs_tick, msg.tempo))
        tempo_events.sort(key=lambda x: x[0])

        def tick_to_sec(target_tick):
            cur_tick = 0
            cur_tempo = 500000
            sec = 0.0
            for t_tick, t_val in tempo_events:
                if t_tick >= target_tick:
                    break
                sec += mido.tick2second(t_tick - cur_tick, mf.ticks_per_beat, cur_tempo)
                cur_tick = t_tick
                cur_tempo = t_val
            sec += mido.tick2second(target_tick - cur_tick, mf.ticks_per_beat, cur_tempo)
            return sec

        abs_tick = 0
        for msg in mf.tracks[track_index]:
            abs_tick += msg.time
            if not msg.is_meta and hasattr(msg, "channel"):
                if channel_filter is not None and msg.channel != channel_filter - 1:
                    continue
                out.append((offset_sec + tick_to_sec(abs_tick), msg.bytes()))
    else:
        seconds = 0.0
        for msg in mf:
            seconds += msg.time
            if not msg.is_meta and hasattr(msg, "channel"):
                if channel_filter is not None and msg.channel != channel_filter - 1:
                    continue
                out.append((offset_sec + seconds, msg.bytes()))
    return out


def _compile_automation(points, tempo_map, sr, region_start_sec):
    if not points:
        return None
    compiled = sorted(
        [(tempo_map.beat_to_seconds(b) - region_start_sec, float(v)) for b, v in points],
        key=lambda x: x[0],
    )
    return [(round(sec * sr), val) for sec, val in compiled]


def compile_score(path, start_beat=None, end_beat=None, only_tracks=None):
    path = Path(path).resolve()
    project = read_json(path)
    validate_shape(project)
    sr = project.get("sample_rate", 48000)
    tempo_map = TempoMap(project.get("bpm", 120), project.get("tempo_changes"))

    if start_beat is not None and start_beat < 0:
        raise ValueError("start_beat must be >= 0.")
    if end_beat is not None and end_beat <= (start_beat or 0):
        raise ValueError("end_beat must be greater than start_beat.")

    region_start_sec = tempo_map.beat_to_seconds(start_beat) if start_beat is not None else 0.0
    region_end_sec = tempo_map.beat_to_seconds(end_beat) if end_beat is not None else None

    # Validate buses
    buses = []
    bus_names = set()
    for bspec in project.get("buses", []):
        bname = bspec["name"]
        if bname in bus_names:
            raise ValueError(f"Duplicate bus name: {bname!r}")
        bus_names.add(bname)
        b = bspec.copy()
        b["effects"] = _resolve_fx_paths(b.get("effects", []), path.parent)
        b["_vol_auto"] = _compile_automation(
            b.get("volume_automation"), tempo_map, sr, region_start_sec
        )
        b["_pan_auto"] = _compile_automation(
            b.get("pan_automation"), tempo_map, sr, region_start_sec
        )
        buses.append(b)

    # Validate track names and solo/mute state
    track_names = [t["name"] for t in project["tracks"]]
    if only_tracks is not None:
        missing = [n for n in only_tracks if n not in track_names]
        if missing:
            raise ValueError(f"Unknown track name(s) in only_tracks: {missing}")
        only_set = set(only_tracks)
        has_solo = False
    else:
        only_set = None
        has_solo = any(t.get("solo", False) for t in project["tracks"])

    end_frame = 0
    tracks = []
    for spec in project["tracks"]:
        tr = spec.copy()
        if only_set is not None:
            active = tr["name"] in only_set
        elif has_solo:
            active = bool(tr.get("solo", False)) and not bool(tr.get("mute", False))
        else:
            active = not bool(tr.get("mute", False))
        tr["_active"] = active

        for snd in tr.get("sends", []):
            if snd["bus"] not in bus_names:
                raise ValueError(f"{tr['name']}: send references unknown bus {snd['bus']!r}.")

        tr["effects"] = _resolve_fx_paths(tr.get("effects", []), path.parent)
        tr["_vol_auto"] = _compile_automation(
            tr.get("volume_automation"), tempo_map, sr, region_start_sec
        )
        tr["_pan_auto"] = _compile_automation(
            tr.get("pan_automation"), tempo_map, sr, region_start_sec
        )

        if "instrument" in tr:
            tr["instrument"] = source(tr["instrument"], path.parent, True)
            channel = tr.get("channel", 1) - 1
            raw_events = []  # (sec, bytes_list)

            for t, prog in tr.get("program_change", []):
                raw_events.append((tempo_map.beat_to_seconds(t), [192 + channel, int(prog)]))
            for t, cc, val in tr.get("cc", []):
                raw_events.append((tempo_map.beat_to_seconds(t), [176 + channel, int(cc), int(val)]))
            for t, bend in tr.get("pitch_bend", []):
                v = int(bend) + 8192
                raw_events.append(
                    (tempo_map.beat_to_seconds(t), [224 + channel, v & 0x7F, (v >> 7) & 0x7F])
                )
            for t, pressure in tr.get("channel_pressure", []):
                raw_events.append((tempo_map.beat_to_seconds(t), [208 + channel, int(pressure)]))
            for t, length, note, velocity in tr.get("notes", []):
                on_sec = tempo_map.beat_to_seconds(t)
                off_sec = max(on_sec + 1.0 / sr, tempo_map.beat_to_seconds(t + length))
                raw_events.append((on_sec, [144 + channel, int(note), int(velocity)]))
                raw_events.append((off_sec, [128 + channel, int(note), 0]))

            if tr.get("midi_file"):
                midi_abs = source(tr["midi_file"], path.parent)
                offset_sec = tempo_map.beat_to_seconds(tr.get("midi_offset_beats", 0))
                raw_events.extend(
                    _import_midi_events(
                        midi_abs,
                        track_index=tr.get("midi_track"),
                        channel_filter=tr.get("midi_channel"),
                        offset_sec=offset_sec,
                    )
                )

            if not any(m[0] >> 4 == 9 and m[2] > 0 for _s, m in raw_events):
                raise ValueError(f"{tr['name']}: no MIDI note-on events.")

            # Apply optional region window while preserving prior controller/program state.
            events = []
            if region_start_sec > 0 or region_end_sec is not None:
                raw_events.sort(key=lambda x: (x[0], _event_priority(x[1])))
                latest_state = {}
                active_notes = {}  # (ch, pitch) -> on_sec
                for sec, msg in raw_events:
                    status = msg[0] & 0xF0
                    ch = msg[0] & 0x0F
                    if sec < region_start_sec:
                        if status == 0xB0:
                            latest_state[("cc", ch, msg[1])] = msg
                        elif status == 0xC0:
                            latest_state[("pc", ch)] = msg
                        elif status == 0xE0:
                            latest_state[("pb", ch)] = msg
                        elif status == 0xD0:
                            latest_state[("cp", ch)] = msg
                        elif status == 0x90 and msg[2] > 0:
                            active_notes[(ch, msg[1])] = sec
                        elif status == 0x80 or (status == 0x90 and msg[2] == 0):
                            active_notes.pop((ch, msg[1]), None)
                        continue
                    if region_end_sec is not None and sec > region_end_sec:
                        if status == 0x80 or (status == 0x90 and msg[2] == 0):
                            if (ch, msg[1]) in active_notes:
                                events.append(
                                    [
                                        round((region_end_sec - region_start_sec) * sr),
                                        [0x80 + ch, msg[1], 0],
                                    ]
                                )
                                active_notes.pop((ch, msg[1]), None)
                        continue
                    rel_frame = max(0, round((sec - region_start_sec) * sr))
                    if status == 0x90 and msg[2] > 0:
                        active_notes[(ch, msg[1])] = sec
                    elif status == 0x80 or (status == 0x90 and msg[2] == 0):
                        active_notes.pop((ch, msg[1]), None)
                    events.append([rel_frame, list(msg)])
                for msg in latest_state.values():
                    events.append([0, list(msg)])
                # Close any lingering notes at region end
                if region_end_sec is not None:
                    end_f = max(1, round((region_end_sec - region_start_sec) * sr))
                    for ch, pitch in list(active_notes.keys()):
                        events.append([end_f, [0x80 + ch, pitch, 0]])
            else:
                for sec, msg in raw_events:
                    events.append([round(sec * sr), list(msg)])

            tr["events"] = sorted(events, key=lambda e: (e[0], _event_priority(e[1])))
            if tr["events"]:
                end_frame = max(end_frame, tr["events"][-1][0] + 1)
            elif region_start_sec > 0 or region_end_sec is not None:
                # Track has no notes inside this slice; mark inactive for this region render
                tr["_active"] = False
        else:
            clips = []
            for spec_clip in tr["clips"]:
                c = spec_clip.copy()
                c["file"] = source(c["file"], path.parent)
                speed = float(c.get("speed", 1.0))
                with AudioFile(c["file"]) as af:
                    if af.num_channels not in (1, 2):
                        raise ValueError("Audio clips must be mono or stereo.")
                    start = round(c.get("in_seconds", 0) * af.samplerate)
                    size = (
                        round(c["length_seconds"] * af.samplerate)
                        if "length_seconds" in c
                        else af.frames - start
                    )
                    if size <= 0 or start + size > af.frames:
                        raise ValueError(f"Clip extends outside {c['file']}.")
                    clip_at_sec = tempo_map.beat_to_seconds(c.get("at", 0))
                    out_frames = max(1, round((size / speed) * sr / af.samplerate))
                    c.update(
                        start=start,
                        size=size,
                        source_sr=af.samplerate,
                        at_frame=round((clip_at_sec - region_start_sec) * sr),
                        frames=out_frames,
                    )
                if c.get("fade_in", 0) + c.get("fade_out", 0) > c["frames"] / sr + 1 / sr:
                    raise ValueError("The two fades exceed the clip length.")
                if region_end_sec is not None:
                    region_frames = round((region_end_sec - region_start_sec) * sr)
                    if c["at_frame"] >= region_frames or c["at_frame"] + c["frames"] <= 0:
                        continue
                elif c["at_frame"] + c["frames"] <= 0:
                    continue
                end_frame = max(end_frame, c["at_frame"] + c["frames"])
                clips.append(c)
            tr["clips"] = clips
        tracks.append(tr)

    if region_end_sec is not None:
        end_frame = max(1, round((region_end_sec - region_start_sec) * sr))
    elif "length_beats" in project:
        len_sec = tempo_map.beat_to_seconds(project["length_beats"]) - region_start_sec
        if len_sec > 0:
            end_frame = max(end_frame, round(len_sec * sr))

    master_effects = _resolve_fx_paths(project.get("master_effects", []), path.parent)
    frames = end_frame + round(project.get("tail_seconds", 2) * sr)
    if end_frame == 0 or not math.isfinite(frames):
        raise ValueError("The timeline is empty.")
    return {
        "project": project,
        "rate": sr,
        "frames": frames,
        "tracks": tracks,
        "buses": buses,
        "master_effects": master_effects,
    }
