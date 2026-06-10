import unittest

from hdvmerge import ts, gop, aux
from . import fixtures as fx


class TestTs(unittest.TestCase):
    def test_framing_and_fields(self):
        data = fx.render_capture(fx.simple_tape(6), 0, 6, (2009, 4, 30, 17, 0, 0))
        fr = ts.detect_framing(data)
        self.assertEqual(fr["stride"], 188)
        pkt = data[fr["first_sync"]:fr["first_sync"] + 188]
        self.assertEqual(pkt[0], 0x47)
        self.assertEqual(ts.pid(pkt), 0)          # first packet is the PAT
        self.assertTrue(ts.pusi(pkt))
        self.assertTrue(ts.has_payload(pkt))

    def test_with_cc_preserves_bytes(self):
        pkt = fx._pkt(0x200, b"\xab" * 184, pusi=True, ccval=3)
        out = ts.with_cc(pkt, 7)
        self.assertEqual(ts.cc(out), 7)
        self.assertEqual(out[:3], pkt[:3])
        self.assertEqual(out[4:], pkt[4:])        # only the CC nibble changed

    def test_disc_marker(self):
        m = ts.make_disc_marker(0x810)
        self.assertEqual(len(m), 188)
        self.assertEqual(m[0], 0x47)
        self.assertEqual(ts.pid(m), 0x810)
        self.assertEqual(ts.afc(m), 2)            # adaptation-field only, carries no payload
        self.assertIsNone(ts.payload_start(m))    # so it never enters any GOP's ES
        self.assertTrue(ts.disc_indicator(m))     # discontinuity_indicator set


class TestAux(unittest.TestCase):
    def test_rec_roundtrip(self):
        payload = fx.aux_payload(2007, 1, 1, 9, 36, 5)
        self.assertEqual(aux.parse_rec(payload), "2007-01-01 09:36:05")

    def test_rec_rejects_garbage(self):
        self.assertIsNone(aux.parse_rec(b"\x00\x00\x01\xbf\x00\x10" + b"\x55" * 40))
        self.assertEqual(aux.parse_aux(b"\x00\x00\x01\xbf\x00\x10" + b"\x55" * 40), (None, None))

    def test_tape_tc_roundtrip(self):
        # tape TC distinct from the wall clock (hour 07 vs 09) — both from one shared anchor
        payload = fx.aux_payload(2007, 1, 1, 9, 36, 5, tc=(7, 36, 5, 12))
        self.assertEqual(aux.parse_aux(payload), ("2007-01-01 09:36:05", "07:36:05:12"))
        self.assertEqual(aux.parse_tc(payload), "07:36:05:12")

    def test_tc_frame_field_decodes(self):
        # frame field is the last byte of HH FF SS MM and must survive at the 25/30 boundary
        self.assertEqual(aux.parse_tc(fx.aux_payload(2007, 1, 1, 9, 0, 0, tc=(1, 2, 3, 24))),
                         "01:02:03:24")


class TestGopSplitter(unittest.TestCase):
    def test_counts_and_hashes(self):
        sp = gop.GopSplitter()
        es = fx.gop_es(0, frames=4) + fx.gop_es(1, frames=3) + fx.gop_es(2, frames=4)
        off = 0
        for k in range(0, len(es), 184):
            chunk = es[k:k + 184]
            sp.feed(off, k == 0, chunk)
            off += 188
        sp.flush()
        gops = sp.finalize(off)
        self.assertEqual(len(gops), 3)
        self.assertEqual([g["npic"] for g in gops], [4, 3, 4])
        self.assertEqual(len({g["h"] for g in gops}), 3)        # distinct content -> distinct hash
        # same tape GOP rendered again hashes identically
        sp2 = gop.GopSplitter()
        es2 = fx.gop_es(1, frames=3) + fx.gop_es(9, frames=4)
        o = 0
        for k in range(0, len(es2), 184):
            sp2.feed(o, k == 0, es2[k:k + 184]); o += 188
        sp2.flush()
        g2 = sp2.finalize(o)
        self.assertEqual(g2[0]["h"], gops[1]["h"])


if __name__ == "__main__":
    unittest.main()
