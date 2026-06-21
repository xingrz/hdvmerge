"""plan — decide which capture supplies each tape GOP, as byte-range segments. Produces
plan.json. Pure decision-making over report.json; no bytes are written.

Greedy walk along the tape, one GOP at a time:
  * stay on the current capture while its next GOP is clean and away from its head/tail margin
    (fewer seams = safer);
  * when the next GOP is damaged or we near an edge, switch to another capture's clean copy of
    the same tape position, located by content hash so a dropped/duplicated GOP anywhere else
    cannot misalign the seam (the true next GOP is decided by hash majority across copies).

The captures can split into several tape islands with no hash bridge between them (damage/indels,
or genuinely disjoint takes), so the walk is run once per island — seeded from each capture whose
tape is not already covered — and the islands are stitched together by tape TC. This keeps the
result independent of which file sorts first: a stray clip can no longer strand the rest, and each
island repairs itself internally from every overlapping copy.

Where two clean copies of the same tape GOP disagree byte-for-byte, one carries intra-frame
damage the TS layer can't flag; these are recorded in ``divergences`` for review (and the plan
is hand-editable). plan.json is the source of truth for ``build``.
"""

from collections import Counter
from datetime import datetime

from .model import Plan, Segment
from .ts import detect_framing

EDGE = 12              # GOPs near a capture's head/tail to avoid (capture can corrupt edges)
CLEAN_RUN_CAP = 120


def _first_pcr(buf):
    """First PCR (seconds) in a TS buffer, or None. The PCR is the tape's own clock, carried verbatim
    by every capture and monotone along the whole tape — the reliable coordinate for ordering islands
    when the wall-clock `rec` is wrong (a mis-set camera date) and tape TC resets per recording."""
    fr = detect_framing(buf[:1 << 16])
    if fr is None:
        return None
    st = fr["stride"]
    base = None
    for s in range(min(st * 2, max(0, len(buf) - st * 6))):
        if all(buf[s + k * st] == 0x47 for k in range(6)):   # 6 packets in step = a true sync lock
            base = s
            break
    if base is None:
        return None
    for p in range(base, len(buf) - st, st):
        if buf[p] != 0x47:
            break
        if (buf[p + 3] >> 4) & 3 in (2, 3) and buf[p + 4] >= 7 and (buf[p + 5] & 0x10):
            b = buf[p + 6:p + 12]
            return ((b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)) / 90000.0
    return None


