"""Data model.

The only persisted artifact is the per-capture **index** (``<capture>.idx.jsonl``): the
expensive-to-compute GOP table, cached next to its source and rebuilt only when the source
content changes. It is JSONL — a meta line followed by one line per GOP — which suits a header
plus thousands of homogeneous records (greppable, ``wc -l`` ≈ GOP count) and is written with
sorted keys and no timestamps / absolute paths, so the same source content always yields a
byte-identical index (idempotent).

Everything else — the cross-capture alignment (:class:`Report`), the byte-range plan
(:class:`Plan`) and the human report — is cheap to re-derive from the indices and is never
persisted as a structured file. The merged ``.m2t`` and the Markdown report are the outputs.
"""

from dataclasses import dataclass, field
from typing import Optional
import json
import os

INDEX_VERSION = 5   # v2: tape timecode (`tc`); v4: dropped the seam flag; v5: tape TC no longer
#                     mis-reads the 0x07 status byte as hours (was a spurious 07:). Older caches rebuild.

# A per-GOP record is a plain dict: i off end nbytes npic closed broken pts h cc tei dec rec tc


@dataclass
class FileIndex:
    tag: str                       # source basename (no directory) — keeps the index portable
    size: int
    fingerprint: str               # content signature for idempotent change detection
    video_pid: int
    aux_pid: Optional[int]
    fps: float
    decoded: bool                  # whether the ffmpeg decode pass has filled `dec`
    gops: list = field(default_factory=list)
    version: int = INDEX_VERSION   # schema version of a loaded cache; a mismatch forces a rebuild

    @property
    def path_hint(self):
        return self.tag


def index_path(source_path, cache_dir=None):
    """Where the index cache lives. Beside the capture by default; in ``cache_dir`` (keyed by
    basename) when one is given, so a directory of indices can sit apart from read-only sources."""
    if cache_dir:
        return os.path.join(cache_dir, os.path.basename(source_path) + ".idx.jsonl")
    return source_path + ".idx.jsonl"


def save_index(idx: FileIndex, path):
    meta = {"v": INDEX_VERSION, "tag": idx.tag, "size": idx.size,
            "fingerprint": idx.fingerprint, "video_pid": idx.video_pid,
            "aux_pid": idx.aux_pid, "fps": idx.fps, "decoded": idx.decoded,
            "ngops": len(idx.gops)}
    tmp = path + ".part"
    with open(tmp, "w") as f:
        f.write(json.dumps(meta, sort_keys=True) + "\n")
        for g in idx.gops:
            f.write(json.dumps(g, sort_keys=True) + "\n")
    os.replace(tmp, path)


def load_index(path) -> FileIndex:
    with open(path) as f:
        meta = json.loads(f.readline())
        gops = [json.loads(line) for line in f if line.strip()]
    return FileIndex(tag=meta["tag"], size=meta["size"], fingerprint=meta["fingerprint"],
                     video_pid=meta["video_pid"], aux_pid=meta["aux_pid"], fps=meta["fps"],
                     decoded=meta["decoded"], gops=gops, version=meta.get("v", 1))


# --- in-memory only (derived fresh from the indices every run) ---

@dataclass
class Report:
    sources: list                  # list[FileIndex]
    chain: list = field(default_factory=list)        # tags in tape order
    shifts: dict = field(default_factory=dict)        # tag -> axis shift (GOP units)
    gaps: list = field(default_factory=list)          # [[axis_lo, axis_hi], ...]

    def source(self, tag):
        for s in self.sources:
            if s.tag == tag:
                return s
        return None


@dataclass
class Segment:
    tag: str
    src: str
    off: int
    end: int
    j0: int
    j1: int
    ngops: int
    nbytes: int
    rec: Optional[str] = None        # recording time of the segment's first GOP
    rec_end: Optional[str] = None    # recording time of the segment's last GOP
    tc: Optional[str] = None         # tape timecode of the segment's first GOP
    tc_end: Optional[str] = None     # tape timecode of the segment's last GOP
    gap_before: bool = False         # a real tape gap precedes this segment (a separate island
                                     # stitched in by tape TC, not hash) — build marks it, never
                                     # re-phases CC across it
    frame0: int = 0                  # cumulative output frame at this segment's start (self-check)


@dataclass
class Plan:
    segments: list
    residuals: list = field(default_factory=list)
    divergences: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    total_frames: int = 0
    fps: float = 25.0
    bad_seams: int = 0
    emitted_cc: int = 0   # continuity breaks within the emitted GOPs — the output self-check's
    emitted_tei: int = 0  # expected totals (re-phasing must add none at seams)
    unused_sources: list = field(default_factory=list)  # sources that couldn't be placed (flagged)
    video_pid: Optional[int] = None   # for build's gap discontinuity markers
    lost: list = field(default_factory=list)  # recorded-but-unreadable-in-every-capture spans
    #                                           (rec-run TC + wall clock jump together); see plan.py
