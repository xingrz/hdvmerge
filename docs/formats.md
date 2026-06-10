# The index format

The only persisted artifact is the per-capture **index**, written as `<capture>.idx.jsonl` next
to each source (or together under `--index-dir DIR`, keyed by file name; `--no-index` skips it and
indexes in memory). It caches the one expensive computation (the GOP table) so re-running only
re-indexes files whose content changed. Everything else — alignment, the segment plan, the
re-capture list — is cheap and re-derived in memory each run, so there is no second on-disk
schema to learn.

The index is **idempotent**: identical source content always yields a byte-identical index
(sorted keys, no mtimes, no timestamps, no absolute paths). That keeps it clean under version
control and safe to regenerate.

## `<capture>.idx.jsonl`

JSONL: the first line is the meta object, every following line is one GOP record.

```jsonc
// meta (line 1) — example values
{"v":4,"tag":"clip-a","size":1234567890,
 "fingerprint":"1234567890:0123456789abcdef0123456789abcdef",  // size + hash(head+tail); change detection
 "video_pid":2064,"aux_pid":2065,                              // Sony 0xA1 stream, or null
 "fps":25.0,"decoded":false,                                   // `decoded`: has the decode pass run?
 "ngops":1500}

// one GOP per line thereafter
{"i":0,                       // index within this capture
 "off":564,                   // file byte offset of the GOP's first TS packet (cut-in point)
 "end":1500564,               // byte offset of the next GOP (cut-out); last GOP -> file size
 "nbytes":1500000,
 "npic":12,                   // coded frames in the GOP
 "closed":0,"broken":0,       // GOP header flags
 "pts":306000,                // 33-bit PTS of the first access unit, or null
 "h":"0123456789abcdef",      // blake2b-8 of the GOP's ES bytes — the alignment key
 "cc":0,                      // TS continuity-counter breaks inside the GOP
 "tei":0,                     // transport_error_indicator packets inside the GOP
 "dec":0,                     // ffmpeg decode errors (0 until the decode pass runs); intra-frame damage
 "rec":"2007-01-01 09:00:00", // wall-clock recording time (nearest AUX packet), or null
 "tc":"07:00:00:00"}          // tape SMPTE timecode HH:MM:SS:FF (nearest AUX packet), or null
```

The schema version `v` gates cache reuse: an index whose `v` predates the running build (e.g. a
v1 cache without `tc`) is rebuilt, just as a fingerprint mismatch forces a rebuild.

`tag` is the basename stem only (no directory), so an index is portable and location-independent.
Two captures of the same tape GOP produce the same `h`, which is how files are aligned, overlaps
found, and seams verified tape-adjacent — see [algorithm.md](algorithm.md).

## Change detection

On each run, `hdvmerge` recomputes the source's `fingerprint` (`size:hash(first4MB+last4MB)`) and
compares it to the value in the index. Match → the index is reused untouched. Mismatch (or a new
file) → the capture is re-indexed. The decode pass additionally re-runs on a cached index whose
`decoded` is `false`, then sets it `true`.

## Re-deriving / patching

There is nothing to hand-edit. To patch in a re-capture, drop the new file beside the others and
re-run `hdvmerge`: only the new file is indexed (its index is created), the patch aligns by
content hash, and the re-capture list shrinks. To force a rebuild, delete the `.idx.jsonl`.
