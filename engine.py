"""Offline stereo DAW mixer with sends/buses, automation, clip DSP, FX latency flush, and stem export."""

from pathlib import Path
import json
import math
import os
import re
import subprocess
import sys
import tempfile

import numpy as np
import pedalboard as pb
from pedalboard.io import AudioFile
from score import ROOT, compile_score, plugin_path, read_json

BLOCK = 32768


def effects(specs):
    types = {
        "reverb": pb.Reverb,
        "delay": pb.Delay,
        "compressor": pb.Compressor,
        "limiter": pb.Limiter,
        "brickwall_limiter": pb.BrickwallLimiter,
        "noise_gate": pb.NoiseGate,
        "highpass": pb.HighpassFilter,
        "lowpass": pb.LowpassFilter,
        "peak": pb.PeakFilter,
        "low_shelf": pb.LowShelfFilter,
        "high_shelf": pb.HighShelfFilter,
        "chorus": pb.Chorus,
        "phaser": pb.Phaser,
        "distortion": pb.Distortion,
        "clipping": pb.Clipping,
        "bitcrush": pb.Bitcrush,
        "pitch_shift": pb.PitchShift,
        "gain": pb.Gain,
        "invert": pb.Invert,
        "convolution": pb.Convolution,
    }
    plugins = []
    for s in specs or []:
        kind = s["type"]
        params = {k: v for k, v in s.items() if k != "type"}
        if kind == "ladder_filter":
            if "mode" in params:
                params["mode"] = getattr(pb.LadderFilter.Mode, params["mode"])
            plugins.append(pb.LadderFilter(**params))
        elif kind == "vst3":
            plug = pb.load_plugin(params["plugin"])
            if not plug.is_effect:
                raise ValueError(f"VST3 plugin is not an audio effect: {params['plugin']}")
            if "preset" in params:
                plug.load_preset(params["preset"])
            for pk, pv in (params.get("parameters") or {}).items():
                if not hasattr(plug, pk):
                    raise ValueError(
                        f"VST3 plugin {plug.name!r} has no parameter {pk!r}. Available: {list(plug.parameters.keys())[:30]}"
                    )
                setattr(plug, pk, pv)
            plugins.append(plug)
        else:
            plugins.append(types[kind](**params))
    return pb.Pedalboard(plugins)


def worker(job, temp, timeout):
    path = temp / "job.json"
    path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    log = temp / "worker.log"
    with open(log, "wb") as f:
        proc = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(ROOT / "instrument.py"), str(path)],
            stdout=f,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    if proc.returncode:
        with log.open("rb") as f:
            f.seek(max(0, log.stat().st_size - 2400))
            detail = f.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Instrument host exit {proc.returncode}. {detail}")


def gain(db):
    return 10 ** (db / 20.0)


def dbfs(linear):
    return round(20 * math.log10(linear), 3) if linear > 1e-12 else None


def pan_balance(pan):
    p = float(np.clip(pan, -1.0, 1.0))
    return np.array(
        [math.cos(max(0.0, p) * math.pi / 2.0), math.cos(max(0.0, -p) * math.pi / 2.0)],
        dtype="f4",
    )


def eval_gain_pan(start, count, base_gain_db, base_pan, vol_auto, pan_auto):
    if vol_auto is None and pan_auto is None:
        return pan_balance(base_pan) * gain(base_gain_db)
    idx = np.arange(start, start + count, dtype=np.float64)
    if vol_auto is not None:
        xp = np.array([p[0] for p in vol_auto], dtype=np.float64)
        fp = np.array([p[1] for p in vol_auto], dtype=np.float64)
        db = base_gain_db + np.interp(idx, xp, fp)
        amp = np.power(10.0, db / 20.0).astype(np.float32)
    else:
        amp = np.full(count, gain(base_gain_db), dtype=np.float32)
    if pan_auto is not None:
        xp = np.array([p[0] for p in pan_auto], dtype=np.float64)
        fp = np.array([p[1] for p in pan_auto], dtype=np.float64)
        pv = np.clip(np.interp(idx, xp, fp), -1.0, 1.0)
        left = np.cos(np.maximum(0.0, pv) * (math.pi / 2.0)).astype(np.float32)
        right = np.cos(np.maximum(0.0, -pv) * (math.pi / 2.0)).astype(np.float32)
        bal = np.stack([left * amp, right * amp], axis=1)
    else:
        b = pan_balance(base_pan)
        bal = np.stack([amp * b[0], amp * b[1]], axis=1)
    return bal


