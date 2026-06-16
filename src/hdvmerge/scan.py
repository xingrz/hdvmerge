"""Indexing + alignment.

``ensure_index`` builds a capture's per-GOP index (``<capture>.idx.jsonl``, beside the source or
in a chosen ``cache_dir``) and is the only expensive step. It is **cached and idempotent**: a
content fingerprint (size + a hash of the head and tail) decides whether the source changed; if
not, the existing index is reused untouched, and re-indexing the same content always writes a
byte-identical file. Caching can be turned off entirely (``use_cache=False``). ``analyze``
ensures every input's index, then aligns them onto one tape axis by content hash — cheap, always
re-derived. Sources stay read-only.

Damage flags recorded here are TS-level (continuity breaks, TEI). The decode pass (see
:mod:`probe`) adds intra-frame `dec` flags via ffmpeg; its result is cached in the index too, so
an unchanged file is never decoded twice. ffmpeg is used for detection only.
"""

import os
import bisect
import hashlib
from collections import Counter, defaultdict

from . import TS
from .psi import find_pids
from .gop import GopSplitter, parse_pts
from .auxpack import parse_aux
from .model import FileIndex, Report, INDEX_VERSION, index_path, save_index, load_index


def fingerprint(path):
    """Content signature: ``size:hash(head+tail)``. Cheap, deterministic — drives idempotent
    change detection without storing mtimes."""
    size = os.path.getsize(path)
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        h.update(f.read(4 << 20))
        if size > 8 << 20:
            f.seek(-(4 << 20), os.SEEK_END)
            h.update(f.read(4 << 20))
    return "%d:%s" % (size, h.hexdigest())


def scan_file(path, on_progress=None):
    """Index one capture into a FileIndex (decoded=False, fingerprint unset). None if not a TS
    with MPEG video."""
    framing, vpid, apid, _pmt = find_pids(path)
    if framing is None or vpid is None:
        return None
    stride = framing["stride"]
    size = os.path.getsize(path)
    split = GopSplitter()
    feed = split.feed
    aux = []
    last_cc = None
    pending_disc = False     # set by an AF-only video disc marker: the next CC jump is signalled
    fps = 25.0
    READ = stride * 65536
    with open(path, "rb") as f:
        chunk_off = framing["first_sync"]
        f.seek(chunk_off)
        while True:
            chunk = f.read(READ)
            if not chunk:
                break
            limit = len(chunk) - TS
            base = 0
            while base <= limit:
                if chunk[base] != 0x47:
                    base += 1
                    continue
                b1 = chunk[base + 1]
                p = ((b1 & 0x1F) << 8) | chunk[base + 2]
                if p == vpid:
                    b3 = chunk[base + 3]
                    afc = (b3 >> 4) & 0x3
                    this_disc = afc >= 2 and chunk[base + 4] and (chunk[base + 5] & 0x80)
                    if afc & 1:
                        ps = base + 4 if afc == 1 else base + 5 + chunk[base + 4]
                        if ps < base + TS:
                            cc_now = b3 & 0x0F
                            disc = this_disc or pending_disc
                            cc_err = 1 if (last_cc is not None and not disc
                                           and cc_now != ((last_cc + 1) & 0x0F)) else 0
                            last_cc = cc_now
                            pending_disc = False
                            pusi = b1 & 0x40
                            payload = chunk[ps:base + TS]
                            pts = parse_pts(payload) if pusi else None
                            feed(chunk_off + base, 1 if pusi else 0, payload,
                                 1 if (b1 & 0x80) else 0, cc_err, pts)
                    elif this_disc:
                        pending_disc = True       # AF-only disc marker (e.g. a gap); arms next CC check
                elif apid is not None and p == apid and (b1 & 0x40):
                    b3 = chunk[base + 3]
                    afc = (b3 >> 4) & 0x3
                    if afc & 1:
                        ps = base + 4 if afc == 1 else base + 5 + chunk[base + 4]
                        if ps < base + TS:
                            rec, tc = parse_aux(chunk[ps:base + TS])
                            if rec:
                                aux.append((chunk_off + base, rec, tc))
                base += stride
            split.flush()
            chunk_off += len(chunk)
            if on_progress:
                on_progress(chunk_off - framing["first_sync"], size)
    gops = split.finalize(size)
    for g in gops:
        g["dec"] = 0
    _attach_rec(gops, aux)
    return FileIndex(tag=os.path.splitext(os.path.basename(path))[0], size=size,
                     fingerprint="", video_pid=vpid, aux_pid=apid, fps=fps,
                     decoded=False, gops=gops)


