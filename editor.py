"""Project inspection, surgical editing, audio/MIDI analysis, MIDI export, and configuration."""

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

import mido
import numpy as np
from pedalboard.io import AudioFile

from score import (
    TempoMap,
    compile_score,
    libraries,
    plugin_path,
    read_json,
    settings,
    settings_path,
    validate_shape,
)


def _dbfs(val):
    return round(20 * math.log10(val), 3) if val > 1e-12 else None


def _atomic_json(path, payload):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", dir=path.parent, delete=False
    ) as f:
        tmp = Path(f.name)
        json.dump(payload, f, indent=2, ensure_ascii=False, allow_nan=False)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def summarize_project(project_path):
    path = Path(project_path).resolve()
    raw = read_json(path)
    validate_shape(raw)
    tempo_map = TempoMap(raw.get("bpm", 120), raw.get("tempo_changes"))
    tracks_summary = []
    max_beat = raw.get("length_beats", 0.0)
    for idx, tr in enumerate(raw["tracks"]):
        item = {
            "index": idx,
            "name": tr["name"],
            "type": "instrument" if "instrument" in tr else "audio",
            "mute": bool(tr.get("mute", False)),
            "solo": bool(tr.get("solo", False)),
            "gain_db": tr.get("gain_db", 0),
            "pan": tr.get("pan", 0),
            "effects_count": len(tr.get("effects", [])),
            "sends": [s["bus"] for s in tr.get("sends", [])],
        }
        if "instrument" in tr:
            notes = tr.get("notes", [])
            item["instrument"] = tr["instrument"]
            item["channel"] = tr.get("channel", 1)
            item["note_count"] = len(notes)
            item["cc_count"] = len(tr.get("cc", []))
            if "midi_file" in tr:
                item["midi_file"] = tr["midi_file"]
            if notes:
                pitches = [n[2] for n in notes]
                end_b = max(n[0] + n[1] for n in notes)
                max_beat = max(max_beat, end_b)
                item["pitch_range"] = [min(pitches), max(pitches)]
                item["beat_range"] = [min(n[0] for n in notes), end_b]
        else:
            clips = tr.get("clips", [])
            item["clip_count"] = len(clips)
            item["clip_files"] = [c["file"] for c in clips]
        tracks_summary.append(item)
    return {
        "project": str(path),
        "version": raw["version"],
        "bpm": raw.get("bpm", 120),
        "tempo_changes": raw.get("tempo_changes", []),
        "sample_rate": raw.get("sample_rate", 48000),
        "tail_seconds": raw.get("tail_seconds", 2),
        "estimated_seconds": round(
            tempo_map.beat_to_seconds(max_beat) + raw.get("tail_seconds", 2), 3
        ),
        "buses": [b["name"] for b in raw.get("buses", [])],
        "master_effects_count": len(raw.get("master_effects", [])),
        "tracks": tracks_summary,
    }


def load_project(project_path, summary_only=False):
    path = Path(project_path).resolve()
    if summary_only:
        return summarize_project(path)
    data = read_json(path)
    return {"project_path": str(path), "summary": summarize_project(path), "project": data}


def _find_track_index(tracks, selector):
    if isinstance(selector, int):
        if 0 <= selector < len(tracks):
            return selector
        raise ValueError(f"Track index {selector} out of range (0..{len(tracks) - 1}).")
    if isinstance(selector, str):
        matches = [i for i, t in enumerate(tracks) if t["name"] == selector]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"Multiple tracks named {selector!r}; use integer track index.")
        raise ValueError(f"Track not found: {selector!r}")
    raise ValueError("Track selector must be a track name (string) or integer index.")


def _in_range(beat, start_beat, end_beat):
    if start_beat is not None and beat < start_beat:
        return False
    if end_beat is not None and beat >= end_beat:
        return False
    return True