def _clip_envelope(pos, duration, sr, gain_db, fade_in, fade_out, fade_curve):
    env = np.full(len(pos), gain(gain_db), dtype=np.float32)
    fi = round(fade_in * sr)
    fo = round(fade_out * sr)
    if fi > 0:
        ramp = np.minimum(1.0, pos / max(1, fi - 1))
        if fade_curve == "equal_power":
            ramp = np.sin(ramp * (math.pi / 2.0))
        env *= ramp.astype(np.float32)
    if fo > 0:
        ramp = np.minimum(1.0, (duration - 1 - pos) / max(1, fo - 1))
        if fade_curve == "equal_power":
            ramp = np.sin(ramp * (math.pi / 2.0))
        env *= ramp.astype(np.float32)
    return env


def clip_audio(target, clips, sr, total_frames):
    for c in clips:
        speed = float(c.get("speed", 1.0))
        pitch = float(c.get("pitch_semitones", 0.0))
        reverse = bool(c.get("reverse", False))
        fade_curve = c.get("fade_curve", "linear")
        clip_bal = pan_balance(c.get("pan", 0.0))
        needs_full = reverse or abs(speed - 1.0) > 1e-6 or abs(pitch) > 1e-6

        with AudioFile(c["file"]) as af:
            reader = af if af.samplerate == sr else af.resampled_to(sr)
            start_sr = round(c["start"] * sr / af.samplerate)
            src_frames_sr = max(1, round(c["size"] * sr / af.samplerate))
            reader.seek(start_sr)

            if needs_full:
                audio = reader.read(src_frames_sr)
                if audio.shape[1] == 0:
                    raise ValueError(f"Unexpected end of clip {c['file']}")
                if audio.shape[0] == 1:
                    audio = np.repeat(audio, 2, axis=0)
                if reverse:
                    audio = np.ascontiguousarray(audio[:, ::-1])
                if abs(speed - 1.0) > 1e-6 or abs(pitch) > 1e-6:
                    audio = pb.time_stretch(
                        np.ascontiguousarray(audio, dtype=np.float32),
                        sr,
                        stretch_factor=speed,
                        pitch_shift_in_semitones=pitch,
                    )
                duration = audio.shape[1]
                pos = np.arange(duration, dtype=np.float64)
                env = _clip_envelope(
                    pos,
                    duration,
                    sr,
                    c.get("gain_db", 0.0),
                    c.get("fade_in", 0.0),
                    c.get("fade_out", 0.0),
                    fade_curve,
                )
                processed = audio.T * env[:, None] * clip_bal[None, :]
                dst_start = c["at_frame"]
                dst_end = dst_start + duration
                clip_l = max(0, -dst_start)
                clip_r = duration - max(0, dst_end - total_frames)
                if clip_r > clip_l:
                    out_l = dst_start + clip_l
                    out_r = dst_start + clip_r
                    target[out_l:out_r] += processed[clip_l:clip_r]
            else:
                duration = c["frames"]
                for offset in range(0, duration, BLOCK):
                    n = min(BLOCK, duration - offset)
                    audio = reader.read(n)
                    actual = audio.shape[1]
                    if actual == 0 and offset + n < duration:
                        raise ValueError(f"Unexpected end of clip {c['file']}")
                    if actual < n - 1:
                        raise ValueError(f"Short read from {c['file']}")
                    if not actual:
                        continue
                    pos = offset + np.arange(actual, dtype=np.float64)
                    env = _clip_envelope(
                        pos,
                        duration,
                        sr,
                        c.get("gain_db", 0.0),
                        c.get("fade_in", 0.0),
                        c.get("fade_out", 0.0),
                        fade_curve,
                    )
                    if audio.shape[0] == 1:
                        stereo = np.repeat(audio.T, 2, axis=1)
                    else:
                        stereo = audio.T
                    chunk = stereo * env[:, None] * clip_bal[None, :]
                    dst_start = c["at_frame"] + offset
                    dst_end = dst_start + actual
                    clip_l = max(0, -dst_start)
                    clip_r = actual - max(0, dst_end - total_frames)
                    if clip_r > clip_l:
                        target[dst_start + clip_l : dst_start + clip_r] += chunk[clip_l:clip_r]


