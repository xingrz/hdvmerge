"""plan — decide which capture supplies each tape GOP, as byte-range segments. Produces
plan.json. Pure decision-making over report.json; no bytes are written.

Greedy walk along the tape, one GOP at a time:
  * stay on the current capture while its next GOP is clean and away from its head/tail margin
    (fewer seams = safer);
  * when the next GOP is damaged or we near an edge, switch to another capture's clean copy of
    the same tape position, located by content hash so a dropped/duplicated GOP anywhere else
    cannot misalign the seam (the true next GOP is decided by hash majority across copies).

Where two clean copies of the same tape GOP disagree byte-for-byte, one carries intra-frame
damage the TS layer can't flag; these are recorded in ``divergences`` for review (and the plan
is hand-editable). plan.json is the source of truth for ``build``.
"""

from collections import Counter
from datetime import datetime

from .model import Plan, Segment

EDGE = 12              # GOPs near a capture's head/tail to avoid (capture can corrupt edges)
CLEAN_RUN_CAP = 120


def _tc_seconds(tc, fps):
    if not tc:
        return None
    try:
        h, m, s, f = (int(x) for x in tc.replace(";", ":").split(":"))
    except (ValueError, AttributeError):
        return None
    return ((h * 60 + m) * 60 + s) + f / (fps or 25.0)


