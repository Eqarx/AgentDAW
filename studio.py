"""KontaktStudio command-line entry point."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from editor import (
    audition_audio,
    audition_project,
    configure_studio,
    edit_project,
    export_midi,
    inspect_audio,
    inspect_midi,
    load_project,
)
from instrument import cache_dir
from score import (
    ROOT,
    SCHEMA,
    catalog,
    compile_score,
    libraries,
    list_vst3_plugins,
    plugin_path,
    validate_shape,
)


def doctor():
    import platform
    import pedalboard

    entries = catalog()
    return {
        "system": platform.platform(),
        "host": "Pedalboard " + pedalboard.__version__,
        "plugin": str(plugin_path()),
        "file_drop_bridge": (ROOT / "native" / "drop.dll").is_file(),
        "preset_cache": str(cache_dir()),
        "library_roots": [str(p) for p in libraries()],
        "instruments": sum(e["kind"] == "nki" for e in entries),
        "multis": sum(e["kind"] == "nkm" for e in entries),
        "vst_presets": sum(e["kind"] == "vstpreset" for e in entries),
        "vst3_plugins": list_vst3_plugins(),
    }


def validate(path):
    s = compile_score(path)
    if any("instrument" in t and t["_active"] for t in s["tracks"]):
        plugin_path()
    return {
        "valid": True,
        "sample_rate": s["rate"],
        "seconds": s["frames"] / s["rate"],
        "tracks": [t["name"] for t in s["tracks"]],
        "active_tracks": [t["name"] for t in s["tracks"] if t["_active"]],
        "buses": [b["name"] for b in s["buses"]],
    }


def store_project(path, score, overwrite=False):
    path = Path(path).resolve()
    if path.suffix.lower() != ".json":
        raise ValueError("Use a .json project filename.")
    validate_shape(score)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", dir=path.parent, delete=False
    ) as f:
        tmp = Path(f.name)
        json.dump(score, f, indent=2, ensure_ascii=False, allow_nan=False)
    try:
        validate(tmp)
        if overwrite:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return {"project": str(path)}


def client_config(client):
    args = ["-B", "-X", "utf8", str(ROOT / "studio.py"), "mcp"]
    if client == "antigravity":
        return {"mcpServers": {"agent-daw": {"command": sys.executable, "args": args}}}
    return (
        "[mcp_servers.agent-daw]\ncommand = "
        + json.dumps(sys.executable)
        + "\nargs = "
        + json.dumps(args)
        + "\ntool_timeout_sec = 900"
    )


def setup_plugin():
    cfg = client_config("antigravity")
    target = ROOT / "mcp_config.json"
    target.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"mcp_config": str(target), **cfg}


def capture(path):
    from engine import worker

    out = Path(path).resolve()
    if out.suffix.lower() != ".vstpreset" or out.exists():
        raise ValueError("Choose a new .vstpreset filename.")
    with tempfile.TemporaryDirectory() as tmp:
        worker({"plugin": str(plugin_path()), "capture": str(out)}, Path(tmp), 1800)
    return {"preset": str(out)}


def main():
    p = argparse.ArgumentParser(description="Offline DAW engine and Kontakt 8 VST3 host.")
    sub = p.add_subparsers(dest="command", required=True)
    for key in ("doctor", "schema", "mcp", "setup-plugin"):
        sub.add_parser(key)

    lib = sub.add_parser("library")
    lib.add_argument("--query", default="")
    lib.add_argument("--kind", default="")
    lib.add_argument("--limit", type=int, default=None)
    lib.add_argument("--offset", type=int, default=0)

    sub.add_parser("validate").add_argument("project")

    ld = sub.add_parser("load")
    ld.add_argument("project")
    ld.add_argument("--summary", action="store_true")

    render = sub.add_parser("render")
    render.add_argument("project")
    render.add_argument("output")
    render.add_argument(
        "--fresh",
        action="store_true",
        help="Reload each NKI/NKM from disk and refresh its cached preset.",
    )
    render.add_argument("--start-beat", type=float, default=None)
    render.add_argument("--end-beat", type=float, default=None)
    render.add_argument("--tracks", nargs="*", default=None)
    render.add_argument("--export-stems", action="store_true")
    render.add_argument("--stems-dir", default=None)

    ia = sub.add_parser("inspect-audio")
    ia.add_argument("audio")
    ia.add_argument("--segment-seconds", type=float, default=1.0)
    ia.add_argument("--start-seconds", type=float, default=None)
    ia.add_argument("--end-seconds", type=float, default=None)

    aa = sub.add_parser("audition-audio")
    aa.add_argument("audio")
    aa.add_argument("--start-seconds", type=float, default=None)
    aa.add_argument("--end-seconds", type=float, default=None)
    aa.add_argument("--output-preview", default=None)
    aa.add_argument("--format", choices=("mp3", "wav"), default="mp3")
    aa.add_argument("--sample-rate", type=int, default=44100)
    aa.add_argument("--segment-seconds", type=float, default=1.0)

    ap = sub.add_parser("audition-project")
    ap.add_argument("project")
    ap.add_argument("--output", default=None)
    ap.add_argument("--start-beat", type=float, default=None)
    ap.add_argument("--end-beat", type=float, default=None)
    ap.add_argument("--tracks", nargs="*", default=None)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--format", choices=("mp3", "wav"), default="mp3")
    ap.add_argument("--sample-rate", type=int, default=44100)
    ap.add_argument("--segment-seconds", type=float, default=1.0)

    im = sub.add_parser("inspect-midi")
    im.add_argument("midi")

    em = sub.add_parser("export-midi")
    em.add_argument("project")
    em.add_argument("output")
    em.add_argument("--tpb", type=int, default=480)

    sub.add_parser("capture").add_argument("output")
    sub.add_parser("client-config").add_argument("client", choices=("codex", "antigravity"))
    a = p.parse_args()

    if a.command == "mcp":
        from mcp_server import serve

        return serve()
    if a.command == "render":
        from engine import render as render_fn

        result = render_fn(
            a.project,
            a.output,
            fresh=a.fresh,
            start_beat=a.start_beat,
            end_beat=a.end_beat,
            only_tracks=a.tracks,
            export_stems=a.export_stems,
            stems_dir=a.stems_dir,
        )
    elif a.command == "capture":
        result = capture(a.output)
    elif a.command == "client-config":
        result = client_config(a.client)
    elif a.command == "setup-plugin":
        result = setup_plugin()
    elif a.command == "validate":
        result = validate(a.project)
    elif a.command == "load":
        result = load_project(a.project, summary_only=a.summary)
    elif a.command == "library":
        result = catalog(a.query, kind=a.kind, limit=a.limit, offset=a.offset)
    elif a.command == "inspect-audio":
        result = inspect_audio(
            a.audio,
            segment_seconds=a.segment_seconds,
            start_seconds=a.start_seconds,
            end_seconds=a.end_seconds,
        )
    elif a.command == "audition-audio":
        result = audition_audio(
            a.audio,
            start_seconds=a.start_seconds,
            end_seconds=a.end_seconds,
            output_preview_path=a.output_preview,
            format=a.format,
            sample_rate=a.sample_rate,
            segment_seconds=a.segment_seconds,
        )
    elif a.command == "audition-project":
        result = audition_project(
            a.project,
            output_path=a.output,
            start_beat=a.start_beat,
            end_beat=a.end_beat,
            only_tracks=a.tracks,
            fresh=a.fresh,
            format=a.format,
            sample_rate=a.sample_rate,
            segment_seconds=a.segment_seconds,
        )
    elif a.command == "inspect-midi":
        result = inspect_midi(a.midi)
    elif a.command == "export-midi":
        result = export_midi(a.project, a.output, ticks_per_beat=a.tpb)
    elif a.command == "schema":
        result = SCHEMA
    else:
        result = doctor()
    print(
        result
        if isinstance(result, str)
        else json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
