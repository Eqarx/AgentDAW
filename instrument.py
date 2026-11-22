"""One disposable Kontakt host process per MIDI track."""

import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

import numpy as np
from pedalboard import load_plugin, VST3Plugin

from score import ROOT, read_json


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        name = f.name
        f.write(data)
    try:
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class InstrumentDrop:
    """Pedalboard polls this callback on the plugin editor's own message thread."""

    def __init__(self, plugin, patch):
        self.plugin = plugin
        self.patch = str(patch)
        self.bridge = ctypes.CDLL(str(ROOT / "native" / "drop.dll"))
        self.bridge.load_file.argtypes = [ctypes.c_wchar_p]
        self.bridge.load_file.restype = ctypes.c_int
        self.empty_size = len(plugin.preset_data)
        self.start = time.monotonic()
        self.sent = None
        self.checked = 0
        self.size = 0
        self.stable_since = 0
        self.ready = False

    def is_set(self):
        now = time.monotonic()
        if now - self.start > 90:
            raise RuntimeError(
                "Kontakt did not finish loading this instrument. Check its library/sample location or use a saved .vstpreset."
            )
        if now - self.start < 0.6:
            return False
        if self.sent is None:
            status = self.bridge.load_file(self.patch)
            if status == 1 and now - self.start < 8:
                return False
            if status not in (0, 5, 6):
                raise RuntimeError(
                    f"Kontakt editor file-drop unavailable (code {status}). Use a .vstpreset or the capture command."
                )
            self.sent = now
            return False
        if now - self.checked < 0.4:
            return False
        self.checked = now
        size = len(self.plugin.preset_data)
        if size != self.size:
            self.size, self.stable_since = size, now
        self.ready = (
            size > self.empty_size + 256
            and now - self.stable_since >= 1.5
            and now - self.sent >= 2.5
        )
        return self.ready


def load_host(path):
    # Pedalboard 0.9.26 otherwise enumerates thousands of Kontakt parameter ranges
    # on construction even for an empty initial parameter dict. Scoped to this worker.
    original = VST3Plugin.__set_initial_parameter_values__

    def initialize(instance, values):
        if values:
            original(instance, values)

    VST3Plugin.__set_initial_parameter_values__ = initialize
    try:
        return load_plugin(str(path), initialization_timeout=0)
    finally:
        VST3Plugin.__set_initial_parameter_values__ = original


def cache_dir():
    override = os.environ.get("AGENT_DAW_CACHE") or os.environ.get("KONTAKT_STUDIO_CACHE")
    if override:
        return Path(override)
    base = Path(os.environ["LOCALAPPDATA"])
    primary = base / "AgentDAW" / "presets"
    legacy = base / "KontaktStudio" / "presets"
    if not primary.exists() and legacy.is_dir():
        return legacy
    return primary


def cached_path(patch, vst):
    plugin_binary = vst
    if vst.is_dir():
        binaries = sorted(vst.glob("Contents/x86_64-win/*.vst3"))
        if binaries:
            plugin_binary = binaries[0]
    identity = [
        str(p.resolve()) + ":" + str(p.stat().st_mtime_ns) + ":" + str(p.stat().st_size)
        for p in (patch, plugin_binary)
    ]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return cache_dir() / (key + ".vstpreset")


def run(job):
    global instance  # The OS unloads the VST after files are closed and published.
    vst = Path(job["plugin"])
    instance = load_host(vst)
    if not instance.is_instrument or "kontakt" not in instance.name.lower():
        raise RuntimeError(f"Expected a Kontakt instrument, loaded {instance.name!r}")
    if job.get("capture"):
        empty = len(instance.preset_data)
        instance.show_editor()
        preset = instance.preset_data
        if len(preset) <= empty + 256:
            raise RuntimeError("Empty rack. Load an instrument before closing its window.")
        atomic_bytes(Path(job["capture"]), preset)
        return
    tr = job["track"]
    patch = Path(tr["instrument"])
    if patch.suffix.lower() in (".nkm", ".vstpreset"):
        # Configure the output bus and sample rate before Kontakt builds a multi rack.
        instance([], 0.05, job["rate"], num_channels=2, buffer_size=512, reset=False)
    cache = None
    if patch.suffix.lower() == ".vstpreset":
        instance.load_preset(str(patch))
        route = "vstpreset"
    else:
        cache = cached_path(patch, vst)
        if cache.is_file() and not job.get("fresh"):
            instance.load_preset(str(cache))
            route = "cache"
        else:
            drop = InstrumentDrop(instance, patch)
            instance.show_editor(drop)
            if not drop.ready:
                raise RuntimeError("Kontakt editor closed before the instrument was ready.")
            route = patch.suffix.lower()[1:]
    # Store the instrument's initial settings, before score CCs change them.
    preset = instance.preset_data if route in ("nki", "nkm") else None
    sr, frames = job["rate"], job["frames"]
    events = iter(tr["events"])
    current = next(events, None)
    peak = 0.0
    sum_sq = 0.0
    with open(job["audio"], "wb") as audio:
        for start in range(0, frames, sr):
            count = min(sr, frames - start)
            midi = []
            while current is not None and current[0] < start + count:
                midi.append((bytes(current[1]), (current[0] - start) / sr))
                current = next(events, None)
            block = instance(
                midi, (count + 0.1) / sr, sr, num_channels=2, buffer_size=512, reset=False
            )
            if (
                block.shape[0] != 2
                or abs(block.shape[1] - count) > 1
                or not np.isfinite(block).all()
            ):
                raise RuntimeError("Invalid audio block from Kontakt.")
            if block.shape[1] < count:
                block = np.pad(block, ((0, 0), (0, 1)))
            block = block[:, :count]
            peak = max(peak, float(np.max(np.abs(block))))
            sum_sq += float(np.sum(block.astype(np.float64) ** 2))
            np.ascontiguousarray(block.T, dtype="<f4").tofile(audio)
    if peak < 1e-9 and not tr.get("allow_silent", False):
        raise RuntimeError(
            f"Silent Kontakt track {tr['name']!r}. Check MIDI channel, note range, and missing samples."
        )
    if preset is not None and peak >= 1e-9:
        atomic_bytes(cache, preset)
    rms = math.sqrt(sum_sq / max(1, frames * 2))
    Path(job["result"]).write_text(
        json.dumps(
            {
                "load": route,
                "instrument_peak": peak,
                "instrument_peak_dbfs": round(20 * math.log10(peak), 3) if peak > 0 else None,
                "instrument_rms_dbfs": round(20 * math.log10(rms), 3) if rms > 0 else None,
            }
        ),
        encoding="utf-8",
    )


def exit_worker(code):
    # On Windows the C runtime's _exit still invokes DLL detach callbacks.
    # Some Kontakt builds crash there. No live files or UI belong to the worker
    # at this point; end its private process without running those callbacks.
    sys.stdout.flush()
    sys.stderr.flush()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    if not kernel.TerminateProcess(kernel.GetCurrentProcess(), code):
        raise ctypes.WinError(ctypes.get_last_error())


if __name__ == "__main__":
    try:
        run(read_json(sys.argv[1]))
    except BaseException:
        traceback.print_exc()
        exit_worker(1)
    exit_worker(0)
