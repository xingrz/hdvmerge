"""The structured (JSON) analysis output — the contract a GUI/consumer reads instead of the
Markdown. Normal CLI use never exercises this path, so without these tests a later model refactor
could silently break it; they pin the shape and the field meanings to the model.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from hdvmerge import scan as scanmod, plan as planmod, jsonout, cli
from . import fixtures as fx


def _write(d, name, data):
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


class TestJsonOut(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tape = fx.simple_tape(50)

    def _captures(self, dmgA=None):
        a = _write(self.tmp, "capA.m2t",
                   fx.render_capture(self.tape, 0, 40, (2007, 1, 1, 9, 0, 0), damage=dmgA))
        b = _write(self.tmp, "capB.m2t",
                   fx.render_capture(self.tape, 10, 50, (2007, 1, 1, 9, 0, 0)))
        return [a, b]

    def _analysis(self, dmgA=None):
        rep = scanmod.analyze(self._captures(dmgA))
        return jsonout.analysis(rep, planmod.build_plan(rep))

    def test_clean_analysis_shape_and_is_json_serializable(self):
        d = self._analysis()
        # round-trips through JSON unchanged (no sets, no dataclasses leaking through)
        self.assertEqual(json.loads(json.dumps(d)), d)

        self.assertEqual(d["schema"], "hdvmerge.analysis/1")
        self.assertTrue(d["version"])
        self.assertEqual(d["chain"], ["capA", "capB"])
        self.assertEqual(d["fps"], 25.0)
        self.assertEqual(d["total_frames"], 50 * 4)
        self.assertEqual(d["bad_seams"], 0)
        self.assertTrue(d["complete"])                       # nothing to re-capture
        self.assertEqual(d["residuals"], [])
        self.assertEqual(d["divergences"], [])
        self.assertEqual(d["gaps"], [])
        self.assertEqual(d["unused_sources"], [])

        self.assertEqual(len(d["sources"]), 2)
        a, b = d["sources"]
        self.assertEqual((a["tag"], b["tag"]), ("capA", "capB"))
        self.assertEqual(a["shift"], 0)
        self.assertEqual(b["shift"] - a["shift"], 10)        # B starts 10 GOPs into the tape
        self.assertEqual(a["ngops"], 40)
        self.assertEqual(a["cc"], 0)
        self.assertEqual(a["tc0"], "07:00:00:00")            # span read from the index, not faked

        self.assertGreaterEqual(len(d["segments"]), 2)       # at least one seam
        seg = d["segments"][0]
        self.assertEqual(seg["tag"], "capA")
        self.assertEqual(seg["j0"], 0)
        self.assertFalse(seg["gap_before"])
        self.assertTrue(seg["tc"])

    def test_single_copy_damage_surfaces_as_residual(self):
        d = self._analysis(dmgA={5: "cc"})               # tape 5 lives only in capA -> no clean copy
        self.assertFalse(d["complete"])
        self.assertTrue(d["residuals"])
        r = d["residuals"][0]
        self.assertEqual(r["tag"], "capA")
        self.assertTrue(r["cc"] or r["tei"] or r.get("dec"))
        self.assertIn("rec", r)
        self.assertIn("tc", r)                            # cue points carried through to JSON

    def test_overlap_byte_divergence_surfaces(self):
        d = self._analysis(dmgA={24: "corrupt"})         # both copies clean at the TS layer but differ
        self.assertTrue(d["divergences"])
        self.assertTrue(any(c["tag"] == "capA"
                            for dv in d["divergences"] for c in dv["copies"]))

    def test_source_carries_its_own_damage_runs(self):
        d = self._analysis(dmgA={5: "cc"})           # capA damaged at tape 5; capB clean
        cap_a = next(s for s in d["sources"] if s["tag"] == "capA")
        cap_b = next(s for s in d["sources"] if s["tag"] == "capB")
        self.assertTrue(cap_a["damage"], "capA should list its own damaged run")
        self.assertGreater(cap_a["damage"][0]["cc"], 0)
        self.assertIsNotNone(cap_a["damage"][0]["tc0"])
        self.assertEqual(cap_b["damage"], [])        # capB is clean, so no own-damage runs
        self.assertEqual(json.loads(json.dumps(d)), d)   # still JSON round-trips

    def test_source_coverage_splits_at_a_tc_jump(self):
        # a capture that drops ~6 s of content (a continuity break) -> two coverage segments
        gops = [{"tc": "07:00:00:00"}, {"tc": "07:00:00:12"}, {"tc": "07:00:01:00"},
                {"tc": "07:00:07:00"}, {"tc": "07:00:07:12"}]   # 01:00 -> 07:00 is a 6 s jump
        segs = jsonout._source_coverage(gops, 25.0)
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0], {"tc0": "07:00:00:00", "tc1": "07:00:01:00"})
        self.assertEqual(segs[1], {"tc0": "07:00:07:00", "tc1": "07:00:07:12"})

    def test_source_coverage_is_one_segment_when_contiguous(self):
        gops = [{"tc": "07:00:00:00"}, {"tc": "07:00:00:12"}, {"tc": "07:00:01:00"}]
        self.assertEqual(len(jsonout._source_coverage(gops, 25.0)), 1)

    def test_lost_spans_flags_recorded_but_unreadable_tape(self):
        # the output jumps ~3 s in BOTH the rec-run tape TC and the wall clock = tape that was
        # recorded but unreadable in every pass
        emitted = [{"tc": "07:00:10:00", "rec": "2009-01-01 08:00:10", "frame": 100, "tag": "A"},
                   {"tc": "07:00:13:00", "rec": "2009-01-01 08:00:13", "frame": 112, "tag": "A"}]
        lost = planmod._lost_spans(emitted, 25.0)
        self.assertEqual(len(lost), 1)
        self.assertEqual((lost[0]["tc0"], lost[0]["tc1"]), ("07:00:10:00", "07:00:13:00"))
        self.assertGreater(lost[0]["frames"], 0)

    def test_lost_spans_ignores_a_camera_stop(self):
        # wall clock jumps 60 s but the rec-run tape TC barely moves = camera was off, not lost tape
        emitted = [{"tc": "07:00:10:00", "rec": "2009-01-01 08:00:10", "frame": 100, "tag": "A"},
                   {"tc": "07:00:10:12", "rec": "2009-01-01 08:01:10", "frame": 112, "tag": "A"}]
        self.assertEqual(planmod._lost_spans(emitted, 25.0), [])

    def test_lost_spans_skips_a_signalled_island_gap(self):
        emitted = [{"tc": "07:00:10:00", "rec": "2009-01-01 08:00:10", "frame": 100, "tag": "A"},
                   {"tc": "07:00:13:00", "rec": "2009-01-01 08:00:13", "frame": 112, "tag": "B",
                    "gap_before": True}]
        self.assertEqual(planmod._lost_spans(emitted, 25.0), [])

    def test_first_pcr_reads_the_tape_clock(self):
        pcr_base = 90000   # 1.0 s at 90 kHz
        af = bytes([7, 0x10,
                    (pcr_base >> 25) & 0xFF, (pcr_base >> 17) & 0xFF, (pcr_base >> 9) & 0xFF,
                    (pcr_base >> 1) & 0xFF, (pcr_base & 1) << 7, 0])
        pkt = bytes([0x47, 0x01, 0x00, 0x20]) + af          # afc=0b10 (adaptation field only)
        pkt = pkt + b"\xff" * (188 - len(pkt))
        self.assertAlmostEqual(planmod._first_pcr(pkt * 10), 1.0, places=3)

    def test_first_pcr_is_none_without_a_pcr(self):
        pkt = bytes([0x47, 0x01, 0x00, 0x10]) + b"\x00" * 184   # payload only, no PCR
        self.assertIsNone(planmod._first_pcr(pkt * 10))

    def test_cli_json_emits_exactly_one_object_on_stdout(self):
        files = self._captures()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--no-decode", "--json", *files])
        self.assertEqual(rc, 0)
        d = json.loads(out.getvalue())                   # stdout is one clean JSON object, nothing else
        self.assertEqual(d["schema"], "hdvmerge.analysis/1")
        self.assertEqual(d["chain"], ["capA", "capB"])
        self.assertIn("capA", err.getvalue())            # per-file status went to stderr, not stdout


if __name__ == "__main__":
    unittest.main()
