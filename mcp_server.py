"""Local JSON-RPC stdio MCP server for KontaktStudio. Native plugin processes never inherit protocol stdout."""

import json
import sys
from pathlib import Path
import fastjsonschema

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
from score import BOOL, SCHEMA, TEXT, array, catalog, integer, number, obj
from studio import doctor, store_project, validate


def absolute(value):
    p = Path(value)
    if not p.is_absolute():
        raise ValueError("MCP file paths must be absolute.")
    return p


def render_project(
    project_path,
    output_path,
    fresh=False,
    start_beat=None,
    end_beat=None,
    only_tracks=None,
    export_stems=False,
    stems_dir=None,
):
    from engine import render

    return render(
        absolute(project_path),
        absolute(output_path),
        fresh=fresh,
        start_beat=start_beat,
        end_beat=end_beat,
        only_tracks=only_tracks,
        export_stems=export_stems,
        stems_dir=absolute(stems_dir) if stems_dir else None,
    )


def list_instruments_tool(query="", kind="", limit=None, offset=0):
    items = catalog(query=query, kind=kind, limit=limit, offset=offset)
    return items


TOOLSET = {
    "studio_doctor": (
        "Check Kontakt 8 VST3 path, native file-drop bridge, preset cache, library roots, patch counts, and available VST3 plugins.",
        obj({}),
        doctor,
    ),
    "list_instruments": (
        "Search local NKI instruments, NKM multis, and VST3 presets. Multi-word queries require all tokens to match.",
        obj(
            {
                "query": {"type": "string"},
                "kind": {"enum": ["", "nki", "nkm", "vstpreset"]},
                "limit": integer(1, 5000),
                "offset": integer(0, 100000),
            }
        ),
        list_instruments_tool,
    ),
    "project_schema": (
        "Return the complete JSON Schema for AgentDAW project files (tracks, notes, MIDI controllers, tempo_changes, clips, buses, sends, automation, effects).",
        obj({}),
        lambda: SCHEMA,
    ),
    "save_project": (
        "Validate and save a complete project JSON at an absolute path. Existing files require overwrite=true.",
        obj(
            {"project_path": TEXT, "project": {"type": "object"}, "overwrite": BOOL},
            ("project_path", "project"),
        ),
        lambda project_path, project, overwrite=False: store_project(
            absolute(project_path), project, overwrite
        ),
    ),
    "load_project": (
        "Read an existing project JSON and its structural summary. Set summary_only=true to inspect track metadata without loading all note arrays.",
        obj(
            {"project_path": TEXT, "summary_only": BOOL},
            ("project_path",),
        ),
        lambda project_path, summary_only=False: load_project(
            absolute(project_path), summary_only=summary_only
        ),
    ),
    "edit_project": (
        "Apply surgical DAW operations to an existing project JSON (set_global, add_track, remove_track, duplicate_track, update_track, edit_notes, edit_cc, edit_clips) and save in place or to output_project_path.",
        obj(
            {
                "project_path": TEXT,
                "operations": array({"type": "object"}),
                "output_project_path": TEXT,
                "overwrite": BOOL,
            },
            ("project_path", "operations"),
        ),
        lambda project_path, operations, output_project_path=None, overwrite=False: edit_project(
            absolute(project_path),
            operations,
            output_project_path=absolute(output_project_path) if output_project_path else None,
            overwrite=overwrite,
        ),
    ),
    "validate_project": (
        "Validate a project JSON's schema, timeline, tempo map, buses, and file references.",
        obj({"project_path": TEXT}, ("project_path",)),
        lambda project_path: validate(absolute(project_path)),
    ),
    "render_project": (
        "Render a project (full timeline or optional [start_beat, end_beat] region, optional only_tracks filter, optional per-track WAV stem export) to 24-bit stereo WAV.",
        obj(
            {
                "project_path": TEXT,
                "output_path": TEXT,
                "fresh": BOOL,
                "start_beat": number(0, 1000000),
                "end_beat": number(0.00001, 1000000),
                "only_tracks": array(TEXT),
                "export_stems": BOOL,
                "stems_dir": TEXT,
            },
            ("project_path", "output_path"),
        ),
        render_project,
    ),
    "inspect_audio": (
        "Inspect a local audio file (full file or optional [start_seconds, end_seconds] slice): sample rate, channels, duration, peak/RMS/crest dBFS, clipping count, active audible span, stereo balance/correlation/width, 6-band spectral energy, and time-segmented loudness profile.",
        obj(
            {
                "audio_path": TEXT,
                "segment_seconds": number(0.05, 600),
                "max_segments": integer(1, 1000),
                "start_seconds": number(0, 1000000),
                "end_seconds": number(0.001, 1000000),
            },
            ("audio_path",),
        ),
        lambda audio_path, segment_seconds=1.0, max_segments=120, start_seconds=None, end_seconds=None: inspect_audio(
            absolute(audio_path),
            segment_seconds=segment_seconds,
            max_segments=max_segments,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        ),
    ),
    "audition_audio": (
        "Prepare a compact listening preview file (MP3 or 16-bit WAV, full file or [start_seconds, end_seconds] slice) together with objective acoustic, stereo, spectral, and dynamic diagnostics. Pass the returned preview_path to read_file to listen natively.",
        obj(
            {
                "audio_path": TEXT,
                "start_seconds": number(0, 1000000),
                "end_seconds": number(0.001, 1000000),
                "output_preview_path": TEXT,
                "format": {"enum": ["mp3", "wav"]},
                "sample_rate": {"enum": [24000, 44100, 48000]},
                "segment_seconds": number(0.05, 600),
            },
            ("audio_path",),
        ),
        lambda audio_path, start_seconds=None, end_seconds=None, output_preview_path=None, format="mp3", sample_rate=44100, segment_seconds=1.0: audition_audio(
            absolute(audio_path),
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            output_preview_path=absolute(output_preview_path) if output_preview_path else None,
            format=format,
            sample_rate=sample_rate,
            segment_seconds=segment_seconds,
        ),
    ),
    "audition_project": (
        "Render a project (full mix, optional [start_beat, end_beat] region, or optional only_tracks solo subset), compute objective acoustic/spectral/stereo diagnostics, and return a compact preview_path ready for native audio listening via read_file.",
        obj(
            {
                "project_path": TEXT,
                "output_path": TEXT,
                "start_beat": number(0, 1000000),
                "end_beat": number(0.00001, 1000000),
                "only_tracks": array(TEXT),
                "fresh": BOOL,
                "format": {"enum": ["mp3", "wav"]},
                "sample_rate": {"enum": [24000, 44100, 48000]},
                "segment_seconds": number(0.05, 600),
            },
            ("project_path",),
        ),
        lambda project_path, output_path=None, start_beat=None, end_beat=None, only_tracks=None, fresh=False, format="mp3", sample_rate=44100, segment_seconds=1.0: audition_project(
            absolute(project_path),
            output_path=absolute(output_path) if output_path else None,
            start_beat=start_beat,
            end_beat=end_beat,
            only_tracks=only_tracks,
            fresh=fresh,
            format=format,
            sample_rate=sample_rate,
            segment_seconds=segment_seconds,
        ),
    ),
    "inspect_midi": (
        "Inspect a standard MIDI (.mid) file: tracks, channels, tempo map, note counts, pitch ranges, and beat spans.",
        obj({"midi_path": TEXT}, ("midi_path",)),
        lambda midi_path: inspect_midi(absolute(midi_path)),
    ),
    "export_midi": (
        "Export an AgentDAW project's MIDI tracks and tempo map to a standard Type-1 MIDI (.mid) file.",
        obj(
            {
                "project_path": TEXT,
                "output_midi_path": TEXT,
                "ticks_per_beat": integer(24, 15360),
            },
            ("project_path", "output_midi_path"),
        ),
        lambda project_path, output_midi_path, ticks_per_beat=480: export_midi(
            absolute(project_path), absolute(output_midi_path), ticks_per_beat=ticks_per_beat
        ),
    ),
    "configure_studio": (
        "Read or update AgentDAW settings.json (library directories, Kontakt 8 VST3 path, extra VST3 effect directories).",
        obj(
            {
                "add_libraries": array(TEXT),
                "remove_libraries": array(TEXT),
                "set_plugin": {"type": "string"},
                "add_vst3_dirs": array(TEXT),
                "remove_vst3_dirs": array(TEXT),
            }
        ),
        configure_studio,
    ),
}
VALIDATORS = {name: fastjsonschema.compile(t[1]) for name, t in TOOLSET.items()}


