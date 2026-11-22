"""Run from the plugin directory: runtime\\python.exe -B -X utf8 tests\\test_engine.py"""

import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mido
import numpy as np
from pedalboard.io import AudioFile

from editor import audition_audio, audition_project, edit_project, export_midi, inspect_audio, inspect_midi, load_project
from engine import render
from mcp_server import dispatch
from score import TempoMap, compile_score


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.project = self.folder / "music.json"

    def score(self, tracks, **extra):
        if isinstance(tracks, dict):
            tracks = [tracks]
        self.project.write_text(
            json.dumps(dict(version=1, tracks=tracks, **extra)), encoding="utf-8"
        )

    def make_wav(self, name, samples, sr=48000):
        path = self.folder / name
        arr = np.asarray(samples, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr[None, :]
        with AudioFile(str(path), "w", sr, arr.shape[0], bit_depth=24) as wav:
            wav.write(arr)
        return path

    def test_clip_placement_balance_and_failed_revision(self):
        values = np.linspace(0.1, 0.3, 24000, dtype="f4")
        self.make_wav("ramp.wav", values, 48000)
        clip = dict(
            file="ramp.wav", at=1, in_seconds=0.1, length_seconds=0.2, fade_in=0.05, fade_out=0.05
        )
        self.score(dict(name="clip", pan=-1, clips=[clip]), bpm=120, tail_seconds=0)
        output = self.folder / "mix.wav"
        render(self.project, output)
        with AudioFile(str(output)) as wav:
            samples = wav.read(wav.frames)
        self.assertEqual(samples.shape, (2, 33600))
        self.assertLess(np.abs(samples[1]).max(), 2e-6)
        self.assertLess(np.abs(samples[0, :24001]).max(), 2e-6)
        self.assertLess(abs(samples[0, -1]), 2e-6)
        self.assertAlmostEqual(samples[0, 28800], values[9600], places=5)
        previous = output.read_bytes()
        clip["length_seconds"] = 1000
        self.score(dict(name="invalid", clips=[clip]))
        with self.assertRaises(ValueError):
            render(self.project, output)
        self.assertEqual(previous, output.read_bytes())

    def test_imported_tempo_map(self):
        midi = mido.MidiFile(ticks_per_beat=480)
        track = midi.add_track()
        for msg in [
            mido.Message("note_on", note=60, velocity=90, time=480),
            mido.Message("note_off", note=60, time=480),
            mido.MetaMessage("set_tempo", tempo=1000000),
            mido.Message("note_on", note=64, velocity=90, time=480),
            mido.Message("note_off", note=64, time=240),
        ]:
            track.append(msg)
        midi.save(self.folder / "tempo.mid")
        (self.folder / "fixture.nki").touch()
        self.score(dict(name="tempo test", instrument="fixture.nki", midi_file="tempo.mid"))
        events = compile_score(self.project)["tracks"][0]["events"]
        self.assertEqual([row[0] for row in events], [24000, 48000, 96000, 120000])

    def test_tempo_map_step_and_linear(self):
        tm = TempoMap(120, [[4, 60, "step"]])
        self.assertAlmostEqual(tm.beat_to_seconds(4), 2.0, places=6)
        self.assertAlmostEqual(tm.beat_to_seconds(6), 4.0, places=6)
        tm_lin = TempoMap(60, [[2, 120, "linear"]])
        expected = 60.0 * 2.0 / 60.0 * math.log(2.0)
        self.assertAlmostEqual(tm_lin.beat_to_seconds(2), expected, places=6)
        self.assertAlmostEqual(tm_lin.beat_to_seconds(4), expected + 1.0, places=6)

    def test_midi_expression_and_ordering(self):
        (self.folder / "inst.nki").touch()
        self.score(
            dict(
                name="expr",
                instrument="inst.nki",
                channel=2,
                program_change=[[0, 5]],
                cc=[[0, 11, 100]],
                pitch_bend=[[0, 4096]],
                channel_pressure=[[0, 64]],
                notes=[[0, 1, 60, 90], [1, 1, 60, 100]],
            ),
            bpm=120,
        )
        events = compile_score(self.project)["tracks"][0]["events"]
        # At frame 0: program_change (0xC1) -> cc/pb/cp -> note_on (0x91)
        at0 = [e[1][0] & 0xF0 for e in events if e[0] == 0]
        self.assertEqual(at0[0], 0xC0)
        self.assertEqual(at0[-1], 0x90)
        # At beat 1 (frame 24000): note_off (0x81) before note_on (0x91)
        at1 = [e[1][0] & 0xF0 for e in events if e[0] == 24000]
        self.assertEqual(at1, [0x80, 0x90])

    def test_buses_sends_automation_latency_fx_and_stems(self):
        t = np.linspace(0, 0.5, 24000, endpoint=False, dtype=np.float32)
        sine = 0.25 * np.sin(2 * math.pi * 440 * t)
        self.make_wav("tone.wav", sine, 48000)
        self.score(
            [
                dict(
                    name="Lead",
                    clips=[dict(file="tone.wav", at=0, fade_curve="equal_power", fade_in=0.05)],
                    volume_automation=[[0, -12], [1, 0]],
                    pan_automation=[[0, -1], [1, 1]],
                    effects=[
                        dict(type="peak", cutoff_frequency_hz=1000, gain_db=2.0, q=1.0),
                        dict(type="brickwall_limiter", ceiling_db=-2.0, lookahead_ms=5.0),
                    ],
                    sends=[dict(bus="Space", gain_db=-6, pre_fader=False)],
                ),
                dict(
                    name="Muted",
                    mute=True,
                    clips=[dict(file="tone.wav", at=0)],
                ),
            ],
            bpm=120,
            tail_seconds=0.2,
            buses=[
                dict(
                    name="Space",
                    effects=[dict(type="reverb", room_size=0.4, wet_level=0.5, dry_level=0.0)],
                )
            ],
            master_effects=[dict(type="highpass", cutoff_frequency_hz=30)],
        )
        out = self.folder / "bus_mix.wav"
        res = render(self.project, out, export_stems=True)
        self.assertTrue(out.is_file())
        self.assertEqual(len(res["stems"]), 1)
        self.assertTrue(Path(res["stems"][0]).is_file())
        self.assertEqual(len(res["buses"]), 1)
        self.assertGreater(res["buses"][0]["mixed_peak"], 0.0)
        self.assertFalse(res["tracks"][1]["active"])

    def test_clip_reverse_and_time_stretch_and_region(self):
        ramp = np.linspace(0.05, 0.4, 24000, dtype=np.float32)
        self.make_wav("r.wav", ramp, 48000)
        self.score(
            dict(
                name="fxclip",
                clips=[
                    dict(file="r.wav", at=0, reverse=True, speed=2.0, pitch_semitones=2.0, pan=0.5)
                ],
            ),
            bpm=120,
            tail_seconds=0,
        )
        out = self.folder / "stretched.wav"
        res = render(self.project, out, start_beat=0, end_beat=0.5)
        self.assertEqual(res["frames"], 12000)
        info = inspect_audio(out, segment_seconds=0.1)
        self.assertEqual(info["frames"], 12000)
        self.assertGreater(len(info["segments"]), 0)

    def test_edit_project_and_midi_roundtrip(self):
        (self.folder / "a.nki").touch()
        (self.folder / "b.nki").touch()
        self.score(
            dict(
                name="Flute",
                instrument="a.nki",
                notes=[[0.02, 1.0, 60, 80], [1.0, 1.0, 64, 90]],
                cc=[[0, 11, 100]],
            ),
            bpm=100,
        )
        v2 = self.folder / "v2.json"
        edit_project(
            self.project,
            [
                {"op": "set_global", "fields": {"bpm": 110}},
                {"op": "update_track", "track": "Flute", "fields": {"name": "Oboe", "instrument": "b.nki"}},
                {"op": "edit_notes", "track": "Oboe", "action": "quantize", "grid_beats": 0.5},
                {"op": "edit_notes", "track": "Oboe", "action": "transpose", "semitones": 2},
                {"op": "edit_notes", "track": "Oboe", "action": "scale_velocity", "factor": 0.5, "offset": 10},
                {"op": "duplicate_track", "track": "Oboe", "new_name": "Oboe 2"},
            ],
            output_project_path=v2,
        )
        loaded = load_project(v2)
        self.assertEqual(loaded["project"]["bpm"], 110)
        self.assertEqual(len(loaded["project"]["tracks"]), 2)
        self.assertEqual(loaded["project"]["tracks"][0]["notes"][0], [0.0, 1.0, 62, 50])

        mid_path = self.folder / "exported.mid"
        ex = export_midi(v2, mid_path)
        self.assertEqual(ex["tracks_exported"], 2)
        insp = inspect_midi(mid_path)
        self.assertEqual(len(insp["tracks"]), 3)  # tempo track + 2 instrument tracks
        self.assertEqual(insp["tracks"][1]["pitch_range"], [62, 66])

    def test_audition_audio_and_project_diagnostics(self):
        sr = 48000
        t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
        left = 0.25 * np.sin(2 * math.pi * 440 * t)
        right = 0.25 * np.sin(2 * math.pi * 440 * t)
        self.make_wav("stereo_440.wav", np.vstack([left, right]), sr)
        self.score(dict(name="Tone", clips=[dict(file="stereo_440.wav", at=0)]), bpm=120, tail_seconds=0)

        aud_proj = audition_project(self.project, format="mp3", sample_rate=24000, segment_seconds=0.5)
        preview = Path(aud_proj["preview_path"])
        self.assertTrue(preview.is_file())
        self.assertEqual(aud_proj["mime_type"], "audio/mpeg")
        an = aud_proj["analysis"]
        self.assertEqual(an["clipped_samples"], 0)
        self.assertAlmostEqual(an["stereo"]["phase_correlation"], 1.0, places=3)
        self.assertGreater(an["spectrum_bands"]["low_mid_250_500hz"]["energy_pct"], 80.0)
        preview.unlink(missing_ok=True)

        aud_slice = audition_audio(
            self.folder / "stereo_440.wav",
            start_seconds=0.2,
            end_seconds=0.7,
            format="wav",
            sample_rate=24000,
        )
        p2 = Path(aud_slice["preview_path"])
        self.assertTrue(p2.is_file())
        self.assertAlmostEqual(aud_slice["analysis"]["seconds"], 0.5, places=3)
        p2.unlink(missing_ok=True)

    def test_mcp_tool_inventory_and_argument_errors(self):
        tools = [t["name"] for t in dispatch("tools/list", {})["tools"]]
        for expected in (
            "studio_doctor",
            "list_instruments",
            "project_schema",
            "save_project",
            "load_project",
            "edit_project",
            "validate_project",
            "render_project",
            "inspect_audio",
            "audition_audio",
            "audition_project",
            "inspect_midi",
            "export_midi",
            "configure_studio",
        ):
            self.assertIn(expected, tools)
        response = dispatch("tools/call", {"name": "validate_project", "arguments": {}})
        self.assertTrue(response["isError"])
        # Null arguments should not crash
        doc_resp = dispatch("tools/call", {"name": "project_schema", "arguments": None})
        self.assertNotIn("isError", doc_resp)


if __name__ == "__main__":
    unittest.main()
