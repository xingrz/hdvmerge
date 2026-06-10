import os
import tempfile
import unittest

from hdvmerge import scan as scanmod, plan as planmod, build as buildmod, verify as verifymod, model
from hdvmerge import ts as tsmod
from . import fixtures as fx


def _write(d, name, data):
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


def _read(p):
    with open(p, "rb") as f:
        return f.read()


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tape = fx.simple_tape(50)

    def _captures(self, dmgA=None):
        # capA covers tape [0,40), capB covers [10,50); overlap [10,40) is wider than 2*EDGE so
        # tape positions interior to BOTH captures exist; damage in tests sits at tape 24.
        a = _write(self.tmp, "capA.m2t",
                   fx.render_capture(self.tape, 0, 40, (2007, 1, 1, 9, 0, 0), damage=dmgA))
        b = _write(self.tmp, "capB.m2t",
                   fx.render_capture(self.tape, 10, 50, (2007, 1, 1, 9, 0, 0)))
        return [a, b]

    def test_index_is_idempotent_and_beside_the_file(self):
        files = self._captures()
        scanmod.ensure_index(files[0], force=True)
        ip = model.index_path(files[0])
        self.assertTrue(os.path.exists(ip))                 # index sits next to the capture
        a = _read(ip)
        scanmod.ensure_index(files[0], force=True)           # re-index same content
        self.assertEqual(_read(ip), a)           # byte-identical (idempotent)
        idx = model.load_index(ip)
        self.assertEqual(len(idx.gops), 40)
        self.assertEqual(idx.fingerprint, scanmod.fingerprint(files[0]))
        self.assertEqual(idx.version, model.INDEX_VERSION)
        self.assertEqual(idx.gops[0]["tc"], "07:00:00:00")   # tape TC attached per GOP

    def test_unchanged_source_is_not_reindexed(self):
        files = self._captures()
        idx1 = scanmod.ensure_index(files[0])
        marks = []
        scanmod.ensure_index(files[0], on_file=lambda i, cached=False, **k: marks.append(cached))
        self.assertEqual(marks, [True])                     # served from cache, not rebuilt

    def test_clean_merge(self):
        rep = scanmod.analyze(self._captures())
        self.assertEqual(len(rep.sources), 2)
        self.assertEqual(rep.chain, ["capA", "capB"])
        self.assertEqual(rep.shifts["capB"] - rep.shifts["capA"], 10)
        self.assertEqual(rep.gaps, [])

        plan = planmod.build_plan(rep)
        self.assertEqual(plan.bad_seams, 0)
        self.assertEqual(plan.total_frames, 50 * 4)
        self.assertEqual(plan.residuals, [])
        self.assertEqual(plan.divergences, [])
        self.assertEqual(len(plan.segments), 2)

        out = os.path.join(self.tmp, "merged.m2t")
        buildmod.build(plan, out)
        self.assertGreaterEqual(len(plan.segments), 2)   # at least one seam exists to mark
        marker = tsmod.make_disc_marker(plan.video_pid)
        expect = bytearray()
        for i, sg in enumerate(plan.segments):
            if i > 0:
                expect += marker                         # build inserts a disc marker per seam
            with open(sg.src, "rb") as f:
                f.seek(sg.off)
                expect += f.read(sg.end - sg.off)
        self.assertEqual(_read(out), bytes(expect))      # build = exact source bytes + seam markers

        out_idx = scanmod.scan_file(out)
        self.assertEqual(len(out_idx.gops), 50)          # whole tape, 50 GOPs (markers add none)
        self.assertEqual(sum(g["cc"] for g in out_idx.gops), 0)  # seam CC jumps are signalled, not flagged
        ok, info = verifymod.verify(out)
        self.assertTrue(ok)
        self.assertTrue(info["rec_head"].startswith("2007-01-01 09:00:0"))
        self.assertTrue(info["tc_head"].startswith("07:00:0"))       # tape TC survived the build

    def test_routes_around_overlap_damage(self):
        rep = scanmod.analyze(self._captures(dmgA={24: "cc"}))
        plan = planmod.build_plan(rep)
        self.assertEqual(plan.bad_seams, 0)
        self.assertEqual(plan.residuals, [])
        self.assertEqual(len(scanmod.scan_file(self._build(plan)).gops), 50)

    def test_single_copy_damage_is_residual(self):
        rep = scanmod.analyze(self._captures(dmgA={5: "cc"}))
        plan = planmod.build_plan(rep)
        recs = {r["rec"] for r in plan.residuals if r["tag"] == "capA"}
        self.assertTrue(recs & {"2007-01-01 09:00:04", "2007-01-01 09:00:05"}, recs)

    def test_overlap_byte_divergence_is_flagged(self):
        rep = scanmod.analyze(self._captures(dmgA={24: "corrupt"}))
        plan = planmod.build_plan(rep)
        self.assertTrue(plan.divergences)
        self.assertTrue(any(c["tag"] == "capA" for d in plan.divergences for c in d["copies"]))

    def test_decode_flag_becomes_residual(self):
        rep = scanmod.analyze(self._captures())
        rep.source("capA").gops[5]["dec"] = 3
        plan = planmod.build_plan(rep)
        self.assertTrue(any(r["tag"] == "capA" and r.get("dec") for r in plan.residuals))

    def _build(self, plan):
        out = os.path.join(self.tmp, "out.m2t")
        buildmod.build(plan, out)
        return out


if __name__ == "__main__":
    unittest.main()