def apply_operations(project, operations):
    data = copy.deepcopy(project)
    for op_idx, op in enumerate(operations):
        if not isinstance(op, dict) or "op" not in op:
            raise ValueError(f"Operation #{op_idx} must be an object with an 'op' field.")
        kind = op["op"]

        if kind == "set_global":
            allowed = {
                "bpm",
                "tempo_changes",
                "sample_rate",
                "tail_seconds",
                "length_beats",
                "master_gain_db",
                "ceiling_db",
                "master_effects",
                "buses",
            }
            fields = op.get("fields", {})
            if not isinstance(fields, dict):
                raise ValueError("set_global requires a 'fields' object.")
            for k, v in fields.items():
                if k not in allowed:
                    raise ValueError(f"Unsupported global field: {k!r}")
                if v is None:
                    data.pop(k, None)
                else:
                    data[k] = v

        elif kind == "add_track":
            track = op.get("track")
            if not isinstance(track, dict):
                raise ValueError("add_track requires a 'track' object.")
            pos = op.get("index")
            if pos is None:
                data["tracks"].append(track)
            else:
                data["tracks"].insert(int(pos), track)

        elif kind == "remove_track":
            idx = _find_track_index(data["tracks"], op["track"])
            data["tracks"].pop(idx)

        elif kind == "duplicate_track":
            idx = _find_track_index(data["tracks"], op["track"])
            cloned = copy.deepcopy(data["tracks"][idx])
            if "new_name" in op:
                cloned["name"] = op["new_name"]
            else:
                cloned["name"] = cloned["name"] + " Copy"
            data["tracks"].insert(idx + 1, cloned)

        elif kind == "update_track":
            idx = _find_track_index(data["tracks"], op["track"])
            fields = op.get("fields", {})
            if not isinstance(fields, dict):
                raise ValueError("update_track requires a 'fields' object.")
            tr = data["tracks"][idx]
            for k, v in fields.items():
                if v is None:
                    tr.pop(k, None)
                else:
                    tr[k] = v

        elif kind == "edit_notes":
            idx = _find_track_index(data["tracks"], op["track"])
            tr = data["tracks"][idx]
            if "instrument" not in tr:
                raise ValueError(f"Track {tr['name']!r} is not an instrument track.")
            notes = list(tr.get("notes", []))
            action = op["action"]
            s_beat = op.get("start_beat")
            e_beat = op.get("end_beat")

            if action == "replace_all":
                notes = list(op["notes"])
            elif action == "append":
                notes.extend(op["notes"])
            elif action == "delete_range":
                notes = [n for n in notes if not _in_range(n[0], s_beat, e_beat)]
            elif action == "replace_range":
                notes = [n for n in notes if not _in_range(n[0], s_beat, e_beat)]
                notes.extend(op["notes"])
            elif action == "transpose":
                semi = int(op["semitones"])
                notes = [
                    [n[0], n[1], max(0, min(127, int(n[2]) + semi)), n[3]]
                    if _in_range(n[0], s_beat, e_beat)
                    else n
                    for n in notes
                ]
            elif action == "shift_time":
                delta = float(op["delta_beats"])
                shifted = []
                for n in notes:
                    if _in_range(n[0], s_beat, e_beat):
                        new_start = n[0] + delta
                        if new_start < 0:
                            raise ValueError("shift_time produced a negative note start beat.")
                        shifted.append([round(new_start, 6), n[1], n[2], n[3]])
                    else:
                        shifted.append(n)
                notes = shifted
            elif action == "scale_velocity":
                factor = float(op.get("factor", 1.0))
                offset = int(op.get("offset", 0))
                notes = [
                    [n[0], n[1], n[2], max(1, min(127, round(n[3] * factor + offset)))]
                    if _in_range(n[0], s_beat, e_beat)
                    else n
                    for n in notes
                ]
            elif action == "quantize":
                grid = float(op["grid_beats"])
                if grid <= 0:
                    raise ValueError("grid_beats must be > 0.")
                strength = float(op.get("strength", 1.0))
                out_notes = []
                for n in notes:
                    if _in_range(n[0], s_beat, e_beat):
                        target = round(n[0] / grid) * grid
                        q_beat = max(0.0, n[0] + (target - n[0]) * strength)
                        out_notes.append([round(q_beat, 6), n[1], n[2], n[3]])
                    else:
                        out_notes.append(n)
                notes = out_notes
            else:
                raise ValueError(f"Unknown edit_notes action: {action!r}")

            notes.sort(key=lambda n: (n[0], n[2]))
            tr["notes"] = notes

        elif kind == "edit_cc":
            idx = _find_track_index(data["tracks"], op["track"])
            tr = data["tracks"][idx]
            if "instrument" not in tr:
                raise ValueError(f"Track {tr['name']!r} is not an instrument track.")
            cc_rows = list(tr.get("cc", []))
            action = op["action"]
            s_beat = op.get("start_beat")
            e_beat = op.get("end_beat")
            ctrl = op.get("controller")

            if action == "replace_all":
                cc_rows = list(op["cc"])
            elif action == "append":
                cc_rows.extend(op["cc"])
            elif action == "delete_range":
                cc_rows = [
                    r
                    for r in cc_rows
                    if not (
                        _in_range(r[0], s_beat, e_beat)
                        and (ctrl is None or int(r[1]) == int(ctrl))
                    )
                ]
            else:
                raise ValueError(f"Unknown edit_cc action: {action!r}")

            cc_rows.sort(key=lambda r: (r[0], r[1]))
            tr["cc"] = cc_rows

        elif kind == "edit_clips":
            idx = _find_track_index(data["tracks"], op["track"])
            tr = data["tracks"][idx]
            if "clips" not in tr:
                raise ValueError(f"Track {tr['name']!r} is not an audio clip track.")
            clips = list(tr["clips"])
            action = op["action"]
            if action == "replace_all":
                clips = list(op["clips"])
            elif action == "append":
                clips.extend(op["clips"])
            elif action == "remove_index":
                clips.pop(int(op["index"]))
            elif action == "update_index":
                cidx = int(op["index"])
                for k, v in op["fields"].items():
                    if v is None:
                        clips[cidx].pop(k, None)
                    else:
                        clips[cidx][k] = v
            else:
                raise ValueError(f"Unknown edit_clips action: {action!r}")
            tr["clips"] = clips

        else:
            raise ValueError(f"Unknown operation: {kind!r}")

    validate_shape(data)
    return data


