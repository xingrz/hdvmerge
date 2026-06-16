"""hdvmerge command line.

``hdvmerge INPUT…`` indexes each capture (reusing the cache for unchanged files), aligns them,
and prints the analysis — above all the re-capture list. Add ``-o FILE`` to also build the merged
``.m2t`` by pure byte concatenation and write the report beside it.

Indexing is the only expensive step and is cached as ``<capture>.idx.jsonl`` — beside the source,
or together under ``--index-dir`` — while everything else is re-derived each run. The merge is
byte-level and never invokes ffmpeg, so Sony's private AUX timecode survives. ffmpeg powers the
intra-frame decode damage pass, which runs whenever ffmpeg is on PATH (``--no-decode`` to skip).
"""

import argparse
import json
import os
import sys

from . import __version__
from . import scan as scanmod
from . import plan as planmod
from . import build as buildmod
from . import verify as verifymod
from . import report as reportmod
from . import jsonout as jsonoutmod
from . import probe as probemod

EXTS = (".m2t", ".m2ts", ".mts", ".ts", ".tts", ".trp", ".tp", ".mpg", ".mpeg")
_PROGRESS_MIN = 64 * 1024 * 1024


def _discover(inputs):
    files = []
    for p in inputs:
        if os.path.isdir(p):
            files += [os.path.join(p, n) for n in sorted(os.listdir(p))
                      if n.lower().endswith(EXTS) and not n.endswith(".idx.jsonl")]
        else:
            files.append(p)
    seen, out = set(), []
    for f in files:
        a = os.path.abspath(f)
        if a not in seen:
            seen.add(a)
            out.append(f)
    return out