def _segment_pcr(seg):
    try:
        with open(seg.src, "rb") as f:
            f.seek(seg.off)
            return _first_pcr(f.read(1 << 18))
    except OSError:
        return None


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
    gap_before}``.

    This is evaluated at EVERY step, including an island boundary (``gap_before``). Where footage is
    missing in every capture the walk cannot bridge the two sides, so they end up as separate islands
    stitched only by TC — there is no GOP between them and no axis gap, so unless it is caught here the
    loss is invisible to the JSON consumer (it shows only as a ⟂ row in the Markdown). The same
    discriminator keeps this honest: a genuinely tape-adjacent island join steps by ~one GOP and never
    trips the > 1.5 s test, and a camera-pause / session-reset boundary fails the wall-clock check."""
    out = []
    hwm = None   # highest tape TC reached in the output so far (seconds) — the assembly frontier
    for a, b in zip(emitted, emitted[1:]):
        ta, tb = _tc_seconds(a["tc"], fps), _tc_seconds(b["tc"], fps)
        if ta is not None:
            hwm = ta if hwm is None else max(hwm, ta)
        ra, rb = _rec_dt(a["rec"]), _rec_dt(b["rec"])
        if ta is None or tb is None or ra is None or rb is None:
            continue
        # Only a jump that departs from the FRONTIER can bound a real loss. The PCR merge works at
        # segment granularity, so a small island whose tape position sits inside a larger segment's
        # span is emitted *after* that segment — behind the high-water mark. The stretch "after" such
        # a backfill fragment was already emitted earlier, so the forward jump off it is not missing
        # footage; skip anything starting more than a GOP behind the frontier.
        if hwm is not None and ta < hwm - 1.0:
            continue
        tc_d = tb - ta
        wall_d = (rb - ra).total_seconds()
        # A forward record-run TC jump > 1.5 s (several GOPs, well past a ~0.5 s single-GOP gap) means
        # that many seconds of *physical tape* sit between the two sides — recorded footage no capture
        # holds. The wall clock only has to CONFIRM that real time elapsed; it may be much LARGER than
        # tc_d when the original recording paused across the stretch (43 s of tape shot over 2 min of
        # wall time is still 43 s of missing tape). Reject only when the wall clock is inconsistent —
        # it barely moved or ran backwards while the TC ran on (a TC glitch / mis-set date, not real
        # tape). A genuine camera stop never reaches here: record-run TC pauses while not recording, so
        # tc_d ~ 0 and the > 1.5 s test already excludes it.
        if tc_d > 1.5 and wall_d > 0 and tc_d - wall_d <= max(1.0, 0.34 * tc_d):
            out.append({"frame": b["frame"], "tag": a["tag"],
                        "tc0": a["tc"], "tc1": b["tc"],
                        "rec0": a["rec"], "rec1": b["rec"],
                        "frames": max(1, int(round(tc_d * fps)) - 1)})
    return out


def _prep(report):
    # `dec` (ffmpeg decode pass) over-reports: it cascades from a lost reference frame onto later,
    # byte-CLEAN GOPs, and isn't deterministic across ffmpeg builds (a GOP can read clean on one machine
    # and `dec` on another). So a `dec` flag is discredited when the byte-identical GOP is clean in some
    # capture: identical ES bytes (same hash) decode to an identical picture, so the flag is a
    # capture-local decoder-state artifact, not damage. probe.py already intends this ("selected by hash
    # regardless of its dec flag"); enforcing it here keeps a clean twin from being stranded as a
    # residual when the walk can't hash-locate it, and makes the result robust to ffmpeg variance. Real
    # intra-GOP damage changes the bytes -> a unique hash absent from clean_h -> never discredited. CC
    # and TEI are deterministic TS-layer parsing (a real packet loss changes the bytes too) and always
    # count.
    clean_h = {g["h"] for tag in report.chain for g in report.source(tag).gops
               if g["cc"] == 0 and g["tei"] == 0 and g.get("dec", 0) == 0}
    F = {}
    for tag in report.chain:
        s = report.source(tag)
        H = [g["h"] for g in s.gops]
        hpos = {}
        for j, h in enumerate(H):
            hpos.setdefault(h, []).append(j)
        bad = [(g["cc"] > 0 or g["tei"] > 0
                or (g.get("dec", 0) > 0 and g["h"] not in clean_h))
               for g in s.gops]
        F[tag] = {"s": s, "gops": s.gops, "n": len(s.gops), "H": H, "hpos": hpos, "bad": bad}
    return F


def build_plan(report):
    chain = report.chain
    if not chain:
        return Plan(segments=[])
    fps = report.source(chain[0]).fps or 25.0
    F = _prep(report)
    sum_n = sum(F[t]["n"] for t in chain)

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

    covered = set()    # clean GOP hashes already emitted, so a later island can't re-emit that tape
    div_at = {}        # emitted (tag, gop) -> divergence info; output frame is filled in at assembly

    def walk(start, start_j):
        out = [(start, start_j)]
        while len(out) <= sum_n:
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
            # a divergence: two clean INTERIOR copies of the same next tape GOP that disagree
            # byte-wise. Edge GOPs are excluded — a capture's first/last GOP has a structurally
            # different hash (its ES slice runs to a file boundary, not the next GOP), which is not
            # content damage, and edges are avoided by the walk anyway.
            clean = [c for c in cands if not F[c[0]]["bad"][c[1]]]
            interior = [c for c in clean if min(c[1], F[c[0]]["n"] - 1 - c[1]) >= EDGE]
            # decide the true next tape GOP: trust same-file contiguity when it is clean, else the
            # hash held by the most clean copies
            if same is not None and not F[t]["bad"][same[1]]:
                true_h = F[t]["H"][same[1]]
                # ...but don't ride this file across its OWN dropout at a divergence: when clean
                # interior copies disagree on the next GOP and the same-file hash is a strict minority,
                # the same-file "next" is a post-dropout resync that skips tape the majority still
                # holds. Follow the majority so those frames emit here, in order — otherwise the walk
                # jumps the hole, the held frames surface only as an out-of-tape-order backfill island,
                # and _lost_spans wrongly flags the jump "recorded but unreadable in every capture". A
                # byte-identical re-encode shares one hash (no divergence), so same-file trust stands.
                if interior:
                    counts = Counter(F[Q]["H"][cj] for Q, cj in interior)
                    maj_h, maj_n = counts.most_common(1)[0]
                    if maj_h != true_h and counts.get(true_h, 0) < maj_n:
                        true_h = maj_h
            else:
                pool = clean or cands
                true_h = Counter(F[Q]["H"][cj] for Q, cj in pool).most_common(1)[0][0]
            group = [c for c in cands if F[c[0]]["H"][c[1]] == true_h]
            cleang = [c for c in group if not F[c[0]]["bad"][c[1]]]
            nxt = min(cleang or group, key=lambda c: score(c, t))
            # stop where the walk re-enters tape an earlier island already emitted (no duplicates);
            # any tape beyond it that is still uncovered will be picked up by its own seed
            if not F[nxt[0]]["bad"][nxt[1]] and F[nxt[0]]["H"][nxt[1]] in covered:
                break
            if len({F[Q]["H"][cj] for Q, cj in interior}) > 1:
                src = interior[0]
                div_at[nxt] = {"rec": F[src[0]]["gops"][src[1]].get("rec"),
                               "tc": F[src[0]]["gops"][src[1]].get("tc"),
                               "copies": [{"tag": Q, "gop": cj, "h": F[Q]["H"][cj]}
                                          for Q, cj in interior]}
            out.append(nxt)
        return out

    # Cover EVERY tape island, not just chain[0]'s. A single walk from one seed only ever covers that
    # seed's island, so whichever capture sorted first used to decide the whole result and could
    # strand all the rest (their clean copies then repaired no one, and a stray clip could silently
    # drop a whole reel). Instead seed a walk from each capture whose tape is not already covered — a
    # byte-identical re-capture (>=90% covered) is skipped so it isn't emitted as a twin island, and a
    # walk stops where it re-enters covered tape, so nothing is emitted twice. Order is by `chain`
    # (shift order) only for determinism; coverage no longer depends on which capture seeds first.
    #
    # Re-seed until coverage stops growing, NOT just once per capture: a single walk can stop partway
    # through a capture — it rides a short clean copy to that copy's end and then can't hop back onto
    # the only remaining capture because damage there changed its GOP hash (the hash-locate fails). The
    # rest of that capture's footage would otherwise be orphaned: not emitted (a silent hole in the
    # result) and not flagged for re-capture (no emitted GOP there means no residual). Each pass walks
    # from each capture's first STILL-uncovered clean GOP; a walk always emits at least that seed GOP,
    # so `covered` grows every pass until nothing is left uncovered, then we stop.
    runs = []
    while True:
        seeded = False
        for seed in chain:
            fo = F[seed]
            # Seed from the longest contiguous run of still-uncovered CLEAN GOPs. What matters is
            # whether a capture holds real footage no walk has emitted yet — NOT what fraction of the
            # capture that is. The old gate ("re-seed only if >10% of the capture is still uncovered")
            # silently stranded a large spine capture's small unique tail: e.g. a minute of footage
            # past the last find-back is ~2% of an hour-long capture, so it was never re-seeded, never
            # emitted, and never flagged (no emitted GOP there -> no residual/lost) — the merged file
            # just ended early. A near-duplicate re-capture, by contrast, leaves only its
            # structurally-different file-edge GOP uncovered (a length-1 run), so require a run of >=2
            # to avoid emitting that edge as a spurious twin island.
            best_j = best_len = cur_j = cur_len = 0
            for j in range(fo["n"]):
                if not fo["bad"][j] and fo["H"][j] not in covered:
                    if cur_len == 0:
                        cur_j = j
                    cur_len += 1
                    if cur_len > best_len:
                        best_len, best_j = cur_len, cur_j
                else:
                    cur_len = 0
            if best_len < 2:
                continue
            path = walk(seed, best_j)
            for (t, j) in path:
                if not F[t]["bad"][j]:
                    covered.add(F[t]["H"][j])
            runs.append(path)
            seeded = True
        if not seeded:
            break

    def coalesce(path):
        rs = []
        for (t, j) in path:
            if rs and rs[-1].tag == t and j == rs[-1].j1 + 1:
                rs[-1].j1 = j
            else:
                rs.append(Segment(tag=t, src=F[t]["s"].src_path, off=0, end=0, j0=j, j1=j,
                                  ngops=0, nbytes=0))
        for sg in rs:
            g = F[sg.tag]["gops"]
            sg.off = g[sg.j0]["off"]
            sg.end = g[sg.j1]["end"]
            sg.ngops = sg.j1 - sg.j0 + 1
            sg.nbytes = sg.end - sg.off
            sg.rec = g[sg.j0].get("rec")
            sg.rec_end = g[sg.j1].get("rec")
            sg.tc = g[sg.j0].get("tc")
            sg.tc_end = g[sg.j1].get("tc")
        return rs

    segs, unused = _assemble_runs([coalesce(p) for p in runs])

    # adjacency sanity: every cross-file seam that is NOT an island boundary must be tape-adjacent
    bad_seams = 0
    for k in range(1, len(segs)):
        prev, cur = segs[k - 1], segs[k]
        if cur.gap_before:
            continue
        if cur.j0 > 0 and F[cur.tag]["H"][cur.j0 - 1] != F[prev.tag]["H"][prev.j1]:
            bad_seams += 1

    # residuals (emitted GOPs still damaged — no clean copy anywhere), divergences resolved to their
    # output frame, and `emitted_cc/tei` (the TS-level breaks the build copies out), walking the
    # assembled segments in output order.
    residuals = []
    divergences = []
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
            d = div_at.get((sg.tag, j))
            if d is not None:
                divergences.append({"frame": frame, "rec": d["rec"], "tc": d["tc"],
                                    "copies": d["copies"]})
            if fo["bad"][j]:
                residuals.append({"frame": frame, "rec": g[j].get("rec"), "tc": g[j].get("tc"),
                                  "tag": sg.tag, "gop": j, "cc": g[j]["cc"], "tei": g[j]["tei"],
                                  "dec": g[j].get("dec", 0)})
            frame += g[j]["npic"]

    return Plan(segments=segs, residuals=residuals, divergences=divergences,
                gaps=report.gaps, total_frames=frame, fps=fps, bad_seams=bad_seams,
                emitted_cc=emitted_cc, emitted_tei=emitted_tei, unused_sources=unused,
                video_pid=report.source(chain[0]).video_pid, lost=_lost_spans(emitted, fps))


def _assemble_runs(run_segs):
    """Interleave the per-island walk runs into one tape-ordered sequence by the **PCR** (the tape's
    own clock), so the merged file's timeline is monotone end to end.

    Neither of the easy coordinates works: tape TC resets per recording, and the wall-clock `rec` can
    be plain wrong (a capture made with a mis-set camera date sorts to the wrong place). The PCR is
    carried verbatim on the tape and increases along the whole reel regardless, so a re-capture
    fragment drops into its true position. A k-way merge keeps every run internally ordered (the walk
    order is authoritative) and emits whichever run's head GOP has the earliest PCR. A run change (an
    island boundary) carries ``gap_before`` — a real discontinuity the build marks and never
    re-phases across. ``rec`` is the fallback when a stream carries no PCR. Nothing is stranded, so
    ``unused`` stays empty."""
    runs = [r for r in run_segs if r]
    pcr = {id(s): _segment_pcr(s) for r in runs for s in r}
    use_pcr = all(pcr[id(s)] is not None for r in runs for s in r)

    def head_key(ri):
        s = runs[ri][pos[ri]]
        if use_pcr:
            return (pcr[id(s)],)
        return (s.rec is None, s.rec or "", s.tc or "")

    pos = [0] * len(runs)
    segs = []
    prev = None
    while True:
        avail = [ri for ri in range(len(runs)) if pos[ri] < len(runs[ri])]
        if not avail:
            break
        ri = min(avail, key=head_key)
        s = runs[ri][pos[ri]]
        pos[ri] += 1
        s.gap_before = prev is not None and ri != prev
        segs.append(s)
        prev = ri
    return segs, []