def stream_effects(src, frames, chain, sr, consumer):
    """Stream (frames, 2) through Pedalboard chain with latency flush, invoking consumer(pos, block)."""
    if not chain:
        for i in range(0, frames, BLOCK):
            blk = np.asarray(src[i : i + BLOCK], dtype=np.float32)
            if not np.isfinite(blk).all():
                raise ValueError("Invalid floating point audio.")
            consumer(i, blk)
        return

    out_pos = 0
    for i in range(0, frames, BLOCK):
        blk = np.asarray(src[i : i + BLOCK], dtype=np.float32)
        y = chain(np.ascontiguousarray(blk.T), sr, reset=False).T
        if y.shape[0] == 0:
            continue
        if not np.isfinite(y).all():
            raise ValueError("Invalid floating point audio in effects chain.")
        take = min(y.shape[0], frames - out_pos)
        if take > 0:
            consumer(out_pos, y[:take])
            out_pos += take

    # Flush buffered latency samples from lookahead / FFT plugins
    flush_rounds = 0
    while out_pos < frames and flush_rounds < 64:
        flush_rounds += 1
        need = frames - out_pos
        pad = np.zeros((2, min(BLOCK, need + 8192)), dtype=np.float32)
        y = chain(pad, sr, reset=False).T
        if y.shape[0] == 0:
            continue
        if not np.isfinite(y).all():
            raise ValueError("Invalid floating point audio during effects flush.")
        take = min(y.shape[0], need)
        consumer(out_pos, y[:take])
        out_pos += take
    if out_pos < frames:
        consumer(out_pos, np.zeros((frames - out_pos, 2), dtype=np.float32))


def _close_memmap(mm):
    if mm is None:
        return
    try:
        mm.flush()
    except Exception:
        pass
    try:
        mm._mmap.close()
    except Exception:
        pass


def _safe_filename(name):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(". ")
    return cleaned or "track"


