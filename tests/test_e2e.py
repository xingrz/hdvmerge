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
        self.assertEqual(idx.gops[0]["tc"], "00:00:00:00")   # tape TC (MM:SS:FF, no hours) per GOP

    def test_unchanged_source_is_not_reindexed(self):
        files = self._captures()
        idx1 = scanmod.ensure_index(files[0])
        marks = []
        scanmod.ensure_index(files[0], on_file=lambda i, cached=False, **k: marks.append(cached))
        self.assertEqual(marks, [True])                     # served from cache, not rebuilt

    def test_needs_index_predicts_the_cache_skip(self):
        f = self._captures()[0]
        self.assertTrue(scanmod.needs_index(f))             # no cache yet -> work to do
        scanmod.ensure_index(f)                             # build + cache it
        self.assertFalse(scanmod.needs_index(f))            # now served from cache, no work
        self.assertTrue(scanmod.needs_index(f, force=True))     # force always reindexes
        self.assertTrue(scanmod.needs_index(f, use_cache=False))  # cache off always works
        # a changed source invalidates the cache (the fingerprint shifts)
        _write(self.tmp, "capA.m2t", fx.render_capture(self.tape, 0, 30, (2007, 1, 1, 9, 0, 0)))
        self.assertTrue(scanmod.needs_index(f))

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
        self.assertGreaterEqual(len(plan.segments), 2)   # at least one seam to re-phase across

        # build re-phases CC and changes NOTHING else: same length, and every byte equals the plain
        # source concatenation except the continuity-counter nibble (low 4 bits of header byte 3).
        expect = bytearray()
        for sg in plan.segments:
            with open(sg.src, "rb") as f:
                f.seek(sg.off)
                expect += f.read(sg.end - sg.off)
        got = _read(out)
        self.assertEqual(len(got), len(expect))          # in-place; no markers, no size change
        for base in range(0, len(expect), 188):
            self.assertEqual(got[base:base + 3], bytes(expect[base:base + 3]))        # sync/PID/flags
            self.assertEqual(got[base + 3] & 0xF0, expect[base + 3] & 0xF0)           # only CC differs
            self.assertEqual(got[base + 4:base + 188], bytes(expect[base + 4:base + 188]))  # payload

        out_idx = scanmod.scan_file(out)
        self.assertEqual(len(out_idx.gops), 50)          # whole tape, 50 GOPs
        self.assertEqual(sum(g["cc"] for g in out_idx.gops), 0)  # CC re-phased continuous at the seam
        ok, info = verifymod.verify(out)
        self.assertTrue(ok)
        self.assertTrue(info["rec_head"].startswith("2007-01-01 09:00:0"))
        self.assertTrue(info["tc_head"].startswith("00:00:0"))       # tape TC survived the build

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

    def test_spine_dropout_at_divergence_is_filled_in_order(self):
        # The spine capture diverges byte-wise at tape 24 AND drops tape [25,28); two other captures
        # hold those GOPs and agree at 24. The walk must follow the 2-vs-1 majority at the seam and
        # emit the held GOPs in tape order — not ride the spine's same-file contiguity across its own
        # hole, which would strand them as an out-of-tape-order backfill island and mis-flag the jump
        # "recorded but unreadable in every capture". (A byte-identical re-encode shares one hash and
        # never diverges, so same-file contiguity still wins for it — see the clean-merge tests.)
        dt = (2007, 1, 1, 9, 0, 0)
        # capA: one continuous-CC stream that is byte-divergent at tape 24 and then drops [25,28) —
        # tape 24 stays CLEAN (a real seam frame, not damaged), and 24->28 is a pure TC/rec jump.
        capA = fx.render_capture(self.tape, 0, 40, dt, damage={24: "corrupt"}, skip={25, 26, 27})
        a = _write(self.tmp, "capA.m2t", capA)
        b = _write(self.tmp, "capB.m2t", fx.render_capture(self.tape, 10, 40, dt))
        c = _write(self.tmp, "capC.m2t", fx.render_capture(self.tape, 10, 40, dt))
        rep = scanmod.analyze([a, b, c])
        self.assertEqual(rep.chain[0], "capA")               # spine = earliest tape position
        plan = planmod.build_plan(rep)
        self.assertTrue(plan.divergences)                    # the seam divergence is still recorded
        self.assertEqual(plan.lost, [])                      # held by capB/capC, so nothing is "lost"
        self.assertEqual(plan.bad_seams, 0)
        out_idx = scanmod.scan_file(self._build(plan))
        self.assertEqual(len(out_idx.gops), 40)              # whole tape [0,40) emitted, in order

    def test_decode_flag_becomes_residual(self):
        rep = scanmod.analyze(self._captures())
        rep.source("capA").gops[5]["dec"] = 3
        plan = planmod.build_plan(rep)
        self.assertTrue(any(r["tag"] == "capA" and r.get("dec") for r in plan.residuals))

    def test_divergent_island_duplicate_is_deduped(self):
        # capLong holds tape [0,40) clean; capShort holds [20,24) as byte-DIFFERENT but still-clean
        # copies (a divergence). The hash-based walk emits capLong's run AND re-seeds capShort's [20,24)
        # as an island (its hashes look uncovered), duplicating moments 20-23 in the output. The dedup
        # must collapse each tape moment to one frame (keeping the longer capLong copy), losing none.
        capLong = fx.render_capture(self.tape, 0, 40, (2007, 1, 1, 9, 0, 0))
        capShort = fx.render_capture(self.tape, 20, 24, (2007, 1, 1, 9, 0, 0),
                                     damage={i: "corrupt" for i in range(20, 24)})
        a = _write(self.tmp, "long.m2t", capLong)
        b = _write(self.tmp, "short.m2t", capShort)
        plan = planmod.build_plan(scanmod.analyze([a, b]))
        out = self._build(plan)
        self.assertEqual(verifymod.find_duplicate_frames(out), [])     # each tape moment emitted once
        self.assertEqual(len(scanmod.scan_file(out).gops), 40)          # whole tape [0,40), nothing lost
        self.assertEqual(plan.bad_seams, 0)

    def test_decode_flag_on_a_byte_clean_twin_is_not_a_residual(self):
        # capA (the spine) is byte-divergent at tape 23, so at tape 24 the walk can't hash-hop to capB
        # and emits capA's own GOP 24 — which then carries a decode flag (an ffmpeg cascade) even though
        # capB holds the byte-IDENTICAL GOP 24 clean. Identical bytes decode identically, so the flag is
        # a capture-local artifact, not damage: it must NOT become a residual. (probe.py intends this
        # for dec; enforced in _prep so the clean twin isn't stranded.)
        rep = scanmod.analyze(self._captures(dmgA={23: "corrupt"}))
        a, b = rep.source("capA"), rep.source("capB")
        twin = next(g for g in b.gops if g["tc"] == a.gops[24]["tc"])
        self.assertEqual(a.gops[24]["h"], twin["h"])          # byte-identical across the two captures
        self.assertEqual((twin["cc"], twin["tei"], twin.get("dec", 0)), (0, 0, 0))  # clean in capB
        a.gops[24]["dec"] = 4                                  # ffmpeg cascades a decode flag onto it
        plan = planmod.build_plan(rep)
        self.assertEqual(plan.residuals, [])                  # the clean twin clears it — not damage

    def test_rephasing_makes_a_built_merge_reread_seamless(self):
        # Re-feed a built merge as a single source: re-phasing made CC continuous at the seam, so
        # the output re-scans with zero continuity breaks (no markers, no residuals).
        rep = scanmod.analyze(self._captures())
        out = self._build(planmod.build_plan(rep))
        idx = scanmod.scan_file(out)
        self.assertEqual(len(idx.gops), 50)
        self.assertEqual(sum(g["cc"] for g in idx.gops), 0)      # seamless on re-read
        self.assertEqual(sum(g["tei"] for g in idx.gops), 0)

    def test_output_self_check(self):
        rep = scanmod.analyze(self._captures())
        plan = planmod.build_plan(rep)
        out = self._build(plan)
        ok, info = verifymod.verify_build(out, plan, decode=False)   # CC-integrity (no ffmpeg)
        self.assertTrue(ok)
        self.assertEqual(info["cc"], info["expected_cc"])
        self.assertEqual(info["expected_cc"], 0)                     # clean merge -> zero breaks

        # a single corrupted CC nibble (a spurious continuity break) must fail the self-check
        data = bytearray(_read(out))
        vp = rep.sources[0].video_pid
        seen = 0
        for base in range(0, len(data) - 188, 188):
            if data[base] == 0x47 and tsmod.pid(data[base:base + 188]) == vp:
                seen += 1
                if seen == 5:
                    data[base + 3] = (data[base + 3] & 0xF0) | ((data[base + 3] + 7) & 0x0F)
                    break
        bad = _write(self.tmp, "corrupt.m2t", bytes(data))
        ok2, info2 = verifymod.verify_build(bad, plan, decode=False)
        self.assertFalse(ok2)                                        # integrity check catches it
        self.assertGreater(info2["cc"], info2["expected_cc"])

    def test_island_is_stitched_back_across_a_gap(self):
        # capA covers tape [0,20), capB [30,50) -> [20,30) is an unbridgeable gap (no overlap). capB
        # is a separate island: the walk can't reach it by hash, but its tape TC and wall-clock both
        # place it cleanly after capA, so the find-back stitches it in across a signalled gap rather
        # than dropping it.
        a = _write(self.tmp, "capA.m2t", fx.render_capture(self.tape, 0, 20, (2007, 1, 1, 9, 0, 0)))
        b = _write(self.tmp, "capB.m2t", fx.render_capture(self.tape, 30, 50, (2007, 1, 1, 9, 0, 0)))
        plan = planmod.build_plan(scanmod.analyze([a, b]))
        self.assertEqual(plan.unused_sources, [])               # recovered, not dropped or flagged
        capb = [s for s in plan.segments if s.tag == "capB"]
        self.assertEqual(len(capb), 1)
        self.assertTrue(capb[0].gap_before)                     # stitched across a signalled gap
        self.assertEqual(plan.total_frames, (20 + 20) * 4)      # both islands' frames recovered

        # the built file: capB's bytes follow a disc marker; re-scanning sees the gap as signalled
        # (not a continuity-break residual), and capA/capB are each internally seamless.
        out = self._build(plan)
        oidx = scanmod.scan_file(out)
        self.assertEqual(len(oidx.gops), 40)
        self.assertEqual(sum(g["cc"] for g in oidx.gops), 0)    # gap is disc-signalled, capB re-phased

    def test_a_stray_first_sorted_capture_does_not_strand_the_rest(self):
        # cap0 sorts first but is a disjoint island [0,10); cap1 [20,50) is damaged at tape 35 where
        # cap2 [30,60) is clean. A single walk seeded on cap0 used to strand cap1/cap2 — cap2 dropped
        # (unused) and cap1's damage left as a residual. Every island must be walked: cap2 repairs
        # cap1, and the result must not depend on cap0 sorting first.
        self.tape = fx.simple_tape(60)
        c0 = _write(self.tmp, "cap0.m2t", fx.render_capture(self.tape, 0, 10, (2007, 1, 1, 9, 0, 0)))
        c1 = _write(self.tmp, "cap1.m2t",
                    fx.render_capture(self.tape, 20, 50, (2007, 1, 1, 9, 0, 0), damage={35: "cc"}))
        c2 = _write(self.tmp, "cap2.m2t", fx.render_capture(self.tape, 30, 60, (2007, 1, 1, 9, 0, 0)))
        plan = planmod.build_plan(scanmod.analyze([c0, c1, c2]))
        self.assertEqual(plan.unused_sources, [])               # nothing stranded
        self.assertEqual(plan.residuals, [])                    # cap1's damage at tape 35 repaired by cap2
        self.assertEqual({s.tag for s in plan.segments}, {"cap0", "cap1", "cap2"})

    def test_a_spine_capture_s_small_unique_tail_is_not_orphaned(self):
        # The long spine holds a unique tail past an internal dropout it can reach ONLY by re-seed: the
        # walk rides a re-capture across the spine's damaged stretch, that re-capture ends where the
        # spine has nothing tape-adjacent (the dropout), so the walk stops before the tail. The tail is
        # a tiny fraction of the spine, so the old ">=90% covered -> skip re-seed" gate stranded it —
        # the merge silently ended early with no gap or residual flagged. It must be recovered.
        import datetime
        self.tape = fx.simple_tape(100)
        t0 = datetime.datetime(2007, 1, 1, 9, 0, 0)

        def emit(cap, idx, bad=False):
            cap.psi()
            t = t0 + datetime.timedelta(seconds=idx)
            cap.aux((t.year, t.month, t.day, t.hour, t.minute, t.second),
                    tc=(t.minute, t.second, idx % 25))
            cap.gop(fx.gop_es(idx, frames=4), cc_break=bad)

        spine = fx.Capture()
        for idx in range(0, 90):                 # first island, damaged across [85,90)
            emit(spine, idx, bad=(85 <= idx < 90))
        for idx in range(95, 100):               # a unique tail past a dropout the spine itself lacks
            emit(spine, idx)
        s = _write(self.tmp, "spine.m2t", spine.bytes())
        re = _write(self.tmp, "recap.m2t", fx.render_capture(self.tape, 85, 95, (2007, 1, 1, 9, 0, 0)))

        plan = planmod.build_plan(scanmod.analyze([s, re]))
        self.assertEqual(plan.unused_sources, [])
        self.assertEqual(plan.total_frames, 100 * 4)          # whole tape, tail included (not 95*4)
        self.assertTrue(any(sg.tag == "spine" and sg.tc and sg.tc >= "00:01:35:00"
                            for sg in plan.segments), "the spine's unique tail was orphaned")

    def test_byte_identical_recapture_is_not_emitted_twice(self):
        # capA and a byte-identical re-capture of the same tape: the twin adds no new tape and must
        # not be emitted as a duplicate island (it would double the output).
        a = _write(self.tmp, "capA.m2t", fx.render_capture(self.tape, 0, 40, (2007, 1, 1, 9, 0, 0)))
        twin = _write(self.tmp, "capA_twin.m2t",
                      fx.render_capture(self.tape, 0, 40, (2007, 1, 1, 9, 0, 0)))
        plan = planmod.build_plan(scanmod.analyze([a, twin]))
        self.assertEqual(plan.total_frames, 40 * 4)            # the tape once, not twice
        self.assertEqual(len([s for s in plan.segments if s.gap_before]), 0)

    def test_islands_assemble_by_rec_not_tc(self):
        # a re-capture fragment whose recording reset the tape TC (low TC) but sits mid-reel by wall
        # clock must slot into its place by rec — not append at the end by its low TC, which would
        # send the player's clock backwards and truncate the reported duration.
        from hdvmerge.model import Segment

        def seg(tag, rec, tc):
            return Segment(tag=tag, src="x", off=0, end=1, j0=0, j1=0, ngops=1, nbytes=1,
                           rec=rec, tc=tc)
        main = [seg("m", "2011-06-10 09:00:00", "07:30:00:00"),
                seg("m", "2011-06-10 09:10:00", "07:40:00:00")]
        frag = [seg("f", "2011-06-10 09:05:00", "07:05:00:00")]
        segs, unused = planmod._assemble_runs([main, frag])
        self.assertEqual([s.tag for s in segs], ["m", "f", "m"])      # frag slotted in by rec
        self.assertTrue(segs[1].gap_before and segs[2].gap_before)    # island boundaries marked
        self.assertEqual(unused, [])

    def _build(self, plan):
        out = os.path.join(self.tmp, "out.m2t")
        buildmod.build(plan, out)
        return out


if __name__ == "__main__":
    unittest.main()
