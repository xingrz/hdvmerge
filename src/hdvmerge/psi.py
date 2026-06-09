"""PAT/PMT parsing to locate the MPEG video PID and Sony's HDV AUX metadata PID.

PMT sections can span several TS packets, so we reassemble by ``payload_unit_start`` before
parsing — a single-packet parse can miss the ``0xA1`` AUX declaration in a long PMT.
"""

from . import TS
from . import ts as T


def _section(buf, pusi):
    """Strip the pointer_field from the first packet of a PSI section payload."""
    return buf[1 + buf[0]:] if pusi else buf


def find_pids(path, scan_bytes=8 * 1024 * 1024):
    """Scan the start of ``path`` and return ``(framing, video_pid, aux_pid, pmt_pid)``.

    aux = Sony HDV camera-metadata stream (``stream_type`` 0xA1, carries recording date/time);
    falls back to 0xA0. Any may be ``None`` if not present / not a TS.
    """
    with open(path, "rb") as f:
        buf = f.read(scan_bytes)
    framing = T.detect_framing(buf)
    if framing is None:
        return None, None, None, None
    stride = framing["stride"]
    pmt_pid = video_pid = aux_pid = None
    pmt_acc = {}  # pmt_pid -> bytearray section being assembled

    pos = framing["first_sync"]
    n = len(buf)
    while pos + TS <= n:
        if buf[pos] != 0x47:
            nxt = T.first_sync(buf, stride, start=pos + 1)
            if nxt is None:
                break
            pos = nxt
            continue
        pkt = buf[pos:pos + TS]
        p = T.pid(pkt)
        ps = T.payload_start(pkt)
        if ps is not None:
            pay = pkt[ps:]
            if p == 0 and pmt_pid is None:
                sec = _section(pay, T.pusi(pkt))
                if len(sec) >= 8 and sec[0] == 0x00:
                    slen = ((sec[1] & 0x0F) << 8) | sec[2]
                    k = 8
                    while k + 4 <= 3 + slen - 4 and k + 4 <= len(sec):
                        prog = (sec[k] << 8) | sec[k + 1]
                        ppid = ((sec[k + 2] & 0x1F) << 8) | sec[k + 3]
                        if prog != 0:
                            pmt_pid = ppid
                            break
                        k += 4
            elif p == pmt_pid:
                if T.pusi(pkt):
                    pmt_acc[p] = bytearray(_section(pay, True))
                elif p in pmt_acc:
                    pmt_acc[p] += pay
                got = _parse_pmt(pmt_acc.get(p, b""))
                if got is not None:
                    video_pid, aux_pid = got
                    return framing, video_pid, aux_pid, pmt_pid
        pos += stride
    return framing, video_pid, aux_pid, pmt_pid


def _parse_pmt(sec):
    """Return ``(video_pid, aux_pid)`` once the full PMT section is present, else None."""
    if len(sec) < 12 or sec[0] != 0x02:
        return None
    slen = ((sec[1] & 0x0F) << 8) | sec[2]
    total = 3 + slen
    if len(sec) < total:  # section not fully assembled yet
        return None
    pil = ((sec[10] & 0x0F) << 8) | sec[11]
    k = 12 + pil
    end = total - 4  # drop CRC
    video = a1 = a0 = None
    while k + 5 <= end:
        stype = sec[k]
        epid = ((sec[k + 1] & 0x1F) << 8) | sec[k + 2]
        esil = ((sec[k + 3] & 0x0F) << 8) | sec[k + 4]
        if stype in (0x01, 0x02) and video is None:
            video = epid
        elif stype == 0xA1 and a1 is None:
            a1 = epid
        elif stype == 0xA0 and a0 is None:
            a0 = epid
        k += 5 + esil
    return video, (a1 if a1 is not None else a0)