def _rec_dt(rec):
    if not rec:
        return None
    try:
        return datetime.strptime(rec, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _lost_spans(emitted, fps):
    """Stretches the merged output skips where the recording was actually CONTINUOUS — tape that was
    recorded but is unreadable in **every** capture. Every pass then jumps across the same spot, and
    the hash walk, seeing no GOPs there, wrongly treats the two sides as tape-adjacent (no axis gap),
    so this loss would otherwise go unreported.

    The discriminator is the **rec-run tape TC** vs the camera **wall clock**: across a real lost
    stretch both advance by the same amount (the tape recorded that long); across a *camera stop* the
    wall clock jumps far more than the rec-run TC (which pauses while not recording). So we flag a
    forward TC jump only when the wall clock confirms it. (This is the HDV analogue of dvmerge's
    abst-gap detection.) ``emitted`` is the output GOPs in order, each ``{tc, rec, frame, tag,
    gap_before}``."""
    out = []
    for a, b in zip(emitted, emitted[1:]):
        if b.get("gap_before"):
            continue   # already a signalled island gap (find-back), not an in-walk loss
        ta, tb = _tc_seconds(a["tc"], fps), _tc_seconds(b["tc"], fps)
        ra, rb = _rec_dt(a["rec"]), _rec_dt(b["rec"])
        if ta is None or tb is None or ra is None or rb is None:
            continue
        tc_d = tb - ta
        wall_d = (rb - ra).total_seconds()
        # > 1.5 s is well past a single GOP gap (~0.5 s), so this is several GOPs the output skipped
        if tc_d > 1.5 and wall_d > 0 and abs(tc_d - wall_d) <= max(1.0, 0.34 * tc_d):
            out.append({"frame": b["frame"], "tag": a["tag"],
                        "tc0": a["tc"], "tc1": b["tc"],
                        "rec0": a["rec"], "rec1": b["rec"],
                        "frames": max(1, int(round(tc_d * fps)) - 1)})
    return out


def _prep(report):
    F = {}
    for tag in report.chain:
        s = report.source(tag)
        H = [g["h"] for g in s.gops]
        hpos = {}
        for j, h in enumerate(H):
            hpos.setdefault(h, []).append(j)
        # A GOP is damaged if the TS layer broke (cc/tei) or the decode pass flagged it (dec). The
        # build re-phases CC so clean seams carry no break and ffmpeg decodes straight through them,
        # so there is no seam over-report to discount — a decode flag is genuine single-copy damage.
        bad = [(g["cc"] > 0 or g["tei"] > 0 or g.get("dec", 0) > 0) for g in s.gops]
        F[tag] = {"s": s, "gops": s.gops, "n": len(s.gops), "H": H, "hpos": hpos, "bad": bad}
    return F


def build_plan(report):
    chain = report.chain
    if not chain:
        return Plan(segments=[])
    fps = report.source(chain[0]).fps or 25.0
    F = _prep(report)

    def locate(Q, rt, rj):
        h = F[rt]["H"][rj]
        cs = F[Q]["hpos"].get(h)
        if not cs:
            return None
        if len(cs) == 1:
            return cs[0]
        ph = F[rt]["H"][rj - 1] if rj > 0 else None
        ctx = [c for c in cs if c > 0 and F[Q]["H"][c - 1] == ph]
        return min(ctx or cs, key=lambda c: abs(c - rj))

    def clean_run(Q, cj):
        fo = F[Q]
        k = 0
        while cj + k < fo["n"] and not fo["bad"][cj + k] and k < CLEAN_RUN_CAP:
            k += 1
        return k

    def score(cand, t):
        Q, cj = cand
        fo = F[Q]
        return (1 if fo["bad"][cj] else 0,
                1 if min(cj, fo["n"] - 1 - cj) < EDGE else 0,
                0 if Q == t else 1,
                -clean_run(Q, cj))

    out = [(chain[0], 0)]
    divergences = []
    frame = 0
    while True:
        t, j = out[-1]
        same = (t, j + 1) if j + 1 < F[t]["n"] else None
        cands = []
        if same:
            cands.append(same)
        for Q in chain:
            if Q == t:
                continue
            c = locate(Q, t, j)
            if c is not None and c + 1 < F[Q]["n"]:
                cands.append((Q, c + 1))
        if not cands:
            break
        # record a divergence: two clean INTERIOR copies of the same next tape GOP that disagree
        # byte-wise. Edge GOPs are excluded — a capture's first/last GOP has a structurally
        # different hash (its ES slice runs to a file boundary, not the next GOP), which is not
        # content damage, and edges are avoided by the walk anyway.
        clean = [c for c in cands if not F[c[0]]["bad"][c[1]]]
        interior = [c for c in clean if min(c[1], F[c[0]]["n"] - 1 - c[1]) >= EDGE]
        if len({F[Q]["H"][cj] for Q, cj in interior}) > 1:
            frame_next = frame + F[t]["gops"][j]["npic"]
            divergences.append({
                "frame": frame_next,
                "rec": F[interior[0][0]]["gops"][interior[0][1]].get("rec"),
                "tc": F[interior[0][0]]["gops"][interior[0][1]].get("tc"),
                "copies": [{"tag": Q, "gop": cj, "h": F[Q]["H"][cj]} for Q, cj in interior],
            })
        # decide the true next tape GOP: trust same-file contiguity when it is clean, else the
        # hash held by the most clean copies
        if same is not None and not F[t]["bad"][same[1]]:
            true_h = F[t]["H"][same[1]]
        else:
            pool = clean or cands
            true_h = Counter(F[Q]["H"][cj] for Q, cj in pool).most_common(1)[0][0]
        group = [c for c in cands if F[c[0]]["H"][c[1]] == true_h]
        cleang = [c for c in group if not F[c[0]]["bad"][c[1]]]
        nxt = min(cleang or group, key=lambda c: score(c, t))
        frame += F[t]["gops"][j]["npic"]
        out.append(nxt)

    # adjacency sanity: every cross-file seam must be tape-adjacent
    bad_seams = 0
    for k in range(1, len(out)):
        (pt, pj), (ct, cj) = out[k - 1], out[k]
        if pt != ct and cj > 0 and F[ct]["H"][cj - 1] != F[pt]["H"][pj]:
            bad_seams += 1

    # coalesce into byte-range segments
    segs = []
    for (t, j) in out:
        if segs and segs[-1].tag == t and j == segs[-1].j1 + 1:
            segs[-1].j1 = j
        else:
            segs.append(Segment(tag=t, src=F[t]["s"].src_path, off=0, end=0, j0=j, j1=j,
                                 ngops=0, nbytes=0))
    for sg in segs:
        g = F[sg.tag]["gops"]
        sg.off = g[sg.j0]["off"]
        sg.end = g[sg.j1]["end"]
        sg.ngops = sg.j1 - sg.j0 + 1
        sg.nbytes = sg.end - sg.off
        sg.rec = g[sg.j0].get("rec")
        sg.rec_end = g[sg.j1].get("rec")
        sg.tc = g[sg.j0].get("tc")
        sg.tc_end = g[sg.j1].get("tc")

    # find-back: a source the greedy walk never reached is a separate tape island (no overlapping
    # GOP to bridge it by hash). Stitch each back in by *tape TC* — but only when the tape TC AND
    # the wall-clock both agree it sits as one disjoint block cleanly before or after the assembled
    # chain (so placement is reliable); otherwise leave it flagged in `unused`. A stitched island is
    # a real discontinuity, so it carries `gap_before` (build marks it and never re-phases across it).
    segs, unused = _stitch_islands(segs, chain, F)

    # residuals: emitted GOPs still damaged (no clean copy anywhere). `emitted_cc/tei` sum the
    # TS-level breaks the build copies out — the output self-check expects exactly these and no more
    # (re-phasing must introduce no new break at any seam).
    residuals = []
    emitted = []
    emitted_cc = emitted_tei = 0
    frame = 0
    for sg in segs:
        sg.frame0 = frame
        fo = F[sg.tag]
        g = fo["gops"]
        for j in range(sg.j0, sg.j1 + 1):
            emitted_cc += g[j]["cc"]
            emitted_tei += g[j]["tei"]
            emitted.append({"tc": g[j].get("tc"), "rec": g[j].get("rec"), "frame": frame,
                            "tag": sg.tag, "gap_before": (j == sg.j0 and sg.gap_before)})
            if fo["bad"][j]:
                residuals.append({"frame": frame, "rec": g[j].get("rec"), "tc": g[j].get("tc"),
                                  "tag": sg.tag, "gop": j, "cc": g[j]["cc"], "tei": g[j]["tei"],
                                  "dec": g[j].get("dec", 0)})
            frame += g[j]["npic"]

    return Plan(segments=segs, residuals=residuals, divergences=divergences,
                gaps=report.gaps, total_frames=frame, fps=fps, bad_seams=bad_seams,
                emitted_cc=emitted_cc, emitted_tei=emitted_tei, unused_sources=unused,
                video_pid=report.source(chain[0]).video_pid, lost=_lost_spans(emitted, fps))


def _stitch_islands(segs, chain, F):
    """Place walk-unreached sources back into ``segs`` by tape TC. Returns ``(segs, unused)``: the
    placed islands carry ``gap_before``; sources that can't be placed reliably stay in ``unused``."""
    used = {sg.tag for sg in segs}
    main_tc = [s.tc for s in segs if s.tc] + [s.tc_end for s in segs if s.tc_end]
    main_rec = [s.rec for s in segs if s.rec] + [s.rec_end for s in segs if s.rec_end]
    islands, unused = [], []
    for t in chain:
        if t in used:
            continue
        gp = F[t]["gops"]
        entry = {"tag": t, "ngops": len(gp), "frames": sum(g["npic"] for g in gp),
                 "tc0": gp[0].get("tc"), "tc1": gp[-1].get("tc"),
                 "rec0": gp[0].get("rec"), "rec1": gp[-1].get("rec")}
        tcs = [g["tc"] for g in gp if g.get("tc")]
        recs = [g["rec"] for g in gp if g.get("rec")]
        placed = False
        if main_tc and main_rec and tcs and recs:
            tlo, thi, rlo, rhi = min(tcs), max(tcs), min(recs), max(recs)
            mtlo, mthi, mrlo, mrhi = min(main_tc), max(main_tc), min(main_rec), max(main_rec)
            after = tlo > mthi and rlo > mrhi
            before = thi < mtlo and rhi < mrlo
            clash = any(not (thi < s.tc or tlo > s.tc_end) for _, s in islands)  # overlaps a placed island
            if (after or before) and not clash:
                islands.append((tlo, Segment(
                    tag=t, src=F[t]["s"].src_path, off=gp[0]["off"], end=gp[-1]["end"],
                    j0=0, j1=len(gp) - 1, ngops=len(gp), nbytes=gp[-1]["end"] - gp[0]["off"],
                    rec=gp[0].get("rec"), rec_end=gp[-1].get("rec"),
                    tc=gp[0].get("tc"), tc_end=gp[-1].get("tc"), gap_before=True)))
                placed = True
        if not placed:
            unused.append(entry)
    if not islands:
        return segs, unused
    runs = [(min(main_tc), segs)] + [(tlo, [seg]) for tlo, seg in islands]
    runs.sort(key=lambda r: r[0])
    out = []
    for i, (_, rsegs) in enumerate(runs):
        if i:
            rsegs[0].gap_before = True
        out.extend(rsegs)
    return out, unused