class RpcError(Exception):
    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


def dispatch(method, params):
    if method == "initialize":
        version = params.get("protocolVersion")
        if version not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            version = "2025-11-25"
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "AgentDAW", "version": "1.0.0"},
            "instructions": "Use absolute paths for all MCP file arguments. Query list_instruments for real patch paths and project_schema for valid fields.",
        }
    if method == "ping":
        return {}
    if method == "resources/list":
        return {"resources": []}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "prompts/list":
        return {"prompts": []}
    if method == "tools/list":
        return {
            "tools": [
                {"name": name, "description": t[0], "inputSchema": t[1]}
                for name, t in TOOLSET.items()
            ]
        }
    if method == "tools/call":
        key = params.get("name")
        if not isinstance(key, str) or key not in TOOLSET:
            raise RpcError(-32602, "Unknown tool")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise RpcError(-32602, "Invalid tool arguments")
        try:
            VALIDATORS[key](args)
            result = TOOLSET[key][2](**args)
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
        except Exception as exc:
            return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    raise RpcError(-32601, "Method not found")


def serve():
    for line in sys.stdin:
        if not line.strip():
            continue
        request_id = None
        try:
            try:
                msg = json.loads(line)
            except ValueError as exc:
                raise RpcError(-32700, "Parse error") from exc
            if (
                not isinstance(msg, dict)
                or msg.get("jsonrpc") != "2.0"
                or not isinstance(msg.get("method"), str)
            ):
                raise RpcError(-32600, "Invalid request")
            if "id" not in msg:
                continue
            request_id = msg["id"]
            params = msg.get("params") or {}
            if not isinstance(params, dict):
                raise RpcError(-32602, "Invalid parameters")
            result = dispatch(msg["method"], params)
            response = {"result": result}
        except RpcError as exc:
            response = {"error": {"code": exc.code, "message": str(exc)}}
        except Exception as exc:
            response = {"error": {"code": -32603, "message": str(exc)}}
        print(
            json.dumps({"jsonrpc": "2.0", "id": request_id, **response}, ensure_ascii=False),
            flush=True,
        )