def _attach_rec(gops, aux):
    """Attach to each GOP the recording time (``rec``) and tape timecode (``tc``) of the AUX
    packet nearest its start offset. AUX entries are ``(offset, rec, tc)``."""
    if not aux:
        for g in gops:
            g["rec"] = None
            g["tc"] = None
        return
    aoff = [a[0] for a in aux]
    for g in gops:
        k = bisect.bisect_left(aoff, g["off"])
        cand = [x for x in (k - 1, k) if 0 <= x < len(aux)]
        best = min((aux[x] for x in cand), key=lambda a: abs(a[0] - g["off"]))
        g["rec"], g["tc"] = best[1], best[2]


def _decode(idx, path):
    from . import probe
    if not probe.have_ffmpeg():
        raise RuntimeError("decode detection needs ffmpeg on PATH (it is used for detection only)")
    errs, _container = probe.decode_errors(path, idx.gops)   # demuxer timestamp msgs aren't GOP damage
    for g in idx.gops:
        g["dec"] = errs.get(g["i"], 0)
    idx.decoded = True


def ensure_index(path, decode=False, force=False, cache_dir=None, use_cache=True,
                 on_progress=None, on_file=None):
    """Return the FileIndex for ``path``, building/refreshing its index only if the source
    content changed (or ``force``). Caches the decode pass too. ``idx.src_path`` carries the
    absolute source path for the build (not persisted).

    The cache lives at ``<path>.idx.jsonl`` by default, or under ``cache_dir`` (keyed by
    basename) when given. With ``use_cache=False`` no cache is read or written — the index is
    built fresh in memory every run."""
    ip = index_path(path, cache_dir)
    fp = fingerprint(path)
    if use_cache and not force and os.path.exists(ip):
        idx = load_index(ip)
        if idx.fingerprint == fp and idx.version == INDEX_VERSION:
            note = "cached"
            if decode and not idx.decoded:
                _decode(idx, path)
                save_index(idx, ip)
                note = "cached + decode"
            idx.src_path = os.path.abspath(path)
            if on_file:
                on_file(idx, cached=True, note=note)
            return idx
    idx = scan_file(path, on_progress=on_progress)
    if idx is None:
        if on_file:
            on_file(None, cached=False, path=path)
        return None
    idx.fingerprint = fp
    if decode:
        _decode(idx, path)
    if use_cache:
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        save_index(idx, ip)
    idx.src_path = os.path.abspath(path)
    if on_file:
        on_file(idx, cached=False)
    return idx


def align(sources):
    """Greedy, indel-tolerant anchoring onto one tape axis (GOP units) by content hash. Auto-
    derives the chain order. Returns ``(chain, shifts, gaps)``."""
    F = sorted(sources, key=lambda s: s.tag)
    if not F:
        return [], {}, []
    gcount = Counter(g["h"] for s in F for g in s.gops)
    AMBIG = 4
    pos = defaultdict(list)
    shifts = {}

    def vote(s):
        v = Counter()
        for j, g in enumerate(s.gops):
            if gcount[g["h"]] > AMBIG:
                continue
            for gi in pos.get(g["h"], ()):
                v[gi - j] += 1
        if not v:
            return None, 0
        return v.most_common(1)[0]

    def place(s, sh):
        shifts[s.tag] = sh
        for j, g in enumerate(s.gops):
            pos[g["h"]].append(j + sh)

    place(F[0], 0)
    remaining = F[1:]
    while remaining:
        best = None
        for s in remaining:
            sh, c = vote(s)
            if sh is not None and (best is None or c > best[2]):
                best = (s, sh, c)
        if best is None:
            gmax = max(max(v) for v in pos.values())
            for s in remaining:
                place(s, gmax + 1)
                gmax += len(s.gops)
            break
        s, sh, _c = best
        place(s, sh)
        remaining.remove(s)

    gmin = min(shifts.values())
    for t in shifts:
        shifts[t] -= gmin
    chain = sorted((s.tag for s in F), key=lambda t: shifts[t])
    covered = set()
    for s in F:
        sh = shifts[s.tag]
        covered.update(range(sh, sh + len(s.gops)))
    lo, hi = min(covered), max(covered)
    gaps = []
    i = lo
    while i <= hi:
        if i not in covered:
            j = i
            while j <= hi and j not in covered:
                j += 1
            gaps.append([i, j - 1])
            i = j
        else:
            i += 1
    return chain, shifts, gaps


def analyze(paths, decode=False, force=False, cache_dir=None, use_cache=True,
            on_progress=None, on_file=None):
    """Ensure every input's index (cached/idempotent), then align. Returns an in-memory Report."""
    sources = []
    for p in paths:
        idx = ensure_index(p, decode=decode, force=force, cache_dir=cache_dir,
                           use_cache=use_cache, on_progress=on_progress, on_file=on_file)
        if idx is not None:
            sources.append(idx)
    chain, shifts, gaps = align(sources)
    return Report(sources=sources, chain=chain, shifts=shifts, gaps=gaps)