def edit_project(project_path, operations, output_project_path=None, overwrite=False):
    src = Path(project_path).resolve()
    dst = Path(output_project_path).resolve() if output_project_path else src
    if dst.suffix.lower() != ".json":
        raise ValueError("Use a .json project filename.")
    if dst != src and dst.exists() and not overwrite:
        raise ValueError(f"Target project already exists: {dst}. Pass overwrite=true to replace.")

    original = read_json(src)
    updated = apply_operations(original, operations)

    # Validate compiled timeline on a temp file next to destination before writing
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", dir=dst.parent, delete=False
    ) as f:
        tmp = Path(f.name)
        json.dump(updated, f, indent=2, ensure_ascii=False, allow_nan=False)
    try:
        compiled = compile_score(tmp)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)

    return {
        "project": str(dst),
        "sample_rate": compiled["rate"],
        "seconds": compiled["frames"] / compiled["rate"],
        "tracks": [t["name"] for t in compiled["tracks"]],
    }


SPECTRUM_BANDS = (
    ("sub_20_60hz", 20.0, 60.0),
    ("bass_60_250hz", 60.0, 250.0),
    ("low_mid_250_500hz", 250.0, 500.0),
    ("mid_500_2000hz", 500.0, 2000.0),
    ("high_mid_2000_6000hz", 2000.0, 6000.0),
    ("high_6000_20000hz", 6000.0, 20000.0),
)