def render(
    project,
    output,
    fresh=False,
    start_beat=None,
    end_beat=None,
    only_tracks=None,
    export_stems=False,
    stems_dir=None,
):
    score = compile_score(
        project, start_beat=start_beat, end_beat=end_beat, only_tracks=only_tracks
    )
    out = Path(output).resolve()
    if out.suffix.lower() != ".wav":
        raise ValueError("Output must have the .wav extension.")
    for tr in score["tracks"]:
        if any(Path(c["file"]) == out for c in tr.get("clips", [])):
            raise ValueError("Choose an output distinct from the source clips.")
    out.parent.mkdir(parents=True, exist_ok=True)

    stem_root = None
    if export_stems:
        stem_root = Path(stems_dir).resolve() if stems_dir else (out.parent / f"{out.stem}_stems")
        stem_root.mkdir(parents=True, exist_ok=True)

    sr, frames, settings = score["rate"], score["frames"], score["project"]
    reports = []
    bus_reports = []
    exported_stems = []

    with tempfile.TemporaryDirectory(prefix=".agent-daw-", dir=out.parent) as tmp:
        temp = Path(tmp)
        shape = (frames, 2)
        mix = np.memmap(temp / "mix.f32", mode="w+", dtype="<f4", shape=shape)
        bus_buffers = {}
        try:
            for idx, b in enumerate(score["buses"]):
                bus_buffers[b["name"]] = np.memmap(
                    temp / f"bus_{idx}.f32", mode="w+", dtype="<f4", shape=shape
                )

            for tr_idx, tr in enumerate(score["tracks"]):
                diag = {"name": tr["name"], "active": bool(tr["_active"])}
                if not tr["_active"]:
                    diag.update(mixed_peak=0.0, mixed_peak_dbfs=None, rms_dbfs=None)
                    reports.append(diag)
                    continue

                if "instrument" in tr:
                    job = dict(
                        track=tr,
                        plugin=str(plugin_path()),
                        audio=str(temp / "track.f32"),
                        result=str(temp / "result.json"),
                        rate=sr,
                        frames=frames,
                        fresh=fresh,
                    )
                    worker(job, temp, max(150, 180 + frames / sr * 3))
                    diag.update(read_json(job["result"]))
                    raw = np.memmap(job["audio"], mode="r", dtype="<f4", shape=shape)
                else:
                    raw = np.memmap(temp / "track.f32", mode="w+", dtype="<f4", shape=shape)

                stem_writer = None
                stem_tmp_path = None
                stem_final_path = None
                if export_stems:
                    stem_name = f"{tr_idx + 1:02d}_{_safe_filename(tr['name'])}.wav"
                    stem_final_path = stem_root / stem_name
                    stem_tmp_path = temp / f"stem_{tr_idx}.wav"
                    stem_writer = AudioFile(str(stem_tmp_path), "w", sr, 2, bit_depth=24)

                try:
                    if "clips" in tr:
                        clip_audio(raw, tr["clips"], sr, frames)
                    chain = effects(tr.get("effects", []))
                    base_gain = tr.get("gain_db", 0.0)
                    base_pan = tr.get("pan", 0.0)
                    vol_auto = tr.get("_vol_auto")
                    pan_auto = tr.get("_pan_auto")
                    pre_sends = [
                        (bus_buffers[s["bus"]], gain(s.get("gain_db", 0.0)))
                        for s in tr.get("sends", [])
                        if s.get("pre_fader", False)
                    ]
                    post_sends = [
                        (bus_buffers[s["bus"]], gain(s.get("gain_db", 0.0)))
                        for s in tr.get("sends", [])
                        if not s.get("pre_fader", False)
                    ]

                    peak_box = [0.0]
                    sum_sq_box = [0.0]

                    def on_track_block(pos, fx_block):
                        n = len(fx_block)
                        for bbuf, sgain in pre_sends:
                            bbuf[pos : pos + n] += fx_block * sgain
                        bal = eval_gain_pan(pos, n, base_gain, base_pan, vol_auto, pan_auto)
                        post = fx_block * bal
                        mix[pos : pos + n] += post
                        for bbuf, sgain in post_sends:
                            bbuf[pos : pos + n] += post * sgain
                        if stem_writer is not None:
                            stem_writer.write(np.ascontiguousarray(post.T))
                        peak_box[0] = max(peak_box[0], float(np.max(np.abs(post))))
                        sum_sq_box[0] += float(np.sum(post.astype(np.float64) ** 2))

                    stream_effects(raw, frames, chain, sr, on_track_block)
                    rms = math.sqrt(sum_sq_box[0] / max(1, frames * 2))
                    diag["mixed_peak"] = peak_box[0]
                    diag["mixed_peak_dbfs"] = dbfs(peak_box[0])
                    diag["rms_dbfs"] = dbfs(rms)
                    reports.append(diag)
                finally:
                    _close_memmap(raw)
                    del raw
                    if stem_writer is not None:
                        stem_writer.close()
                if stem_tmp_path is not None and stem_tmp_path.exists():
                    os.replace(stem_tmp_path, stem_final_path)
                    exported_stems.append(str(stem_final_path))
                    diag["stem"] = str(stem_final_path)

            # Process buses into mix
            for b in score["buses"]:
                bname = b["name"]
                if b.get("mute", False):
                    bus_reports.append({"name": bname, "active": False, "mixed_peak": 0.0})
                    continue
                braw = bus_buffers[bname]
                bchain = effects(b.get("effects", []))
                bgain = b.get("gain_db", 0.0)
                bpan = b.get("pan", 0.0)
                bvol = b.get("_vol_auto")
                bpan_a = b.get("_pan_auto")
                bpeak = [0.0]
                bsum = [0.0]

                def on_bus_block(pos, fx_block):
                    n = len(fx_block)
                    bal = eval_gain_pan(pos, n, bgain, bpan, bvol, bpan_a)
                    post = fx_block * bal
                    mix[pos : pos + n] += post
                    bpeak[0] = max(bpeak[0], float(np.max(np.abs(post))))
                    bsum[0] += float(np.sum(post.astype(np.float64) ** 2))

                stream_effects(braw, frames, bchain, sr, on_bus_block)
                brms = math.sqrt(bsum[0] / max(1, frames * 2))
                bus_reports.append(
                    {
                        "name": bname,
                        "active": True,
                        "mixed_peak": bpeak[0],
                        "mixed_peak_dbfs": dbfs(bpeak[0]),
                        "rms_dbfs": dbfs(brms),
                    }
                )
                braw = None

            # Master effects chain (buffered to separate memmap to avoid in-place overwrite hazard)
            master_chain = effects(score["master_effects"])
            master_gain = gain(settings.get("master_gain_db", 0.0))
            master_out = np.memmap(temp / "master.f32", mode="w+", dtype="<f4", shape=shape)
            peak_box = [0.0]
            sum_sq_box = [0.0]
            try:

                def on_master_block(pos, fx_block):
                    blk = fx_block * master_gain
                    master_out[pos : pos + len(blk)] = blk
                    peak_box[0] = max(peak_box[0], float(np.max(np.abs(blk))))
                    sum_sq_box[0] += float(np.sum(blk.astype(np.float64) ** 2))

                stream_effects(mix, frames, master_chain, sr, on_master_block)
                peak = peak_box[0]
                if peak < 1e-8:
                    raise ValueError("Final mix is silent.")
                scale = min(1.0, gain(settings.get("ceiling_db", -1.0)) / peak)
                final_rms = math.sqrt(sum_sq_box[0] / max(1, frames * 2)) * scale
                with AudioFile(str(temp / "finished.wav"), "w", sr, 2, bit_depth=24) as writer:
                    for i in range(0, frames, BLOCK):
                        writer.write(np.ascontiguousarray(master_out[i : i + BLOCK].T * scale))
            finally:
                _close_memmap(master_out)
                del master_out
        finally:
            for k in list(bus_buffers.keys()):
                _close_memmap(bus_buffers[k])
                del bus_buffers[k]
            _close_memmap(mix)
            del mix

        with AudioFile(str(temp / "finished.wav")) as check:
            if (check.frames, check.num_channels, check.samplerate) != (frames, 2, sr):
                raise RuntimeError("Final WAV verification failed.")
        os.replace(temp / "finished.wav", out)

    result = {
        "output": str(out),
        "frames": frames,
        "sample_rate": sr,
        "seconds": frames / sr,
        "peak_dbfs": round(20 * math.log10(peak * scale), 3),
        "rms_dbfs": dbfs(final_rms),
        "ceiling_gain_db": round(20 * math.log10(scale), 3),
        "tracks": reports,
    }
    if bus_reports:
        result["buses"] = bus_reports
    if exported_stems:
        result["stems"] = exported_stems
    if start_beat is not None or end_beat is not None:
        result["region_beats"] = [start_beat or 0, end_beat]
    return result