class _Bar:
    def __init__(self, label):
        self.label = label
        self.tty = sys.stderr.isatty()
        self.shown = -1

    def __call__(self, done, total):
        if total < _PROGRESS_MIN:
            return
        pct = min(100, int(100 * done / max(1, total)))
        if self.tty:
            if pct != self.shown:
                sys.stderr.write("\r  %-28s %3d%% (%d/%d MB)"
                                 % (self.label, pct, done >> 20, total >> 20))
                sys.stderr.flush()
                self.shown = pct
        else:
            step = (pct // 25) * 25
            if step > self.shown:
                sys.stderr.write("  %s %d%%\n" % (self.label, step))
                self.shown = step

    def clear(self):
        if self.tty and self.shown >= 0:
            sys.stderr.write("\r" + " " * 60 + "\r")
            sys.stderr.flush()
        self.shown = -1

    def done(self):
        if self.tty and self.shown >= 0:
            sys.stderr.write("\r  %-28s done%s\n" % (self.label, " " * 20))
        self.shown = -1


def _analyse(files, decode, cache_dir=None, use_cache=True, status_stream=None):
    """Ensure indices (cached) and align. Returns (report, plan). Prints per-file status to
    ``status_stream`` (stdout by default; pass stderr in ``--json`` mode to keep stdout clean)."""
    out = status_stream or sys.stdout
    bar = _Bar("indexing")

    def on_file(idx, cached=False, note=None, path=None):
        bar.clear()
        if idx is None:
            print("  %-22s SKIP (not a TS with MPEG video)" % os.path.basename(path or "?"), file=out)
            return
        if cached:
            print("  %-22s %s" % (idx.tag, note or "cached"), file=out)
            return
        cc = sum(g["cc"] for g in idx.gops)
        tei = sum(g["tei"] for g in idx.gops)
        dec = sum(g.get("dec", 0) for g in idx.gops)
        recs = [g["rec"] for g in idx.gops if g.get("rec")]
        span = ("%s … %s" % (recs[0], recs[-1])) if recs else "no AUX time"
        tcs = [g["tc"] for g in idx.gops if g.get("tc")]
        tcspan = ("  [TC %s … %s]" % (tcs[0], tcs[-1])) if tcs else ""
        extra = ", dec=%d" % dec if idx.decoded else ""
        print("  %-22s indexed: %d gops, cc=%d tei=%d%s, %s%s"
              % (idx.tag, len(idx.gops), cc, tei, extra, span, tcspan), file=out)

    rep = scanmod.analyze(files, decode=decode, cache_dir=cache_dir, use_cache=use_cache,
                          on_progress=bar, on_file=on_file)
    plan = planmod.build_plan(rep)
    return rep, plan


def cmd_run(args):
    """Index, align, and print the re-capture report (Markdown, or a JSON analysis with ``--json``).
    With ``-o`` also build the merged ``.m2t`` (pure byte concat) and write the report beside it.

    In ``--json`` mode stdout carries exactly one JSON object (the analysis); all human status,
    progress, and build messages go to stderr so the output stays machine-parseable."""
    msg = sys.stderr if args.json else sys.stdout   # where human chatter goes
    files = _discover(args.inputs)
    if not files:
        print("error: no capture files found", file=sys.stderr)
        return 2
    decode = not args.no_decode
    if decode and not probemod.have_ffmpeg():
        print("note: ffmpeg not on PATH — skipping intra-frame decode detection "
              "(TS-level damage detection still runs; pass --no-decode to silence)",
              file=sys.stderr)
        decode = False
    try:
        rep, plan = _analyse(files, decode, cache_dir=args.index_dir,
                             use_cache=not args.no_index, status_stream=msg)
    except RuntimeError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    md = reportmod.render(plan)
    if args.json:
        print(json.dumps(jsonoutmod.analysis(rep, plan), sort_keys=True))
    else:
        print("\nchain: %s\n" % " -> ".join(rep.chain))
        print(md)
    if not args.output:
        return 0
    if plan.bad_seams:
        print("error: %d non-adjacent seam(s); refusing to build" % plan.bad_seams, file=sys.stderr)
        return 1
    out = args.output
    bar = _Bar("building")
    buildmod.build(plan, out, on_progress=bar)
    bar.done()
    report_path = out + ".report.md"
    with open(report_path, "w") as f:
        f.write(md)
    print("verifying output…", file=sys.stderr)
    ok, info = verifymod.verify_build(out, plan, decode=decode)
    print("wrote %s (%.2f GB) and %s" % (out, os.path.getsize(out) / 1e9, os.path.basename(report_path)),
          file=msg)
    print("  AUX timecode %s — head %s / TC %s, tail %s / TC %s"
          % ("OK" if info.get("rec_head") and info.get("rec_tail") else "MISSING",
             info.get("rec_head"), info.get("tc_head"), info.get("rec_tail"), info.get("tc_tail")),
          file=msg)
    print("  CC/TEI integrity %s (cc %d/%d, tei %d/%d)"
          % ("OK" if info.get("cc") == info.get("expected_cc")
             and info.get("tei") == info.get("expected_tei") else "FAIL",
             info.get("cc"), info.get("expected_cc"), info.get("tei"), info.get("expected_tei")),
          file=msg)
    if "unexplained_decode" in info:
        if info.get("decode_gate"):
            status = "OK" if info["unexplained_decode"] == 0 else "FAIL"
        else:
            status = "info (noisy on a damaged merge — CC/TEI is the gate)"
        print("  decode integrity %s (%d errors, %d unexplained)"
              % (status, info.get("decode_errors", 0), info["unexplained_decode"]), file=msg)
        if info.get("seam_discontinuities"):
            print("  note: %d seam timestamp discontinuit%s (byte-exact splice; affects only some "
                  "players' seeking, not content)"
                  % (info["seam_discontinuities"],
                     "y" if info["seam_discontinuities"] == 1 else "ies"), file=msg)
    if not ok:
        print("error: output self-check FAILED — the merged file may be corrupt", file=sys.stderr)
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hdvmerge",
                                 description="Detect damage in, align, and (with -o) losslessly "
                                             "merge overlapping HDV tape captures.")
    ap.add_argument("--version", action="version", version="hdvmerge " + __version__)
    ap.add_argument("inputs", nargs="+", help="capture files or a directory of them")
    ap.add_argument("-o", "--output", metavar="FILE",
                    help="build the merged .m2t at FILE (pure byte concat) and write FILE.report.md "
                         "beside it; without -o, only analyse and print the re-capture report")
    ap.add_argument("--no-decode", action="store_true",
                    help="skip the ffmpeg intra-frame decode detection pass (on by default when "
                         "ffmpeg is available; detection only, never affects the merged bytes)")
    ap.add_argument("--json", action="store_true",
                    help="emit the analysis as one JSON object on stdout instead of the Markdown "
                         "report (a faithful dump of the model for tools to consume); all human "
                         "status goes to stderr")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--index-dir", metavar="DIR",
                   help="store and read index caches in DIR (keyed by file name) instead of "
                        "beside each capture")
    g.add_argument("--no-index", action="store_true",
                   help="do not read or write any index cache; build the index in memory each run")

    args = ap.parse_args(argv)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