def inspect_audio(
    audio_path,
    segment_seconds=1.0,
    max_segments=120,
    start_seconds=None,
    end_seconds=None,
):
    path = Path(audio_path).resolve()
    if not path.is_file():
        raise ValueError(f"Audio file not found: {path}")
    with AudioFile(str(path)) as af:
        sr = af.samplerate
        ch = af.num_channels
        total_frames = af.frames
        total_duration = total_frames / sr if sr else 0.0

        s_frame = 0 if start_seconds is None else max(0, round(float(start_seconds) * sr))
        e_frame = (
            total_frames
            if end_seconds is None
            else min(total_frames, round(float(end_seconds) * sr))
        )
        if s_frame >= e_frame:
            raise ValueError("Invalid audio inspection range: start_seconds must be < end_seconds.")
        frames = e_frame - s_frame
        duration = frames / sr if sr else 0.0
        if s_frame > 0:
            af.seek(s_frame)

        peak = 0.0
        sum_sq = 0.0
        clipped_samples = 0
        ch_sum_sq = np.zeros(ch, dtype=np.float64)
        lr_dot = 0.0
        mid_sq = 0.0
        side_sq = 0.0

        # Active audible span above -60 dBFS (0.001)
        audible_thresh = 1e-3
        first_audible = None
        last_audible = None

        # Welch-style averaged power spectrum (4096-point Hann window, 50% hop)
        n_fft = 4096
        hop = n_fft // 2
        window = np.hanning(n_fft).astype(np.float64)
        win_norm = float(np.sum(window ** 2))
        psd_acc = np.zeros(n_fft // 2 + 1, dtype=np.float64)
        psd_frames = 0
        fft_tail = np.zeros(0, dtype=np.float64)

        segments = []
        seg_frames = max(1, round(float(segment_seconds) * sr)) if segment_seconds else 0
        seg_peak = 0.0
        seg_sum_sq = 0.0
        seg_count = 0
        seg_start = s_frame

        remaining = frames
        rel_pos = 0
        while remaining > 0:
            block = af.read(min(65536, remaining))
            n = block.shape[1]
            if n == 0:
                break
            b64 = block.astype(np.float64)
            abs_blk = np.abs(block)
            peak = max(peak, float(np.max(abs_blk)))
            sum_sq += float(np.sum(b64 ** 2))
            clipped_samples += int(np.sum(abs_blk >= 0.999))
            ch_sum_sq += np.sum(b64 ** 2, axis=1)

            if ch >= 2:
                l_ch, r_ch = b64[0], b64[1]
                lr_dot += float(np.dot(l_ch, r_ch))
                mid = 0.5 * (l_ch + r_ch)
                side = 0.5 * (l_ch - r_ch)
                mid_sq += float(np.sum(mid ** 2))
                side_sq += float(np.sum(side ** 2))

            frame_env = np.max(abs_blk, axis=0)
            audible_idx = np.flatnonzero(frame_env >= audible_thresh)
            if audible_idx.size > 0:
                if first_audible is None:
                    first_audible = s_frame + rel_pos + int(audible_idx[0])
                last_audible = s_frame + rel_pos + int(audible_idx[-1])

            # Accumulate mono-summed FFT frames
            mono = np.mean(b64, axis=0)
            buf = np.concatenate([fft_tail, mono]) if fft_tail.size else mono
            offset_fft = 0
            while offset_fft + n_fft <= buf.size:
                seg_w = buf[offset_fft : offset_fft + n_fft] * window
                spec = np.fft.rfft(seg_w)
                psd_acc += (np.abs(spec) ** 2) / (n_fft * win_norm)
                psd_frames += 1
                offset_fft += hop
            fft_tail = buf[offset_fft:]

            if seg_frames > 0 and len(segments) < max_segments:
                cursor = 0
                while cursor < n and len(segments) < max_segments:
                    need = seg_frames - seg_count
                    take = min(need, n - cursor)
                    sub = b64[:, cursor : cursor + take]
                    seg_peak = max(seg_peak, float(np.max(np.abs(sub))))
                    seg_sum_sq += float(np.sum(sub ** 2))
                    seg_count += take
                    cursor += take
                    if seg_count >= seg_frames:
                        s_rms = math.sqrt(seg_sum_sq / max(1, seg_count * ch))
                        segments.append(
                            {
                                "start_s": round(seg_start / sr, 3),
                                "end_s": round((seg_start + seg_count) / sr, 3),
                                "peak_dbfs": _dbfs(seg_peak),
                                "rms_dbfs": _dbfs(s_rms),
                            }
                        )
                        seg_start += seg_count
                        seg_peak = 0.0
                        seg_sum_sq = 0.0
                        seg_count = 0
            rel_pos += n
            remaining -= n

        if seg_frames > 0 and seg_count > 0 and len(segments) < max_segments:
            s_rms = math.sqrt(seg_sum_sq / max(1, seg_count * ch))
            segments.append(
                {
                    "start_s": round(seg_start / sr, 3),
                    "end_s": round((seg_start + seg_count) / sr, 3),
                    "peak_dbfs": _dbfs(seg_peak),
                    "rms_dbfs": _dbfs(s_rms),
                }
            )

    rms = math.sqrt(sum_sq / max(1, frames * ch))
    peak_db = _dbfs(peak)
    rms_db = _dbfs(rms)
    crest_db = round(peak_db - rms_db, 3) if (peak_db is not None and rms_db is not None) else None

    stereo = None
    if ch >= 2:
        l_rms = math.sqrt(ch_sum_sq[0] / max(1, frames))
        r_rms = math.sqrt(ch_sum_sq[1] / max(1, frames))
        l_db = _dbfs(l_rms)
        r_db = _dbfs(r_rms)
        denom = math.sqrt(ch_sum_sq[0] * ch_sum_sq[1])
        corr = round(max(-1.0, min(1.0, lr_dot / denom)), 4) if denom > 1e-18 else None
        m_rms = math.sqrt(mid_sq / max(1, frames))
        s_rms = math.sqrt(side_sq / max(1, frames))
        width = round(s_rms / (m_rms + s_rms), 4) if (m_rms + s_rms) > 1e-12 else 0.0
        stereo = {
            "left_rms_dbfs": l_db,
            "right_rms_dbfs": r_db,
            "lr_balance_db": round(r_db - l_db, 3) if (l_db is not None and r_db is not None) else None,
            "phase_correlation": corr,
            "stereo_width": width,
        }

    spectrum_bands = {}
    if psd_frames > 0:
        psd = psd_acc / psd_frames
        # Single-sided power scaling (double interior bins)
        if psd.size > 2:
            psd[1:-1] *= 2.0
        freqs = np.fft.rfftfreq(n_fft, d=1.0 / sr)
        total_energy = float(np.sum(psd[(freqs >= 20.0) & (freqs <= min(20000.0, sr * 0.5))]))
        for bname, f_lo, f_hi in SPECTRUM_BANDS:
            mask = (freqs >= f_lo) & (freqs < min(f_hi, sr * 0.5))
            energy = float(np.sum(psd[mask])) if np.any(mask) else 0.0
            spectrum_bands[bname] = {
                "rms_dbfs": _dbfs(math.sqrt(max(0.0, energy))),
                "energy_pct": round(100.0 * energy / total_energy, 2) if total_energy > 1e-18 else 0.0,
            }

    result = {
        "file": str(path),
        "sample_rate": sr,
        "channels": ch,
        "frames": frames,
        "seconds": round(duration, 4),
        "total_seconds": round(total_duration, 4),
        "slice_seconds": [round(s_frame / sr, 4), round(e_frame / sr, 4)],
        "peak_dbfs": peak_db,
        "rms_dbfs": rms_db,
        "crest_factor_db": crest_db,
        "clipped_samples": clipped_samples,
        "active_span_seconds": [round(first_audible / sr, 3), round(last_audible / sr, 3)]
        if first_audible is not None
        else None,
        "spectrum_bands": spectrum_bands,
        "segments": segments,
    }
    if stereo is not None:
        result["stereo"] = stereo
    return result


def audition_audio(
    audio_path,
    start_seconds=None,
    end_seconds=None,
    output_preview_path=None,
    format="mp3",
    sample_rate=44100,
    segment_seconds=1.0,
):
    """Prepare a compact listening preview file plus objective acoustic diagnostics for multimodal audio inspection."""
    src = Path(audio_path).resolve()
    if not src.is_file():
        raise ValueError(f"Audio file not found: {src}")
    fmt = (format or "mp3").lower().lstrip(".")
    if fmt not in ("mp3", "wav"):
        raise ValueError("Preview format must be 'mp3' or 'wav'.")

    analysis = inspect_audio(
        src,
        segment_seconds=segment_seconds,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )

    if output_preview_path:
        preview = Path(output_preview_path).resolve()
        if preview.suffix.lower() != f".{fmt}":
            raise ValueError(f"output_preview_path must end with .{fmt}.")
    else:
        aud_dir = Path(tempfile.gettempdir()) / "agent-daw-audition"
        aud_dir.mkdir(parents=True, exist_ok=True)
        stat = src.stat()
        key = hashlib.sha256(
            json.dumps(
                [str(src), stat.st_mtime_ns, stat.st_size, start_seconds, end_seconds, fmt, sample_rate]
            ).encode()
        ).hexdigest()[:16]
        preview = aud_dir / f"{src.stem}_{key}.{fmt}"

    preview.parent.mkdir(parents=True, exist_ok=True)
    target_sr = int(sample_rate)

    with tempfile.NamedTemporaryFile(
        dir=preview.parent, suffix=f".{fmt}", delete=False
    ) as tf:
        tmp_path = Path(tf.name)
    try:
        with AudioFile(str(src)) as af:
            ch = min(2, af.num_channels)
            reader = af if af.samplerate == target_sr else af.resampled_to(target_sr)
            s_frame = 0 if start_seconds is None else max(0, round(float(start_seconds) * target_sr))
            total_target = round(af.frames * target_sr / af.samplerate)
            e_frame = (
                total_target
                if end_seconds is None
                else min(total_target, round(float(end_seconds) * target_sr))
            )
            if s_frame > 0:
                reader.seek(s_frame)
            rem = max(1, e_frame - s_frame)
            writer_kwargs = {"bit_depth": 16} if fmt == "wav" else {}
            with AudioFile(str(tmp_path), "w", target_sr, ch, **writer_kwargs) as writer:
                while rem > 0:
                    blk = reader.read(min(65536, rem))
                    if blk.shape[1] == 0:
                        break
                    writer.write(np.ascontiguousarray(blk[:ch]))
                    rem -= blk.shape[1]
        os.replace(tmp_path, preview)
    finally:
        tmp_path.unlink(missing_ok=True)

    return {
        "preview_path": str(preview),
        "mime_type": "audio/mpeg" if fmt == "mp3" else "audio/wav",
        "preview_bytes": preview.stat().st_size,
        "preview_sample_rate": target_sr,
        "listen_hint": "Call read_file on preview_path to listen natively to this audio.",
        "analysis": analysis,
    }


def audition_project(
    project_path,
    output_path=None,
    start_beat=None,
    end_beat=None,
    only_tracks=None,
    fresh=False,
    format="mp3",
    sample_rate=44100,
    segment_seconds=1.0,
):
    """Render a project (or region/soloed tracks), analyze acoustics, and produce a listening preview."""
    from engine import render as render_fn

    proj = Path(project_path).resolve()
    if output_path:
        wav_path = Path(output_path).resolve()
        render_report = render_fn(
            proj,
            wav_path,
            fresh=fresh,
            start_beat=start_beat,
            end_beat=end_beat,
            only_tracks=only_tracks,
        )
        aud = audition_audio(
            wav_path,
            format=format,
            sample_rate=sample_rate,
            segment_seconds=segment_seconds,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="agent-daw-listen-") as tmp:
            wav_path = Path(tmp) / f"{proj.stem}_audition.wav"
            render_report = render_fn(
                proj,
                wav_path,
                fresh=fresh,
                start_beat=start_beat,
                end_beat=end_beat,
                only_tracks=only_tracks,
            )
            aud = audition_audio(
                wav_path,
                format=format,
                sample_rate=sample_rate,
                segment_seconds=segment_seconds,
            )
            render_report["output"] = None

    return {
        "preview_path": aud["preview_path"],
        "mime_type": aud["mime_type"],
        "preview_bytes": aud["preview_bytes"],
        "listen_hint": aud["listen_hint"],
        "render": render_report,
        "analysis": aud["analysis"],
    }


def inspect_midi(midi_path):
    path = Path(midi_path).resolve()
    if not path.is_file():
        raise ValueError(f"MIDI file not found: {path}")
    mf = mido.MidiFile(str(path))
    tempos = []
    tracks_info = []
    for idx, trk in enumerate(mf.tracks):
        abs_tick = 0
        name = ""
        channels = set()
        pitches = []
        note_count = 0
        cc_count = 0
        first_tick = None
        last_tick = 0
        for msg in trk:
            abs_tick += msg.time
            if msg.is_meta:
                if msg.type == "track_name":
                    name = msg.name
                elif msg.type == "set_tempo":
                    tempos.append(
                        {
                            "beat": round(abs_tick / mf.ticks_per_beat, 4),
                            "bpm": round(mido.tempo2bpm(msg.tempo), 3),
                        }
                    )
            else:
                if hasattr(msg, "channel"):
                    channels.add(msg.channel + 1)
                if msg.type == "note_on" and msg.velocity > 0:
                    note_count += 1
                    pitches.append(msg.note)
                    if first_tick is None:
                        first_tick = abs_tick
                    last_tick = max(last_tick, abs_tick)
                elif msg.type == "control_change":
                    cc_count += 1
        tracks_info.append(
            {
                "index": idx,
                "name": name,
                "channels": sorted(channels),
                "note_count": note_count,
                "cc_count": cc_count,
                "pitch_range": [min(pitches), max(pitches)] if pitches else None,
                "beat_range": [
                    round(first_tick / mf.ticks_per_beat, 4),
                    round(last_tick / mf.ticks_per_beat, 4),
                ]
                if first_tick is not None
                else None,
            }
        )
    return {
        "file": str(path),
        "type": mf.type,
        "ticks_per_beat": mf.ticks_per_beat,
        "seconds": round(mf.length, 4),
        "tempos": tempos,
        "tracks": tracks_info,
    }


def export_midi(project_path, output_midi_path, ticks_per_beat=480):
    src = Path(project_path).resolve()
    dst = Path(output_midi_path).resolve()
    if dst.suffix.lower() != ".mid":
        raise ValueError("Output MIDI path must end with .mid.")
    project = read_json(src)
    validate_shape(project)

    mf = mido.MidiFile(type=1, ticks_per_beat=int(ticks_per_beat))
    tpb = mf.ticks_per_beat

    # Track 0: tempo map
    meta_trk = mido.MidiTrack()
    tempo_points = [(0.0, float(project.get("bpm", 120)))]
    for item in project.get("tempo_changes", []):
        tempo_points.append((float(item[0]), float(item[1])))
    by_beat = {}
    for b, bpm in tempo_points:
        by_beat[b] = bpm
    prev_tick = 0
    for b in sorted(by_beat):
        tick = round(b * tpb)
        meta_trk.append(
            mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(by_beat[b]), time=max(0, tick - prev_tick))
        )
        prev_tick = tick
    meta_trk.append(mido.MetaMessage("end_of_track", time=0))
    mf.tracks.append(meta_trk)

    exported_tracks = 0
    for tr in project["tracks"]:
        if "instrument" not in tr:
            continue
        exported_tracks += 1
        ch = tr.get("channel", 1) - 1
        ev = []  # (tick, priority, mido.Message)
        for beat, prog in tr.get("program_change", []):
            ev.append((round(beat * tpb), 1, mido.Message("program_change", channel=ch, program=int(prog))))
        for beat, cc, val in tr.get("cc", []):
            ev.append(
                (round(beat * tpb), 2, mido.Message("control_change", channel=ch, control=int(cc), value=int(val)))
            )
        for beat, bend in tr.get("pitch_bend", []):
            ev.append((round(beat * tpb), 2, mido.Message("pitchwheel", channel=ch, pitch=int(bend))))
        for beat, press in tr.get("channel_pressure", []):
            ev.append((round(beat * tpb), 2, mido.Message("aftertouch", channel=ch, value=int(press))))
        for beat, dur, pitch, vel in tr.get("notes", []):
            on_t = round(beat * tpb)
            off_t = max(on_t + 1, round((beat + dur) * tpb))
            ev.append((on_t, 3, mido.Message("note_on", channel=ch, note=int(pitch), velocity=int(vel))))
            ev.append((off_t, 0, mido.Message("note_off", channel=ch, note=int(pitch), velocity=0)))
        ev.sort(key=lambda x: (x[0], x[1]))

        trk = mido.MidiTrack()
        trk.append(mido.MetaMessage("track_name", name=tr["name"], time=0))
        prev = 0
        for tick, _prio, msg in ev:
            trk.append(msg.copy(time=max(0, tick - prev)))
            prev = tick
        trk.append(mido.MetaMessage("end_of_track", time=0))
        mf.tracks.append(trk)

    dst.parent.mkdir(parents=True, exist_ok=True)
    mf.save(str(dst))
    return {"midi": str(dst), "tracks_exported": exported_tracks, "ticks_per_beat": tpb}


