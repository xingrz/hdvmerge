"""hdvmerge CLI: report | merge.

Two verbs, named after the two artifacts:

  report   refresh the per-capture indices (only re-indexing changed files) and print/write the
           human analysis report — what was found and, above all, the re-capture list.
  merge    the same analysis, then build the merged .m2t (pure byte concatenation) and write the
           report beside it.

Indexing is the one expensive step and is cached next to each capture (``<capture>.idx.jsonl``);
everything else is re-derived cheaply each run. The merge never uses ffmpeg, so Sony's private
AUX timecode survives; ffmpeg is optional and only powers ``--decode`` damage detection.
"""

import argparse
import os
import sys

from . import __version__
from . import scan as scanmod
from . import plan as planmod
from . import build as buildmod
from . import verify as verifymod
from . import report as reportmod

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


def _analyse(files, decode):
    """Ensure indices (cached) and align. Returns (report, plan). Prints per-file status."""
    bar = _Bar("indexing")

    def on_file(idx, cached=False, note=None, path=None):
        bar.clear()
        if idx is None:
            print("  %-22s SKIP (not a TS with MPEG video)" % os.path.basename(path or "?"))
            return
        if cached:
            print("  %-22s %s" % (idx.tag, note or "cached"))
            return
        cc = sum(g["cc"] for g in idx.gops)
        tei = sum(g["tei"] for g in idx.gops)
        dec = sum(g.get("dec", 0) for g in idx.gops)
        recs = [g["rec"] for g in idx.gops if g.get("rec")]
        span = ("%s … %s" % (recs[0], recs[-1])) if recs else "no AUX time"
        extra = ", dec=%d" % dec if idx.decoded else ""
        print("  %-22s indexed: %d gops, cc=%d tei=%d%s, %s"
              % (idx.tag, len(idx.gops), cc, tei, extra, span))

    rep = scanmod.analyze(files, decode=decode, on_progress=bar, on_file=on_file)
    plan = planmod.build_plan(rep)
    return rep, plan


def cmd_report(args):
    files = _discover(args.inputs)
    if not files:
        print("error: no capture files found", file=sys.stderr)
        return 2
    try:
        rep, plan = _analyse(files, args.decode)
    except RuntimeError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    md = reportmod.render(plan)
    print("\nchain: %s\n" % " -> ".join(rep.chain))
    if args.output:
        with open(args.output, "w") as f:
            f.write(md)
        print(md)
        print("wrote %s" % args.output)
    else:
        print(md)
    return 0


def cmd_merge(args):
    files = _discover(args.inputs)
    if not files:
        print("error: no capture files found", file=sys.stderr)
        return 2
    out = args.output
    try:
        rep, plan = _analyse(files, args.decode)
    except RuntimeError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    md = reportmod.render(plan)
    print("\nchain: %s\n" % " -> ".join(rep.chain))
    print(md)
    if plan.bad_seams:
        print("error: %d non-adjacent seam(s); refusing to build" % plan.bad_seams, file=sys.stderr)
        return 1
    bar = _Bar("building")
    buildmod.build(plan, out, on_progress=bar)
    bar.done()
    report_path = out + ".report.md"
    with open(report_path, "w") as f:
        f.write(md)
    ok, info = verifymod.verify(out)
    print("wrote %s (%.2f GB) and %s — AUX recording timecode %s (head %s, tail %s)"
          % (out, os.path.getsize(out) / 1e9, os.path.basename(report_path),
             "OK" if ok else "MISSING", info.get("rec_head"), info.get("rec_tail")))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hdvmerge",
                                 description="Detect damage in, align, and losslessly merge "
                                             "overlapping HDV tape captures.")
    ap.add_argument("--version", action="version", version="hdvmerge " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("report", help="analyse captures and print/write the re-capture report")
    p.add_argument("inputs", nargs="+", help="capture files or a directory of them")
    p.add_argument("-o", "--output", help="also write the report to this Markdown file")
    p.add_argument("-d", "--decode", action="store_true",
                   help="run ffmpeg decode detection for intra-frame damage (detection only)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("merge", help="analyse + build the merged file (pure byte concat)")
    p.add_argument("inputs", nargs="+", help="capture files or a directory of them")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("-d", "--decode", action="store_true",
                   help="run ffmpeg decode detection during indexing (detection only)")
    p.set_defaults(func=cmd_merge)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