def configure_studio(
    add_libraries=None,
    remove_libraries=None,
    set_plugin=None,
    add_vst3_dirs=None,
    remove_vst3_dirs=None,
):
    cfg = settings()
    libs = list(cfg.get("libraries", []))
    vst3s = list(cfg.get("vst3_dirs", []))
    changed = False

    if add_libraries:
        for item in add_libraries:
            p = Path(item).expanduser().resolve()
            if not p.is_dir():
                raise ValueError(f"Library directory does not exist: {p}")
            s = str(p).replace("\\", "/")
            if not any(Path(x).expanduser().resolve() == p for x in libs):
                libs.append(s)
                changed = True

    if remove_libraries:
        rem = {Path(x).expanduser().resolve() for x in remove_libraries}
        new_libs = [x for x in libs if Path(x).expanduser().resolve() not in rem]
        if len(new_libs) != len(libs):
            libs = new_libs
            changed = True

    if set_plugin is not None:
        if set_plugin == "":
            cfg.pop("plugin", None)
        else:
            p = Path(set_plugin).expanduser().resolve()
            if not p.exists():
                raise ValueError(f"Plugin path does not exist: {p}")
            cfg["plugin"] = str(p).replace("\\", "/")
        changed = True

    if add_vst3_dirs:
        for item in add_vst3_dirs:
            p = Path(item).expanduser().resolve()
            if not p.is_dir():
                raise ValueError(f"VST3 directory does not exist: {p}")
            s = str(p).replace("\\", "/")
            if not any(Path(x).expanduser().resolve() == p for x in vst3s):
                vst3s.append(s)
                changed = True

    if remove_vst3_dirs:
        rem = {Path(x).expanduser().resolve() for x in remove_vst3_dirs}
        new_vst3s = [x for x in vst3s if Path(x).expanduser().resolve() not in rem]
        if len(new_vst3s) != len(vst3s):
            vst3s = new_vst3s
            changed = True

    if changed:
        cfg["libraries"] = libs
        if vst3s:
            cfg["vst3_dirs"] = vst3s
        else:
            cfg.pop("vst3_dirs", None)
        _atomic_json(settings_path(), cfg)

    return {
        "settings_file": str(settings_path()),
        "plugin": str(plugin_path()),
        "libraries": [str(p) for p in libraries()],
        "vst3_dirs": cfg.get("vst3_dirs", []),
    }
