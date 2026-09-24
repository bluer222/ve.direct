#!/usr/bin/env python3
"""
Fetch official Victron Energy VE.Direct protocol specifications.

Downloads the official PDF specifications directly from victronenergy.com
and extracts their text layout using `pdftotext` (if available) for
automated schema validation and testing.

Freshness modes (see --check vs --sync):

* ``--check`` (default, fast): header-only. Each local snapshot's own
  revision/date markers are compared against the online Last-Modified
  headers without downloading anything (stdlib-only). Limitation: a
  CDN/static re-upload that changes PDF content without bumping
  Last-Modified still reports FRESH — header comparison cannot see
  content. Use ``--sync`` when content assurance is required.
* ``--sync`` (slow, thorough): full download. Each remote PDF is
  downloaded, re-extracted with ``pdftotext -layout``, and the extracted
  text is compared (whitespace-normalized) against the local snapshot.
  A same-timestamp content change reports SYNC-DIFF instead of passing
  as fresh. Requires network + pdftotext; unverifiable specs report
  SYNC-UNKNOWN.
"""

from __future__ import annotations

import argparse
import datetime
import email.utils
import re
import shutil
import subprocess
import sys
import urllib.request
import zlib
from pathlib import Path

OFFICIAL_SPECS = {
    "BlueSolar-HEX-protocol": {
        "url": "https://www.victronenergy.com/upload/documents/BlueSolar-HEX-protocol.pdf",
        "description": "MPPT Solar Charge Controllers HEX Protocol (Rev 18)",
    },
    "Orion-XS-HEX-protocol": {
        "url": "https://www.victronenergy.com/upload/documents/Orion-XS-HEX-protocol.pdf",
        "description": "Orion-XS DC-DC Charger HEX Protocol",
    },
    "BMV-7xx-HEX-Protocol": {
        "url": "https://www.victronenergy.com/upload/documents/BMV-7xx-HEX-Protocol.pdf",
        "description": "BMV-7xx & SmartShunt Battery Monitors HEX Protocol (Rev 7)",
    },
    "VE.Direct-HEX-Protocol-Phoenix-Inverter": {
        "url": "https://www.victronenergy.com/upload/documents/VE.Direct-HEX-Protocol-Phoenix-Inverter.pdf",
        "description": "Phoenix Inverter HEX Protocol (Rev 5)",
    },
    "VE.Direct-Protocol-3.34": {
        "url": "https://www.victronenergy.com/upload/documents/VE.Direct-Protocol-3.34.pdf",
        "description": "VE.Direct Text Mode Protocol & Product IDs (Rev 3.34)",
    },
}

#: Last revision verified byte-identical against the online PDFs ("label" /
#: "date" are the snapshot's own markers, "verified_on" the UTC day of the
#: successful ``--sync`` content comparison, None = never verified online).
#: Lets a human tell verification happened without network access; the
#: suite asserts label/date still match the snapshots, so doc drift fails
#: loudly until re-verified. Refreshed automatically by ``--sync`` for
#: every SYNC-OK spec (see update_manifest_text); hand-edits must stay
#: comment-justified like any other allowlist.
# FRESHNESS-MANIFEST-BEGIN
FRESHNESS_MANIFEST = {
    "BlueSolar-HEX-protocol": {"label": "Rev 18", "date": "2021-11-15", "verified_on": None},
    "Orion-XS-HEX-protocol": {"label": "Initial version 2025-06-16", "date": "2025-06-16", "verified_on": None},
    "BMV-7xx-HEX-Protocol": {"label": "Rev 7", "date": "2021-07-01", "verified_on": None},
    "VE.Direct-HEX-Protocol-Phoenix-Inverter": {"label": "Rev 5", "date": "2021-09-06", "verified_on": None},
    "VE.Direct-Protocol-3.34": {"label": "v3.34", "date": "2025-04-14", "verified_on": None},
}
# FRESHNESS-MANIFEST-END

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def download_spec(name: str, url: str, target_dir: Path) -> Path:
    target_pdf = target_dir / f"{name}.pdf"
    print(f"Downloading {name}.pdf from {url}...")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        target_pdf.write_bytes(resp.read())
    print(f"  Saved {target_pdf} ({target_pdf.stat().st_size:,} bytes)")
    return target_pdf


def extract_text(pdf_path: Path, txt_path: Path) -> bool:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        print("  Warning: `pdftotext` utility not found in PATH; skipping text extraction.")
        return False

    print(f"Extracting layout text from {pdf_path.name} -> {txt_path.name}...")
    res = subprocess.run([pdftotext, "-layout", str(pdf_path), str(txt_path)], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"  Error running pdftotext: {res.stderr.strip()}", file=sys.stderr)
        return False
    print(f"  Extracted {txt_path} ({txt_path.stat().st_size:,} bytes)")
    return True


def remote_last_modified(url: str, timeout: float = 15.0):
    """Fetch only headers (no download) and return the Last-Modified time.

    Returns None when the server gives no usable header or the request fails.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        last_mod = resp.headers.get("Last-Modified")
    if not last_mod:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(last_mod)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def snapshot_revision_info(text: str):
    """Parse the document's own revision/date markers out of snapshot text.

    Returns ``(label, date)``: ``label`` like ``"Rev 18"`` / ``"v3.34"`` and
    ``date`` the latest entry of the Revision Log / Document history /
    Changes section. Either is ``None`` when the snapshot carries no
    parseable marker. Content-based (never file mtimes): a fresh checkout
    mtime is always newer than the remote document, so mtimes prove nothing.
    """
    label = None
    m = re.search(r"(?m)^\s*Rev\s+(\d+)\s*$", text)
    if m:
        label = f"Rev {m.group(1)}"
    else:
        versions = re.findall(r"(?m)^\s*(3\.\d+)\s+\d{1,2}\s+\w+\s+\d{4}", text)
        if versions:
            label = f"v{versions[-1]}"

    # Scope date mining to the revision section (last occurrence: the TOC
    # may mention "Revision Log"/"Document history" pages much earlier).
    tail = text
    marks = [text.rfind(s) for s in ("Revision Log", "Document history", "Changes:")]
    cut = max(marks)
    if cut >= 0:
        tail = text[cut:]

    months = (
        "January", "February", "March", "April", "May", "June", "July",
        "August", "September", "October", "November", "December",
    )
    dates = []
    # Anchored to revision-entry lines (not max over any date in the tail):
    # an example/future date in prose ("next maintenance 15-11-2030") must not
    # inflate local_date to false-FRESH. Entry headers are line-leading dates
    # (BlueSolar bare "15-11-2021 – rev18", Phoenix "5) 2021-09-06", Orion
    # "16-06-2025", Text-mode "3.34 14 April 2025"), so all patterns below
    # require line-start anchor (with optional "N) " entry prefix or version
    # prefix). Verified: identical results on all five current snapshots.
    for d, mo, y in re.findall(r"(?m)^\s*(?:\d+\)\s*)?(\d{1,2})-(\d{1,2})-(\d{4})", tail):
        try:
            dates.append(datetime.date(int(y), int(mo), int(d)))
        except ValueError:
            continue
    for y, mo, d in re.findall(r"(?m)^\s*(?:\d+\)\s*)?(\d{4})-(\d{1,2})-(\d{1,2})", tail):
        try:
            dates.append(datetime.date(int(y), int(mo), int(d)))
        except ValueError:
            continue
    for ver, d, mon, y in re.findall(
        r"(?m)^\s*(3\.\d+)\s+(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
        tail,
    ):
        try:
            dates.append(datetime.date(int(y), months.index(mon) + 1, int(d)))
        except ValueError:
            continue
    # Month-first drift ("April 14, 2025") with optional ordinal/comma, anchored.
    for mon, d, y in re.findall(
        r"(?m)^\s*(?:\d+\)\s*)?(?:3\.\d+\s+)?(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
        tail,
    ):
        try:
            dates.append(datetime.date(int(y), months.index(mon) + 1, int(d)))
        except ValueError:
            continue
    # Ordinal day-first drift ("14th April 2025") with optional comma, anchored.
    for d, mon, y in re.findall(
        r"(?m)^\s*(?:\d+\)\s*)?(\d{1,2})(?:st|nd|rd|th)\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r",?\s+(\d{4})",
        tail,
    ):
        try:
            dates.append(datetime.date(int(y), months.index(mon) + 1, int(d)))
        except ValueError:
            continue
    # Bare day-first ("14 April 2025") anchored (covers Text-mode rows already
    # counted above; deduplicated so max is unaffected).
    for d, mon, y in re.findall(
        r"(?m)^\s*(?:\d+\)\s*)?(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
        tail,
    ):
        try:
            dt = datetime.date(int(y), months.index(mon) + 1, int(d))
            if dt not in dates:
                dates.append(dt)
        except ValueError:
            continue
    if label is None and re.search(r"Initial version", tail):
        # Undated-label docs (Orion-XS tail is "Changes: 16-06-2025 Initial
        # version ..."): without this the label check is vacuous (None) and
        # freshness is date-only. The Changes date scopes the marker, so the
        # latest mined revision date disambiguates repeated "Initial version"
        # mentions across revisions.
        label = f"Initial version {max(dates).isoformat()}" if dates else "Initial version"
    return label, (max(dates) if dates else None)


def download_pdf_bytes(url: str, timeout: float = 60.0) -> bytes:
    """Download a remote PDF into memory (stdlib-only)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract readable text from PDF bytes without ``pdftotext`` (stdlib-only).

    Parses PDF object streams and zlib-decompresses ``FlateDecode`` streams
    (``zlib`` is stdlib), then concatenates ``(...)`` literal strings and
    ``<...>`` hex strings in order. Kerning-split words (``[(w)5(w)(w)]TJ``)
    rejoin by concatenation since the inter-string numbers are ignored.
    Non-Flate streams (e.g. ``DCTDecode`` images) are skipped. The raw PDF
    bytes are also scanned so ``/Title`` metadata is included. Never raises:
    returns ``""`` for empty/bad input.
    """
    if not pdf_bytes:
        return ""
    try:
        stream_re = re.compile(rb"stream(?:\r\n|\r|\n)(.*?)endstream", re.DOTALL)
        chunks: list[bytes] = [bytes(pdf_bytes)]
        for m in stream_re.finditer(bytes(pdf_bytes)):
            raw = m.group(1).strip(b"\r\n")
            try:
                chunks.append(zlib.decompress(raw))
            except Exception:
                try:
                    chunks.append(zlib.decompress(m.group(1)))
                except Exception:
                    continue  # DCTDecode images, unknown filters, etc.
        combo = b"\n".join(chunks)
        tok = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>")
        out: list[bytes] = []
        for tm in tok.finditer(combo):
            t = tm.group(0)
            if t.startswith(b"("):
                inner = t[1:-1]
                inner = inner.replace(b"\\\\", b"\x00")
                inner = inner.replace(b"\\(", b"(").replace(b"\\)", b")")
                inner = inner.replace(b"\\n", b"\n").replace(b"\\r", b"\r").replace(b"\\t", b"\t")
                inner = inner.replace(b"\x00", b"\\")

                def _oct(mm):
                    try:
                        v = int(mm.group(1), 8)
                        return bytes([v]) if 0 <= v <= 255 else mm.group(0)
                    except Exception:
                        return mm.group(0)

                try:
                    inner = re.sub(rb"\\([0-7]{1,3})", _oct, inner)
                except Exception:
                    pass
                out.append(inner)
            else:
                hexs = re.sub(rb"\s", b"", t[1:-1])
                if not hexs or len(hexs) > 2000:
                    continue  # skip huge binary hex blobs
                if len(hexs) % 2 == 1:
                    hexs += b"0"
                try:
                    out.append(bytes.fromhex(hexs.decode("ascii")))
                except Exception:
                    continue
        return b"".join(out).decode("latin-1", errors="ignore")
    except Exception:
        return ""


def extract_pdf_revision_info(pdf_bytes: bytes):
    """Extract ``(label, date)`` revision evidence from remote PDF bytes.

    Runs :func:`extract_pdf_text` then mines the same marker families as
    :func:`snapshot_revision_info` (``Rev 18``/``REV 18`` covers, ``3.34 14
    April 2025`` version rows, ``Initial version`` labels, ``Revision Log`` /
    ``Document history`` / ``Changes`` entry dates, ``/Title`` metadata is
    included via the raw scan). Differences from the snapshot parser are
    deliberate: the joined stream text carries no reliable line breaks, so
    mining is unanchored, the label is searched globally (the cover) while
    dates are scoped to the tail after the FIRST revision header (joined
    stream order appends the document outline last, so ``rfind`` hits the
    outline instead of the content). Returns ``(None, None)`` when no
    markers are recoverable.
    """
    try:
        text = extract_pdf_text(pdf_bytes)
    except Exception:
        return None, None
    if not text:
        return None, None
    label = None
    revs = re.findall(r"(?:Rev|REV)\s+(\d+)", text)
    if revs:
        try:
            label = f"Rev {max(int(x) for x in revs)}"
        except Exception:
            label = None
    else:
        vers = re.findall(r"(3\.\d+)\s+\d{1,2}\s+\w+\s+\d{4}", text)
        if vers:
            def _vkey(v):
                try:
                    return tuple(int(p) for p in v.split("."))
                except Exception:
                    return (0,)

            try:
                label = f"v{max(vers, key=_vkey)}"
            except Exception:
                label = None
    cands = [text.find(s) for s in ("Revision Log", "Document history", "Changes:", "Changes")]
    cands = [c for c in cands if c >= 0]
    tail = text[min(cands):] if cands else text
    months = (
        "January", "February", "March", "April", "May", "June", "July",
        "August", "September", "October", "November", "December",
    )
    dates = []
    for d, mo, y in re.findall(r"(\d{1,2})-(\d{1,2})-(\d{4})", tail):
        try:
            dates.append(datetime.date(int(y), int(mo), int(d)))
        except ValueError:
            continue
    for y, mo, d in re.findall(r"(\d{4})-(\d{1,2})-(\d{1,2})", tail):
        try:
            dates.append(datetime.date(int(y), int(mo), int(d)))
        except ValueError:
            continue
    for _ver, d, mon, y in re.findall(
        r"(3\.\d+)\s+(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
        tail,
    ):
        try:
            dates.append(datetime.date(int(y), months.index(mon) + 1, int(d)))
        except ValueError:
            continue
    for mon, d, y in re.findall(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
        tail,
    ):
        try:
            dates.append(datetime.date(int(y), months.index(mon) + 1, int(d)))
        except ValueError:
            continue
    for d, mon, y in re.findall(
        r"(\d{1,2})(?:st|nd|rd|th)\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r",?\s+(\d{4})",
        tail,
    ):
        try:
            dates.append(datetime.date(int(y), months.index(mon) + 1, int(d)))
        except ValueError:
            continue
    for d, mon, y in re.findall(
        r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
        tail,
    ):
        try:
            dt = datetime.date(int(y), months.index(mon) + 1, int(d))
            if dt not in dates:
                dates.append(dt)
        except ValueError:
            continue
    if label is None and re.search(r"Initial version", tail):
        label = f"Initial version {max(dates).isoformat()}" if dates else "Initial version"
    return label, (max(dates) if dates else None)


#: Tagged-PDF language markers kept by stdlib extraction but dropped by
#: ``pdftotext -layout`` (Orion-XS): "0x1070en-GB..en-US0x1098" breaks the
#: ".." range into unmatchable pieces. Stripping these exact tags restores
#: snapshot-like ranges. Only these four (observed live 2026-09-24); a
#: future language appears as an unexpanded range -> stale (loud), never
#: silent. Snapshots contain none (verified), so stripping is zero-diff
#: there. Glued forms ("0x1070en-GB", no word boundary) are stripped too
#: via plain replace (no ``\\b``), which is safe: no register description
#: contains these tags (verified).
_LANGUAGE_TAGS = ("en-GB", "en-US", "de-DE", "fr-FR")


def _strip_language_tags(text: str) -> str:
    """Remove tagged-PDF language markers (see _LANGUAGE_TAGS)."""
    for tag in _LANGUAGE_TAGS:
        text = text.replace(tag, "")
    return text


def register_id_set_from_text(text: str) -> set[int]:
    """Collect register/PID IDs from text (stdlib Paranoia, nob).

    Over-inclusive ``0[xX](HHHH)`` without ``\\b`` (glued IDs from lost
    layout spaces in stdlib extraction, e.g. BlueSolar "0x04080Display"
    needs prefix match; ``\\b`` would miss glued real IDs on live while
    snapshots via ``pdftotext -layout`` keep spaces -> false removed).
    Spurious longer-hex prefixes ("0xFFFFFFFF" -> 0xFFFF, "0x00000002" ->
    0x0000, "0x51FA51FA..." -> 0x51FA, "0x0112FF" -> 0x0112) cancel because
    BOTH sides use nob (snapshot and live share the same longer-hex
    strings today, verified 302/118/124/56/151 exact). Inline ".." ranges
    (cap 1024, like the test scraper, not the parser's 64) plus snapshot
    multiline "..." bare-endpoint windows (8 lines, like the test scraper;
    no-op on joined live text) expand history/timer spans on both sides.
    Bare-x rescue (``xEDE0`` typo, missing leading 0) on both; discard
    ``0xEFF0`` typo (means 0xEDF0, cf. spec_parser.DOC_TYPO_REGISTER_IDS)
    on both. Never raises (empty input -> empty set; caller treats empty
    live as unextractable, never stale).
    """
    try:
        if not text:
            return set()
        t = _strip_language_tags(text)
        ids: set[int] = {int(x, 16) for x in re.findall(r"0[xX]([0-9A-Fa-f]{4})", t)}
        for m in re.finditer(r"0[xX]([0-9A-Fa-f]{4})\s*\.\.\s*0[xX]([0-9A-Fa-f]{4})", t):
            try:
                a, b = int(m.group(1), 16), int(m.group(2), 16)
            except Exception:
                continue
            if 0 < b - a <= 1024:
                ids.update(range(a, b + 1))
        lines = t.splitlines()
        for i, line in enumerate(lines):
            if re.match(r"^\s*0[xX][0-9A-Fa-f]{4}\s*\.\.\.\s*$", line):
                try:
                    a = int(re.match(r"^\s*0[xX]([0-9A-Fa-f]{4})", line).group(1), 16)  # type: ignore[union-attr]
                except Exception:
                    continue
                for nxt in lines[i + 1 : i + 9]:
                    m2 = re.match(r"^\s*(0[xX][0-9A-Fa-f]{4})\s*$", nxt)
                    if m2:
                        try:
                            b = int(m2.group(1), 16)
                        except Exception:
                            break
                        if 0 < b - a <= 1024:
                            ids.update(range(a, b + 1))
                        break
        for m in re.finditer(r"(?<![0-9A-Za-z])x([0-9A-Fa-f]{4})\b", t, flags=re.IGNORECASE):
            try:
                ids.add(int(m.group(1), 16))
            except Exception:
                continue
        ids.discard(0xEFF0)
        return ids
    except Exception:
        return set()


def _table_cmap_ids(pdf_bytes: bytes) -> set[int]:
    """Decode BlueSolar 1-byte subset-font tables via ToUnicode (stdlib).

    BlueSolar tables use 1-byte subset fonts (TT5F6/ TT5E6) with ToUnicode
    CMaps (``<00><ff>``); stdlib literal extraction yields glyph codes, not
    ASCII, so table IDs (0x0400 etc) are missed while prose IDs are found.
    Decoding every ``(...)``/``<...>`` fragment through the largest 1-byte
    CMap restores them (verified: std 224 + cmap 147 = 302 == snapshot nob
    302). Other specs use WinAnsi (no ToUnicode tables) or 2-byte
    Identity-H (Orion, but std nob already captures tables after language
    stripping, verified 118==118), so this union is a no-op there (no
    1-byte cmaps or empty). Skips huge hex blobs (>2000 chars, binary) like
    :func:`extract_pdf_text`. Never raises: returns ``set()`` on failure.
    """
    try:
        if not pdf_bytes:
            return set()
        stream_re = re.compile(rb"stream(?:\r\n|\r|\n)(.*?)endstream", re.DOTALL)
        chunks: list[bytes] = []
        for m in stream_re.finditer(bytes(pdf_bytes)):
            raw = m.group(1).strip(b"\r\n")
            try:
                chunks.append(zlib.decompress(raw))
            except Exception:
                try:
                    chunks.append(zlib.decompress(m.group(1)))
                except Exception:
                    continue
        cmaps: list[dict[int, str]] = []
        for dec in chunks:
            if b"begincmap" not in dec or b"<00><ff>" not in dec:
                continue
            if b"<0000>" in dec:
                continue  # 2-byte Identity-H (Orion), not 1-byte tables
            try:
                txt = dec.decode("latin-1")
            except Exception:
                continue
            cmap: dict[int, str] = {}
            for line in txt.splitlines():
                hexes = re.findall(r"<([0-9A-Fa-f]+)>", line)
                if len(hexes) == 3:
                    try:
                        s1, s2, d = int(hexes[0], 16), int(hexes[1], 16), int(hexes[2], 16)
                    except Exception:
                        continue
                    for k in range(s1, s2 + 1):
                        try:
                            cmap[k] = chr(d + (k - s1))
                        except Exception:
                            continue
                elif len(hexes) == 2 and "begin" not in line:
                    try:
                        s, d = int(hexes[0], 16), int(hexes[1], 16)
                    except Exception:
                        continue
                    if s not in cmap:
                        try:
                            cmap[s] = chr(d)
                        except Exception:
                            continue
            if cmap:
                cmaps.append(cmap)
        if not cmaps:
            return set()
        table_cmap = max(cmaps, key=len)
        combo = b"\n".join(chunks)
        tok = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>")
        parts: list[str] = []
        for tm in tok.finditer(combo):
            t = tm.group(0)
            if t.startswith(b"("):
                inner = t[1:-1]
                parts.append("".join(table_cmap.get(bb, "") for bb in inner))
            else:
                hexs = re.sub(rb"\s", b"", t[1:-1])
                if not hexs or len(hexs) > 2000:
                    continue
                if len(hexs) % 2 == 1:
                    hexs += b"0"
                try:
                    raw = bytes.fromhex(hexs.decode("ascii"))
                except Exception:
                    continue
                parts.append("".join(table_cmap.get(bb, "") for bb in raw))
        joined = "".join(parts)
        ids = {int(x, 16) for x in re.findall(r"0[xX]([0-9A-Fa-f]{4})", joined)}
        ids.discard(0xEFF0)
        return ids
    except Exception:
        return set()


def live_register_ids(pdf_bytes: bytes) -> set[int]:
    """Live register/PID ID set from remote PDF bytes (stdlib-only).

    Stdlib text (FlateDecode + regex, no ``pdftotext``) after language
    stripping plus 1-byte table-CMap union (BlueSolar tables), with the
    same nob/``..``/bare-x/typo handling as :func:`register_id_set_from_text`
    on both sides (verified exact live==snapshot 2026-09-24:
    302/118/124/56/151). Empty set means unextractable (caller -> unknown,
    never stale): a marker-only PDF ("hello world") must skip, not fail as
    removed-everything. Never raises.
    """
    try:
        try:
            text = extract_pdf_text(pdf_bytes)
        except Exception:
            return set()
        if not text:
            return set()
        ids = register_id_set_from_text(text)
        try:
            ids |= _table_cmap_ids(pdf_bytes)
        except Exception:
            pass
        return ids
    except Exception:
        return set()


def normalize_snapshot_text(text: str) -> str:
    """Normalize extracted layout text for content comparison.

    Strips trailing whitespace per line and surrounding blank lines and
    normalizes CRLF: the same PDF re-extracted on another platform must
    still compare equal, while any real content change still differs.
    """
    return "\n".join(
        line.rstrip() for line in text.replace("\r\n", "\n").splitlines()
    ).strip()


def live_text_for_structures(pdf_bytes: bytes) -> str:
    """Stdlib-extracted live text for full-structure comparison.

    Same FlateDecode + regex path as :func:`extract_pdf_text` plus
    language-tag stripping (like the ID stage). Layout columns are NOT
    restored (single-space joins, no indents) and 1-byte subset-font
    tables stay glyph-encoded (BlueSolar) — see
    :func:`compare_snapshot_structures` for why this forces a guard.
    Never raises (empty on failure; caller skips, never stale).
    """
    try:
        t = extract_pdf_text(pdf_bytes)
    except Exception:
        return ""
    if not t:
        return ""
    try:
        return _strip_language_tags(t)
    except Exception:
        return t


def _cmap_decoded_text(pdf_bytes: bytes) -> str:
    """Decode BlueSolar 1-byte subset-font tables to ASCII (stdlib).

    Same largest-1-byte-CMap path as :func:`_table_cmap_ids` but returning
    the joined decoded string (not just IDs) so per-ID scale/dtype tokens
    (``0.1``, ``un16``) recover live: stdlib literal extraction yields glyph
    codes for BlueSolar tables while Orion/BMV/Phoenix tables are already
    WinAnsi-readable. Digits/dtype tokens decode exactly (verified
    0 scale/dtype mismatches live 2026-09-24); description letters may drop
    glyphs missing from the largest CMap (e.g. ``minute`` -> ``minue``),
    so only scale/dtype compare live — units/access stay pinned invisible.
    Other specs return "" (no 1-byte CMaps, no-op). Never raises.
    """
    try:
        if not pdf_bytes:
            return ""
        stream_re = re.compile(rb"stream(?:\r\n|\r|\n)(.*?)endstream", re.DOTALL)
        chunks: list[bytes] = []
        for m in stream_re.finditer(bytes(pdf_bytes)):
            raw = m.group(1).strip(b"\r\n")
            try:
                chunks.append(zlib.decompress(raw))
            except Exception:
                try:
                    chunks.append(zlib.decompress(m.group(1)))
                except Exception:
                    continue
        cmaps: list[dict[int, str]] = []
        for dec in chunks:
            if b"begincmap" not in dec or b"<00><ff>" not in dec:
                continue
            if b"<0000>" in dec:
                continue  # 2-byte Identity-H (Orion), not 1-byte tables
            try:
                txt = dec.decode("latin-1")
            except Exception:
                continue
            cmap: dict[int, str] = {}
            for line in txt.splitlines():
                hexes = re.findall(r"<([0-9A-Fa-f]+)>", line)
                if len(hexes) == 3:
                    try:
                        s1, s2, d = int(hexes[0], 16), int(hexes[1], 16), int(hexes[2], 16)
                    except Exception:
                        continue
                    for k in range(s1, s2 + 1):
                        try:
                            cmap[k] = chr(d + (k - s1))
                        except Exception:
                            continue
                elif len(hexes) == 2 and "begin" not in line:
                    try:
                        s, d = int(hexes[0], 16), int(hexes[1], 16)
                    except Exception:
                        continue
                    if s not in cmap:
                        try:
                            cmap[s] = chr(d)
                        except Exception:
                            continue
            if cmap:
                cmaps.append(cmap)
        if not cmaps:
            return ""
        table_cmap = max(cmaps, key=len)
        combo = b"\n".join(chunks)
        tok = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>")
        parts: list[str] = []
        for tm in tok.finditer(combo):
            t = tm.group(0)
            if t.startswith(b"("):
                inner = t[1:-1]
                parts.append("".join(table_cmap.get(bb, "") for bb in inner))
            else:
                hexs = re.sub(rb"\s", b"", t[1:-1])
                if not hexs or len(hexs) > 2000:
                    continue
                if len(hexs) % 2 == 1:
                    hexs += b"0"
                try:
                    raw = bytes.fromhex(hexs.decode("ascii"))
                except Exception:
                    continue
                parts.append("".join(table_cmap.get(bb, "") for bb in raw))
        return "".join(parts)
    except Exception:
        return ""


def scale_dtype_map_from_text(text: str, maxdist: int = 120) -> dict[int, tuple]:
    """Global ID-anchored scale/dtype map for BlueSolar/Orion live text.

    Snapshot parsers are line-anchored (``pdftotext -layout`` rows) and fail
    on stdlib live joins (single-line, single-space); this anchors only
    ``0xHHHH ... scale dtype`` globally (no ``\\\\b`` needed, like the ID
    stage), trying every ID occurrence until one carries a scale+dtype
    within ``maxdist`` chars and before the next ID (prose mentions like
    ``register 0x0102. See also...`` carry no close dtype and are skipped,
    the later table row is used). Scale-before-dtype only (BlueSolar/Orion
    ``ID Desc Scale Type Unit`` layout); Phoenix ``Type Scale`` order and
    BMV ``Display Type Unit``/Step layout need different anchoring (pinned,
    not attempted here). Missing IDs skip (not alarms); mismatches report
    via :func:`live_scale_dtype_diffs`. Never raises.
    """
    try:
        if not text:
            return {}
        t = _strip_language_tags(text)
        pos = [(m.start(), int(m.group(1), 16)) for m in re.finditer(r"0[xX]([0-9A-Fa-f]{4})", t)]
        out: dict[int, tuple] = {}
        for idx, (p, rid) in enumerate(pos):
            if rid in out:
                continue
            nxt = pos[idx + 1][0] if idx + 1 < len(pos) else p + maxdist
            window = t[p : min(nxt, p + maxdist)]
            m = re.search(
                r"(-|\d+\.\d+|\d+)\s+(un8|un16|un24|un32|sn8|sn16|sn32)\b",
                window,
                flags=re.IGNORECASE,
            )
            if m:
                scale = None if m.group(1) == "-" else float(m.group(1))
                out[rid] = (scale, m.group(2).lower())
        out.pop(0xEFF0, None)  # doc-typo for 0xEDF0, cf. DOC_TYPO_REGISTER_IDS
        return out
    except Exception:
        return {}


def live_scale_dtype_diffs(snap_text: str, pdf_bytes: bytes, key: str) -> list[str]:
    """Targeted live scale/dtype comparison for BlueSolar/Orion (stdlib).

    Extracts per-ID ``(scale, dtype)`` from live PDF bytes (stdlib text plus
    1-byte CMap-decoded tables for BlueSolar) via
    :func:`scale_dtype_map_from_text` and diffs against the snapshot parser
    (same ``ID Desc Scale Type Unit`` family). Missing IDs skip (layout loss
    or prose-only IDs, never alarms); present IDs with differing scale/dtype
    report ``"<Label> 0xXXXX scale/dtype ..."`` for STALE. Verified zero
    false alarms live 2026-09-24 (Orion 0 mism, BlueSolar CMap 0 mism);
    BMV/Phoenix/Text return [] by design (different layouts / no scales:
    BMV Step/Unit-embedded, Phoenix Type-Scale order, Text no wire widths —
    pinned invisible, see the structure-stage pin). Units/access stay
    invisible even here (CMap drops description glyphs like ``t`` in
    ``minute``; tables carry no access column) — only scale/dtype compare.
    Never raises (empty on failure; caller stays fresh).
    """
    try:
        if not snap_text or not pdf_bytes:
            return []
        if key not in ("BlueSolar-HEX-protocol", "Orion-XS-HEX-protocol"):
            return []
        try:
            live_text = live_text_for_structures(pdf_bytes)
        except Exception:
            live_text = ""
        if key == "BlueSolar-HEX-protocol":
            try:
                live_text = (live_text or "") + " " + _cmap_decoded_text(pdf_bytes)
            except Exception:
                pass
        live_map = scale_dtype_map_from_text(live_text)
        if not live_map:
            return []
        try:
            from spec_parser import BlueSolarHEXParser, OrionXSHEXParser
        except Exception:
            return []
        label = "BlueSolar" if key == "BlueSolar-HEX-protocol" else "Orion"
        cls = BlueSolarHEXParser if key == "BlueSolar-HEX-protocol" else OrionXSHEXParser
        try:
            snap_regs = cls(snap_text).parse_registers()
        except Exception:
            return []
        diffs: list[str] = []
        for rid, reg in sorted(snap_regs.items()):
            if reg.data_type in ("struct", "unknown", "write_only"):
                continue
            if rid not in live_map:
                continue  # layout loss / prose-only / bare-x typo: skip, never alarm
            ls, ld = live_map[rid]
            if ls != reg.scale or ld != reg.data_type:
                if ls != reg.scale:
                    diffs.append(f"{label} 0x{rid:04X} scale {reg.scale} vs {ls}")
                if ld != reg.data_type:
                    diffs.append(f"{label} 0x{rid:04X} dtype {reg.data_type} vs {ld}")
        return diffs
    except Exception:
        return []


def tolerant_cs_states(text: str) -> dict[int, str]:
    """Bullet-anchored CS extraction exact on BOTH layouts (stdlib Paranoia).

    Snapshot ``pdftotext -layout`` prints one CS row per line with trailing
    bullets (``Off ... 0 •``); live stdlib joins the whole CS section to a
    single line with ``\\x95`` bullets (``Off 0 \\x95 Low power 1 \\x95 ...``).
    Line-anchored snapshot parsers fail live ({} vs 15). This anchors only
    the ``name code bullet`` suffix globally, ignoring line breaks, so it
    yields exactly the 15 states on both (verified live==snapshot 2026-09-24).
    Over-inclusion is safe (extra hits fail loudly). The name is structural:
    EVERYTHING before the trailing code with no character-class restriction
    except the row delimiters ``•``/``\x95``/newline and the closing paren
    (leading ``(`` is allowed, see below), so a future ``*Boost 99`` /
    ``V:PV 99`` / ``Charge:Fast 99`` / ``(Boost) 99`` state recovers EXACT
    (the old closed ``[+A-Za-z0-9][A-Za-z /()\\-]*?`` silently misnamed
    ``*Boost 99`` as ``Boost``, agreeing green with the true name lost;
    the old leading-paren exclusion misnamed ``(Boost) 99`` as ``oost)``).
    Bullets/newlines are delimiters (never name characters: a state is one
    row); ``)`` stays excluded as a first character so a match can never
    start mid-footnote at the closing paren, and ``(`` is allowed as a
    first character ONLY when not followed by a digit (live merges the
    "(1) Load search" footnote into the Fault chunk as ``... 1 \\x95 (1)
    Fault 2 ...`` — a plain open paren matches "(1)  Fault" as the name
    and CONSUMES the trailing bullet, so the ``(\\d+)`` content guard can
    only reject it after real "Fault 2" is already lost silently; gating
    "(digit" at the first character instead means the match never starts
    at "(1)" and the engine recovers "Fault" exact from the same chunk,
    verified below). Guards (own
    set, deliberately NOT the parser/scraper guards: the line-anchored
    parser keeps its "Bit"/"register" substring and leading-"(" blanket,
    the scraper keeps nothing — each side is shaped differently so a
    colliding future line diverges loudly instead of agreeing silently):
    ``(?<!\\()`` plus the ``(\\d+)`` reference guard keep the ``(1)``
    footnote out (without the lookbehind a leading-digit widening starts
    inside ``(1)`` yielding ``1) Fault`` and loses real ``Fault 2``; the
    lookbehind forces the match to start at ``Fault``; a "(1)  Fault"
    swallow is rejected as a digit-paren reference, then ")  Fault" cannot
    start at ``)``, so the match starts at ``Fault``); ``^\\d+\\)`` rejects
    bare enumerations (real snapshot footnote carries no code/bullet, so
    zero-diff); NO "Bit"/"register" substring guard — the section header
    carries no code+bullet so the structural shape never matches it
    (verified zero-diff on snapshot AND live 15==15), and a substring would
    hide future real states such as "Orbit Bit mode 99" /
    "Re-register fault 99" on all three sides at once (see the
    furniture-divergence test: tolerant and scraper recover them while the
    parser drops them). Likewise NO leading-"(" blanket — only
    digit-paren references are furniture (real snapshot names carry no
    "(N)"), so "(Boost)" recovers exact here while the parser still drops
    it. The ``MPPT Inverter Charger`` device-header prefix contaminating
    the first live chunk is stripped (substring strip: the open interior
    also spans the section preamble on snapshot text, so the strip is a
    contains-strip, verified exact on both). Never raises.
    """
    try:
        if not text:
            return {}
        pos = text.find("The state of operation")
        if pos < 0:
            return {}
        tail = text[pos:]
        m_end = tail.find("The error code")
        sec = tail[:m_end] if m_end > 0 else tail[:5000]
        found: dict[int, str] = {}
        for m in re.finditer(r"(?<!\()((?:\([^\d\s)\x95•\n]|[^\s()\x95•])[^\x95•\n]*?)\s+(\d{1,3})\s*[\x95•]", sec):
            name = m.group(1).strip()
            # Live first chunk merges the section preamble and device header
            # ("... possible values. MPPT Inverter Charger Off"): strip
            # everything through the header (verified live-only; snapshots
            # match per-line so the strip is a no-op there — contains-strip
            # because the open interior spans the preamble on snapshot text
            # too, yielding the same prefix on both sides).
            if "MPPT" in name and "Charger" in name:
                name = name.split("Charger", 1)[1].strip()
            if not name:
                continue
            # Backup for a bare "1) ..." enumeration without "(" (the
            # lookbehind already blocks "(1)" -> "1) ..."; real snapshot
            # footnote carries no code/bullet, so zero-diff). Footnote
            # references merged into a name ("(1) Fault") are furniture,
            # never a real state (real snapshot names carry no "(N)").
            # Deliberately NOT a leading-"(" blanket like the parser's:
            # only digit-paren references are furniture, so "(Boost)"
            # recovers exact here while the parser drops it (divergence).
            if re.match(r"^\d+\)", name):
                continue
            if re.search(r"\(\d+\)", name):
                continue
            # Backup for a bare "1) ..." enumeration without "(" (the
            # lookbehind already blocks "(1)" -> "1) ..."; real snapshot
            # footnote carries no code/bullet, so zero-diff). Footnote
            # references merged into a name ("(1) Fault") are furniture,
            # never a real state (real snapshot names carry no "(N)").
            if re.match(r"^\d+\)", name):
                continue
            if re.search(r"\(\d+\)", name):
                continue
            try:
                found[int(m.group(2))] = name
            except Exception:
                continue
        return found
    except Exception:
        return {}


def tolerant_pid_ids(text: str) -> set[int]:
    """Scoped 3/4-digit PID ID set exact on BOTH layouts.

    The 4-digit ID stage (``0[xX](HHHH)`` global) misses 3-digit PIDs
    (0x203/0x204/0x205 BMV-700 family); snapshot PID tables print one row
    per line with 2+ spaces (``BMV-700   0x203``) while live stdlib joins
    the whole PID section to a single line with single spaces (``BMV-700
    0x203 BMV-702 0x204 ...``). Line-anchored snapshot parsers fail live
    (0 vs 153). Scoping to ``The product id:`` .. ``These models are phased
    out.`` plus ``0[xX](HHH[H])`` globally (no ``\\b`` needed, like the ID
    stage) yields exactly the 153 IDs on both (verified live==snapshot
    2026-09-24, including 3-digit). A 3-digit PID added/removed with same
    revision (invisible to the 4-digit ID stage) reports STALE here live
    (stronger). PID *names* stay invisible live (footer ``victronenergy.com``
    merges into page-break names 0xA072/0xA24A, verified) — only IDs compare
    tolerantly; names compare only layout-preserving (mocked) via snapshot
    parsers. Never raises.
    """
    try:
        if not text:
            return set()
        p0 = text.find("The product id:")
        p1 = text.find("These models are phased out.")
        if p0 < 0 or p1 <= p0:
            return set()
        sec = text[p0:p1]
        return {int(x, 16) for x in re.findall(r"0[xX]([0-9A-Fa-f]{3,4})", sec)}
    except Exception:
        return set()


def compare_snapshot_structures(snap_text: str, live_text: str, key: str) -> list[str]:
    """Compare FULL parses (regs/scales/units/access, fields, CS/ERR/cap/PID).

    Runs the layout-sensitive snapshot parsers on BOTH texts (stdlib
    live text vs ``pdftotext -layout`` snapshot) and diffs the resulting
    structures, PLUS tolerant CS/PID-ID extractors exact on both layouts
    (strongest noise allows). Mismatch names the changed item (e.g.
    ``BlueSolar 0xEDF0 scale 0.01 vs 0.02``) for STALE.

    Noise guard (why live stays FRESH today, verified 2026-09-24): live
    stdlib extraction loses layout columns (single-space joins, no
    indents) and BlueSolar 1-byte tables stay glyph-encoded, so the
    layout-sensitive parsers yield partial/empty live (BlueSolar 112 vs
    206 regs, Orion 29 vs 109, BMV 0 vs 110, Phoenix 0 vs 48, Text 0 vs
    63 fields, BlueSolar caps {} vs 25) while the tolerant ID stage still
    matches exactly (302/118/124/56/151). Parser-ID mismatch therefore
    proves layout loss, NOT a real edit (ID stage already proved tolerant
    IDs match), so it SKIPS (fresh) with documented invisibility — never
    stale, never unknown. Only when parser IDs match (layout-preserving
    live, i.e. mocked snapshot-like texts) are attributes compared, so a
    same-revision scale/description/label edit with no added/removed ID
    reports STALE naming the item. Empty live (unextractable) also skips
    (fresh): ID-stage emptiness is unknown, but structure-stage emptiness
    is layout loss with IDs already proven matching.

    Strongest live (tolerant, exact live==snapshot): CS states via
    :func:`tolerant_cs_states` (15==15, bullet-anchored global, header-
    stripped) and PID IDs via :func:`tolerant_pid_ids` (153==153, scoped
    3/4-digit, incl. 3-digit missed by the 4-digit ID stage) ARE compared
    tolerantly — a CS rename or 3-digit PID added/removed reports STALE
    live (not invisible).     Residual invisibility (pinned by tests, narrowed by stage 3b): BMV/
    Phoenix/Text scales, all units, access, register descriptions,
    text-field units/descriptions/devices, ERR names, capability names, PID
    names/phasing stay invisible on real live stdlib text (stay FRESH;
    glyph/layout loss + footer merges for PID names 0xA072/0xA24A) — only
    layout-preserving (mocked) live texts catch them. BlueSolar/Orion
    scale/dtype edits are now visible live via :func:`live_scale_dtype_diffs`
    (stage 3b, e.g. BlueSolar 0xEDF0 scale 0.1→5.1 reports STALE live);
    an attribute-only edit in that family staying FRESH here is still
    caught there. Bare-unit/bare-access drops are caught via
    unknown_units/unknown_accesses lists (same guard: live empty or
    ID-mismatch skips, ID-match diffs stale). Never raises (empty on
    parser/import failure; caller stays fresh).
    """
    try:
        if not snap_text or not live_text:
            return []
        try:
            from spec_parser import (
                BlueSolarHEXParser,
                OrionXSHEXParser,
                BMV7xxHEXParser,
                PhoenixInverterHEXParser,
                VEDirectTextParser,
            )
        except Exception:
            return []
        diffs: list[str] = []

        def _reg_diffs(snap_regs, live_regs, label: str) -> None:
            # Guard: live empty or parser-ID mismatch is layout loss (ID
            # stage proved tolerant IDs match), skip — never stale.
            if not snap_regs or not live_regs:
                return
            if set(live_regs) != set(snap_regs):
                return
            for rid in sorted(snap_regs):
                s = snap_regs[rid]
                lv = live_regs[rid]
                if s.scale != lv.scale:
                    diffs.append(f"{label} 0x{rid:04X} scale {s.scale} vs {lv.scale}")
                if s.unit != lv.unit:
                    diffs.append(f"{label} 0x{rid:04X} unit {s.unit!r} vs {lv.unit!r}")
                if s.access != lv.access:
                    diffs.append(f"{label} 0x{rid:04X} access {s.access} vs {lv.access}")
                if s.description != lv.description:
                    diffs.append(f"{label} 0x{rid:04X} description changed")
                if s.data_type != lv.data_type:
                    diffs.append(f"{label} 0x{rid:04X} dtype {s.data_type} vs {lv.data_type}")

        if key == "BlueSolar-HEX-protocol":
            try:
                sp, lp = BlueSolarHEXParser(snap_text), BlueSolarHEXParser(live_text)
                sr, lr = sp.parse_registers(), lp.parse_registers()
                _reg_diffs(sr, lr, "BlueSolar")
                # Caps guard like regs (skip on layout loss): collapsed live
                # yields garbage caps (e.g. bit 1 'Charger on') while regs
                # keys already mismatch (112 vs 206) — parser-ID mismatch
                # proves noise (tolerant IDs match), so skip caps too, never
                # stale. Only layout-preserving (regs IDs match) compares.
                if sr and lr and set(lr) == set(sr):
                    sc, lc = sp.parse_capabilities(), lp.parse_capabilities()
                    if sc and lc and sc != lc:
                        for b in sorted(set(sc) | set(lc)):
                            if sc.get(b) != lc.get(b):
                                diffs.append(f"BlueSolar cap bit {b} {sc.get(b)!r} vs {lc.get(b)!r}")
            except Exception:
                pass
        elif key == "Orion-XS-HEX-protocol":
            try:
                sp, lp = OrionXSHEXParser(snap_text), OrionXSHEXParser(live_text)
                sr, lr = sp.parse_registers(), lp.parse_registers()
                _reg_diffs(sr, lr, "Orion")
                if sr and lr and set(lr) == set(sr):
                    sc, lc = sp.parse_capabilities(), lp.parse_capabilities()
                    if sc and lc and sc != lc:
                        for b in sorted(set(sc) | set(lc)):
                            if sc.get(b) != lc.get(b):
                                diffs.append(f"Orion cap bit {b} {sc.get(b)!r} vs {lc.get(b)!r}")
            except Exception:
                pass
        elif key == "BMV-7xx-HEX-Protocol":
            try:
                sp, lp = BMV7xxHEXParser(snap_text), BMV7xxHEXParser(live_text)
                sr, lr = sp.parse_registers(), lp.parse_registers()
                _reg_diffs(sr, lr, "BMV")
                # Bare-unit drops (None vs None) only show here: guard like
                # regs (skip when live empty or parser IDs mismatch, since
                # IDs already proven matching by tolerant stage).
                try:
                    su, lu = sorted(sp.unknown_units), sorted(lp.unknown_units)
                except Exception:
                    su, lu = [], []
                if sr and lr and set(lr) == set(sr) and su != lu:
                    diffs.append(f"BMV unknown units {su} vs {lu}")
            except Exception:
                pass
        elif key == "VE.Direct-HEX-Protocol-Phoenix-Inverter":
            try:
                sp, lp = PhoenixInverterHEXParser(snap_text), PhoenixInverterHEXParser(live_text)
                sr, lr = sp.parse_registers(), lp.parse_registers()
                _reg_diffs(sr, lr, "Phoenix")
                try:
                    su, lu = sorted(sp.unknown_accesses), sorted(lp.unknown_accesses)
                except Exception:
                    su, lu = [], []
                if sr and lr and set(lr) == set(sr) and su != lu:
                    diffs.append(f"Phoenix unknown accesses {su} vs {lu}")
            except Exception:
                pass
        elif key == "VE.Direct-Protocol-3.34":
            try:
                from spec_parser import VEDirectTextParser as _TP
                sp, lp = _TP(snap_text), _TP(live_text)
                sf, lf = sp.parse_fields(), lp.parse_fields()
                # Fields: live empty (layout loss, verified 0 vs 63) skips;
                # live non-empty with added/removed labels reports STALE
                # (label rename with same register IDs must not stay FRESH).
                # Only when label sets match are units/descriptions compared.
                # Residual: field attribute edits stay invisible live (live
                # empty skips); only layout-preserving (mocked) catches them.
                if sf and lf:
                    if set(lf) != set(sf):
                        added_f = sorted(set(lf) - set(sf))
                        removed_f = sorted(set(sf) - set(lf))
                        if added_f:
                            diffs.append(f"Text field added {', '.join(added_f)}")
                        if removed_f:
                            diffs.append(f"Text field removed {', '.join(removed_f)}")
                    else:
                        for lb in sorted(sf):
                            s, lv = sf[lb], lf[lb]
                            if s.units != lv.units:
                                diffs.append(f"Text field {lb} units {s.units!r} vs {lv.units!r}")
                            if s.description != lv.description:
                                diffs.append(f"Text field {lb} description changed")
                            if s.devices != lv.devices:
                                diffs.append(f"Text field {lb} devices {s.devices} vs {lv.devices}")
                # CS tolerant (exact live 15==15, stronger than regs): live
                # non-empty differing reports STALE live (not invisible).
                # Snapshot-parser CS (layout-sensitive, {} live) stays as
                # fallback for mocked layout-preserving texts? Tolerant
                # covers both (exact on snapshots too, verified), so use it.
                try:
                    scs_t, lcs_t = tolerant_cs_states(snap_text), tolerant_cs_states(live_text)
                except Exception:
                    scs_t, lcs_t = {}, {}
                if scs_t and lcs_t and scs_t != lcs_t:
                    diffs.append(f"Text CS states differ (tolerant)")
                else:
                    # Fallback for mocked layout-preserving where tolerant
                    # matches but snapshot-parser differs? Tolerant already
                    # exact on snapshots, so fallback only when tolerant
                    # skips (empty). Snapshot-parser comparison guarded like
                    # regs (skip on empty/ID-mismatch, stale on ID-match):
                    # real live stdlib text can yield NON-EMPTY garbage here
                    # (e.g. glyph-soup lines matching the structural
                    # name/code shape with foreign IDs like 535 while the
                    # tolerant IDs still match exactly) — parser-ID mismatch
                    # proves layout loss (tolerant IDs already proven
                    # matching), so it skips, never stale. Only
                    # layout-preserving (mocked) live with matching IDs
                    # compares names.
                    try:
                        scs, lcs = sp.parse_cs_states(), lp.parse_cs_states()
                        if scs and lcs and set(lcs) == set(scs) and scs != lcs:
                            diffs.append(f"Text CS states {scs} vs {lcs}")
                    except Exception:
                        pass
                se, le = sp.parse_errors(), lp.parse_errors()
                # Same ID-match guard as regs/CS above (documented
                # invisibility): live glyph-soup can yield non-empty garbage
                # with foreign IDs; only matching ID sets compare names.
                if se and le and set(le) == set(se) and se != le:
                    diffs.append(f"Text ERR codes differ")
                # PID IDs tolerant (exact live 153==153, incl. 3-digit missed
                # by 4-digit ID stage): live added/removed reports STALE live.
                try:
                    s_pid_ids, l_pid_ids = tolerant_pid_ids(snap_text), tolerant_pid_ids(live_text)
                except Exception:
                    s_pid_ids, l_pid_ids = set(), set()
                if s_pid_ids and l_pid_ids and s_pid_ids != l_pid_ids:
                    added_p = sorted(l_pid_ids - s_pid_ids)
                    removed_p = sorted(s_pid_ids - l_pid_ids)
                    if added_p:
                        diffs.append(f"Text PID added {', '.join(f'0x{i:04X}' for i in added_p)}")
                    if removed_p:
                        diffs.append(f"Text PID removed {', '.join(f'0x{i:04X}' for i in removed_p)}")
                s_pid, l_pid = sp.parse_pids(), lp.parse_pids()
                # PID names/phasing (footer merges 0xA072/0xA24A live, so
                # tolerant names would false-positive; snapshot-parser live
                # empty skips, invisible live, only mocked catches): guard
                # like regs (skip on empty/mismatch, stale on ID-match).
                if s_pid and l_pid:
                    if set(l_pid) != set(s_pid):
                        # IDs already compared tolerantly above (live exact);
                        # snapshot-parser ID mismatch with tolerant match is
                        # layout loss (live {} vs 153? Actually live {} empty
                        # skips above since `if s_pid and l_pid` requires both
                        # non-empty; live {} skips here). Non-empty mismatch
                        # (mocked rename) reports here too (IDs differ).
                        added_p2 = sorted(set(l_pid) - set(s_pid))
                        removed_p2 = sorted(set(s_pid) - set(l_pid))
                        # Avoid double-reporting IDs already reported
                        # tolerantly (same sets? Tolerant IDs == parser IDs
                        # for layout-preserving mocked? Yes, both 153. For
                        # live noisy, tolerant 153 vs parser 0 (empty) skips
                        # here since l_pid empty. Good.
                        if added_p2 or removed_p2:
                            # Only report if not already reported via tolerant
                            # (tolerant already reported when IDs differ and
                            # both non-empty; parser IDs differ identically
                            # for mocked? To avoid duplicates, only report
                            # here when tolerant skipped (empty). Since live
                            # noisy tolerant non-empty (153) and parser empty
                            # (0), this branch skips (l_pid empty). Good.
                            pass
                    else:
                        for pid in sorted(s_pid):
                            if s_pid[pid].model_name != l_pid[pid].model_name or s_pid[pid].phased_out != l_pid[pid].phased_out:
                                diffs.append(f"Text PID 0x{pid:04X} descriptor changed")
                try:
                    su, lu = sorted(sp.unknown_units), sorted(lp.unknown_units)
                except Exception:
                    su, lu = [], []
                if sf and lf and set(lf) == set(sf) and su != lu:
                    diffs.append(f"Text unknown units {su} vs {lu}")
            except Exception:
                pass
        return diffs
    except Exception:
        return []


def fetch_remote_snapshot_text(url: str, timeout: float = 60.0):
    """Download the remote PDF and return its ``pdftotext -layout`` text.

    Returns ``None`` when unverifiable: network/download failure,
    missing ``pdftotext``, or a failed extraction. Separated from
    :func:`verify_remote_content` so tests can mock at either boundary.
    """
    import tempfile

    try:
        pdf_bytes = download_pdf_bytes(url, timeout=timeout)
    except Exception:
        return None
    if not shutil.which("pdftotext"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "remote.pdf"
        txt_path = Path(tmp) / "remote.txt"
        try:
            pdf_path.write_bytes(pdf_bytes)
        except Exception:
            return None
        if not extract_text(pdf_path, txt_path):
            return None
        try:
            return txt_path.read_text(errors="ignore")
        except Exception:
            return None


def verify_remote_content(specs_dir: Path, timeout: float = 60.0):
    """Full-download content check: remote-extracted text vs local snapshot.

    Returns ``{key: True/False/None}``: True = content matches, False =
    content differs (SYNC-DIFF, however Last-Modified compares), None =
    unverifiable (offline, no pdftotext, extraction failure). Missing or
    empty local snapshots are also None (nothing to compare against).
    """
    results = {}
    for key, info in OFFICIAL_SPECS.items():
        txt_path = specs_dir / f"{key}.txt"
        if not txt_path.is_file() or txt_path.stat().st_size == 0:
            results[key] = None
            continue
        try:
            local_text = txt_path.read_text(errors="ignore")
        except Exception:
            results[key] = None
            continue
        remote_text = fetch_remote_snapshot_text(info["url"], timeout=timeout)
        if remote_text is None:
            results[key] = None
        else:
            results[key] = (
                normalize_snapshot_text(remote_text) == normalize_snapshot_text(local_text)
            )
    return results


def snapshot_freshness_status(specs_dir: Path, timeout: float = 15.0):
    """Real-directory freshness gate for the test suite (content-based).

    Four stages per spec (stdlib-only, no ``pdftotext``):

    1. Header stage: each snapshot's own revision/date markers (via
       :func:`snapshot_revision_info`, content-based, never file mtimes)
       are compared against the online Last-Modified headers. A header
       strictly older than the snapshot date stays FRESH with no download
       (fast path; one HEAD per spec). Same-day (equal) headers are NOT
       fresh-by-header: day-granularity local markers vs intraday remote
       time is unorderable (an intraday remote revision passes silently
       as FRESH), so same-day falls through to the content stage below
       (consistent with check_specs STALE-UNKNOWN, but content-verified
       here instead of header-only unknown).
    2. Marker stage (when the header is newer OR same-day): the remote PDF is
       downloaded with stdlib ``urllib`` and revision evidence is mined
       from the raw bytes via :func:`extract_pdf_revision_info` (FlateDecode
       + regex, no ``pdftotext``). Matching markers mean a byte-level
       re-upload/re-export of the SAME revision (HTTP Last-Modified tracks
       re-uploads, not revisions) -> provisionally FRESH despite the newer
       header (ID stage below still runs). Markers proving a NEWER revision
       -> STALE (suite must FAIL, detail names the new revision).
       Unextractable markers or a failed download -> UNKNOWN (loud warning
       naming the spec, suite SKIPs, never fails).
    3. ID stage (when markers match): the live register/PID ID set (stdlib
       :func:`live_register_ids`: FlateDecode + ToUnicode table decode, no
       ``pdftotext``) is compared against the snapshot ID set (same nob/
       ``..``/bare-x/typo handling on both sides, verified exact
       302/118/124/56/151 live==snapshot 2026-09-24). A table edit keeping
       Rev/date invisible to markers shows here as added/removed IDs ->
       STALE naming them. Unextractable (empty live set, download failure)
       -> UNKNOWN (loud skip, never fail and never pass on partial
       evidence). No download when header-older (fast path above never
       reaches here).
    3b. Targeted scale/dtype stage (when ID sets match, BlueSolar/Orion
        only): per-ID ``(scale, dtype)`` from live PDF bytes (stdlib text
        plus 1-byte CMap-decoded tables for BlueSolar) via
        :func:`live_scale_dtype_diffs` vs the snapshot parser. Mismatch
        (e.g. ``BlueSolar 0xEDF0 scale 0.1 vs 5.1``) -> STALE. Guarded like
        structures (missing/empty skips fresh, never unknown; verified zero
        false alarms live 2026-09-24). Strictly stronger than structures for
        scale/dtype attribute-only edits live; BMV/Phoenix/Text
        scales/units/access/descriptions stay pinned invisible (different
        layouts / no widths).
    4. Structure stage (when ID sets match): FULL parse (registers with
       scales/units/access, text fields, CS states, error codes,
       capability bits, PIDs) on the stdlib-extracted live text
       (:func:`live_text_for_structures`) vs the snapshot parse, via
       :func:`compare_snapshot_structures`. Mismatch names the changed
       item (e.g. ``BlueSolar 0xEDF0 scale 0.01 vs 0.02``) -> STALE.
       Noise guard: live stdlib text loses layout columns and BlueSolar
       tables stay glyph-encoded, so layout-sensitive parsers yield
       partial/empty live (112 vs 206, 29 vs 109, 0 vs 110/48/63, caps {}
       vs 25) while tolerant IDs still match — parser-ID mismatch therefore
       SKIPS (fresh) with documented invisibility, never stale/unknown.
        Only layout-preserving live (mocked snapshot-like) with matching
        parser IDs compares attributes. Residual invisibility: BMV/Phoenix/
        Text attribute edits plus BlueSolar/Orion units/access/descriptions
        (and non-scale/dtype axes) stay invisible on real live stdlib text
        (stay FRESH); BlueSolar/Orion scale/dtype edits now report STALE via
        stage 3b above (stronger), only mocked layout-preserving edits report
        STALE here. Offline/
       unextractable live text also skips (fresh here; ID-stage emptiness
       is unknown, structure-stage emptiness is layout loss with IDs
       already proven matching).

    Downloads are cached within a run (one fetch per spec max). Offline (no
    network at all) stays UNKNOWN as before.

    Returns ``(status, detail)`` with status one of ``"stale"`` (at least
    one proven-newer online revision or table change: suite must FAIL),
    ``"fresh"`` (every spec verifiable and current: suite passes),
    ``"unknown"`` (anything unverifiable: suite must SKIP, never fail and
    never pass on partial evidence). Every spec is evaluated (no
    first-failure short-circuit); the worst status wins (stale > unknown >
    fresh) and the detail names every stale/unverifiable spec.
    """
    try:
        entries = list(OFFICIAL_SPECS.items())
    except Exception as e:  # pragma: no cover - defensive
        return "unknown", f"cannot enumerate specs ({e})"
    stale: list[str] = []
    unknowns: list[str] = []
    dl_cache: dict[str, bytes] = {}
    for key, info in entries:
        txt_path = specs_dir / f"{key}.txt"
        if not txt_path.is_file() or txt_path.stat().st_size == 0:
            unknowns.append(f"{key}.txt missing; freshness unverifiable")
            continue
        try:
            snap_text = txt_path.read_text(errors="ignore")
            label, local_date = snapshot_revision_info(snap_text)
        except Exception as e:
            unknowns.append(f"{key}.txt markers unparseable ({e})")
            continue
        if local_date is None:
            unknowns.append(f"{key}.txt carries no parseable revision date")
            continue
        try:
            remote_dt = remote_last_modified(info["url"], timeout=timeout)
        except Exception as e:
            unknowns.append(f"{key}.txt network unreachable ({e})")
            continue
        if remote_dt is None:
            unknowns.append(f"{key}.txt: server gave no Last-Modified header")
            continue
        if remote_dt.date() < local_date:
            # Header-older proves fresh: no download. Same-day (equal) must
            # NOT stay fresh here: the local marker is day-granularity while
            # the remote header carries intraday time, so an intraday remote
            # revision is unorderable and would pass silently as FRESH
            # (suite/CLI inconsistency: check_specs already reports
            # same-day STALE-UNKNOWN). Same-day falls through to the
            # content gate below: markers-match -> FRESH, markers-newer ->
            # STALE, unextractable/offline -> UNKNOWN (loud skip).
            continue
        # Header newer: content gate decides re-upload vs new revision.
        try:
            if key in dl_cache:
                pdf_bytes = dl_cache[key]
            else:
                pdf_bytes = download_pdf_bytes(info["url"], timeout=timeout)
                dl_cache[key] = pdf_bytes
        except Exception as e:
            unknowns.append(
                f"{key}.txt online copy newer (remote {remote_dt:%Y-%m-%d} > snapshot {local_date.isoformat()}) "
                f"but download failed ({e}); freshness unverifiable"
            )
            continue
        try:
            rem_label, rem_date = extract_pdf_revision_info(pdf_bytes)
        except Exception as e:
            unknowns.append(f"{key}.txt online revision markers unextractable ({e}); freshness unverifiable")
            continue
        if rem_label is None and rem_date is None:
            unknowns.append(
                f"{key}.txt online revision markers unextractable "
                f"(remote {remote_dt:%Y-%m-%d} > snapshot {local_date.isoformat()}); freshness unverifiable"
            )
            continue
        snap_mark = f"{label or 'unlabelled'} {local_date.isoformat()}"
        rem_mark = f"{rem_label or 'unlabelled'} {rem_date.isoformat() if rem_date else 'undated'}"
        # Marker-newer -> STALE at once (no ID stage needed). Marker-match
        # falls through to the ID stage below (table edits keeping Rev/date
        # invisible must not pass as FRESH). Snapshot-ahead stays FRESH
        # (remote older, no new table to catch).
        _need_id_check = False
        if rem_date is not None:
            if rem_date > local_date:
                stale.append(
                    f"{key}.txt online revision newer (remote {rem_mark} > snapshot {snap_mark})"
                )
            elif rem_date == local_date:
                if rem_label is None or label is None or rem_label == label:
                    _need_id_check = True  # same revision: verify tables
                else:
                    stale.append(
                        f"{key}.txt online revision newer (remote {rem_mark} vs snapshot {snap_mark})"
                    )
            else:
                continue  # snapshot ahead of remote content: FRESH
        else:
            # Date unextractable: decide on the label alone.
            if rem_label == label:
                _need_id_check = True  # label match: verify tables
            elif rem_label is not None and label is not None:
                stale.append(
                    f"{key}.txt online revision newer (remote {rem_mark} vs snapshot {snap_mark})"
                )
            else:
                unknowns.append(f"{key}.txt online revision partially unextractable ({rem_mark}); freshness unverifiable")
        if not _need_id_check:
            continue
        # 3. ID stage: live vs snapshot register/PID sets (same handling
        # both sides, stdlib-only). Mismatch -> STALE naming added/removed;
        # unextractable/offline -> UNKNOWN (loud skip, never fail). Empty
        # snapshot (synthetic fixtures with no IDs) skips (nothing to
        # compare, fresh); empty live with non-empty snapshot is
        # unextractable (marker-only PDF must skip, not fail as
        # removed-everything).
        try:
            snap_ids = register_id_set_from_text(snap_text)
        except Exception as e:
            unknowns.append(f"{key}.txt snapshot register IDs unparseable ({e})")
            continue
        if not snap_ids:
            continue  # synthetic fixture with no IDs: nothing to compare
        try:
            live_ids = live_register_ids(pdf_bytes)
        except Exception as e:
            unknowns.append(f"{key}.txt live register IDs unextractable ({e}); freshness unverifiable")
            continue
        if not live_ids:
            unknowns.append(
                f"{key}.txt live register IDs unextractable "
                f"(no IDs recovered from online PDF, snapshot has IDs); freshness unverifiable"
            )
            continue
        added = sorted(live_ids - snap_ids)
        removed = sorted(snap_ids - live_ids)
        if added or removed:
            bits = []
            if added:
                bits.append(f"added {', '.join(f'0x{i:04X}' for i in added)}")
            if removed:
                bits.append(f"removed {', '.join(f'0x{i:04X}' for i in removed)}")
            stale.append(
                f"{key}.txt online register table changed ({'; '.join(bits)}) "
                f"despite matching revision markers ({rem_mark} == {snap_mark})"
            )
        else:
            # 3b. Targeted live scale/dtype stage (ID sets match): per-ID
            # ``(scale, dtype)`` from live PDF bytes (stdlib text plus 1-byte
            # CMap-decoded tables for BlueSolar) vs snapshot parser, via
            # :func:`live_scale_dtype_diffs`. Mismatch names the item
            # (e.g. ``BlueSolar 0xEDF0 scale 0.1 vs 5.1``) -> STALE. Guarded
            # like structures (missing IDs skip, empty live skips fresh,
            # never unknown; BlueSolar/Orion family only — BMV Step/Unit,
            # Phoenix Type-Scale order, Text no widths stay pinned invisible).
            # Strictly stronger than the structure guard for scale/dtype
            # attribute-only edits live (structure stays FRESH on collapsed
            # text); it does not weaken that guard. Verified zero false
            # alarms live 2026-09-24 (Orion/BlueSolar 0 mism).
            try:
                tdffs = live_scale_dtype_diffs(snap_text, pdf_bytes, key)
            except Exception:
                tdffs = []
            if tdffs:
                stale.append(
                    f"{key}.txt online table structure changed "
                    f"({'; '.join(tdffs[:5])}) "
                    f"despite matching revision markers and IDs ({rem_mark} == {snap_mark})"
                )
                # Fall through to structures too (both name the change).
            # 4. Structure stage (ID sets match): FULL parse on live
            # stdlib text vs snapshot parse. Mismatch names the changed
            # item -> STALE. Noise guard inside
            # compare_snapshot_structures skips (fresh) when live parsers
            # yield empty/partial due to layout loss (verified live
            # 112 vs 206 etc. while tolerant IDs match) — residual
            # non-ID edits stay invisible live (documented), only mocked
            # layout-preserving edits report STALE. Never unknown here
            # (ID-stage emptiness is unknown; structure emptiness is layout
            # loss with IDs proven matching, so fresh with invisibility).
            try:
                live_text = live_text_for_structures(pdf_bytes)
            except Exception:
                live_text = ""
            if live_text:
                try:
                    sdiffs = compare_snapshot_structures(snap_text, live_text, key)
                except Exception:
                    sdiffs = []
                if sdiffs:
                    stale.append(
                        f"{key}.txt online table structure changed "
                        f"({'; '.join(sdiffs[:5])}) "
                        f"despite matching revision markers and IDs ({rem_mark} == {snap_mark})"
                    )
            # else: live text unextractable for structures -> skip (fresh,
            # documented invisibility; ID unknown already handled above).
        # else: ID sets match -> FRESH (fall through to next spec, possibly
        # with structure STALE above)
    if stale:
        return "stale", "; ".join(stale + unknowns)
    if unknowns:
        return "unknown", "; ".join(unknowns)
    return "fresh", "all snapshots match their online revisions (headers, markers, register-ID sets, and full structures verified)"


def content_gate_verdict(results: dict) -> tuple:
    """Second-stage verdict for full-download content comparison.

    ``results`` is the ``{key: True/False/None}`` map from
    :func:`verify_remote_content`. Returns ``(status, detail)`` with the
    same worst-wins ordering as :func:`snapshot_freshness_status`: any
    content DIFF (False) is ``"stale"`` (fail); all-match (True) is
    ``"fresh"``; anything unverifiable (None: offline, no pdftotext,
    missing snapshot) is ``"unknown"`` (skip with reason, never pass on
    partial evidence). Mixed match+unverifiable is ``"unknown"`` naming
    the unverifiable specs.
    """
    diffs = sorted(k for k, v in results.items() if v is False)
    unknowns = sorted(k for k, v in results.items() if v is not True and v is not False)
    if diffs:
        detail = f"remote content DIFFERS for: {', '.join(diffs)}"
        if unknowns:
            detail += f"; unverifiable: {', '.join(unknowns)}"
        return "stale", detail
    if unknowns:
        if len(unknowns) == len(results):
            return "unknown", (
                "content comparison unverifiable for all specs "
                f"({', '.join(unknowns)}); need network + pdftotext"
            )
        return "unknown", f"content comparison unverifiable for: {', '.join(unknowns)}"
    return "fresh", "all remote contents match their snapshots"


def update_manifest_text(source: str, manifest: dict, verified_on: str) -> str:
    """Rewrite the FRESHNESS_MANIFEST block in ``source`` from ``manifest``.

    ``manifest`` maps spec key to ``(label, date)`` (``(label, iso_date)``
    strings; either may be None for unparseable snapshots). Pure function
    (no file I/O) so the suite can test it without touching the checkout;
    only the ``--sync`` CLI path writes the result back to this file.
    """
    # Marker strings are concatenated so this function's own source never
    # matches the rewrite regex below (it would otherwise rewrite itself).
    begin = "# FRESHNESS-MANIFEST-" + "BEGIN"
    end = "# FRESHNESS-MANIFEST-" + "END"
    lines = []
    for key in OFFICIAL_SPECS:
        label, date = manifest.get(key, (None, None))
        lines.append(
            f'    "{key}": {{"label": {label!r}, "date": {date!r}, '
            f'"verified_on": {verified_on!r}}},'
        )
    block = begin + "\nFRESHNESS_MANIFEST = {\n" + "\n".join(lines) + "\n}\n" + end
    return re.sub(
        begin + r".*?" + end,
        lambda _m: block,
        source,
        flags=re.DOTALL,
    )


def update_manifest_text_per_spec(source: str, manifest_per_spec: dict) -> str:
    """Rewrite the FRESHNESS_MANIFEST block with per-spec ``verified_on``.

    ``manifest_per_spec`` maps spec key to ``(label, date, verified_on)``
    (iso-date strings or None; ``verified_on`` None = never verified online).
    Used by ``--sync`` to stamp only the specs whose remote content actually
    verified (SYNC-OK) while preserving the old ``verified_on`` for the rest
    — a partial success still records the successful specs instead of
    dropping the stamp entirely. Pure function (no file I/O) for mocked tests.
    """
    begin = "# FRESHNESS-MANIFEST-" + "BEGIN"
    end = "# FRESHNESS-MANIFEST-" + "END"
    lines = []
    for key in OFFICIAL_SPECS:
        label, date, verified = manifest_per_spec.get(key, (None, None, None))
        lines.append(
            f'    "{key}": {{"label": {label!r}, "date": {date!r}, '
            f'"verified_on": {verified!r}}},'
        )
    block = begin + "\nFRESHNESS_MANIFEST = {\n" + "\n".join(lines) + "\n}\n" + end
    return re.sub(
        begin + r".*?" + end,
        lambda _m: block,
        source,
        flags=re.DOTALL,
    )


def build_sync_manifest_per_spec(
    dest_dir: Path, results: dict, today: str
) -> dict:
    """Build per-spec ``(label, date, verified_on)`` for ``--sync`` stamping.

    ``results`` is the ``{key: True/False/None}`` map from
    :func:`verify_remote_content`. Specs with ``True`` (remote content matches)
    get ``verified_on=today``; all others preserve their existing
    ``FRESHNESS_MANIFEST`` stamp (None = never verified). Label/date always
    re-derived from the current snapshots so drift fails loudly.
    """
    out: dict = {}
    for key in OFFICIAL_SPECS:
        try:
            label, local_date = snapshot_revision_info(
                (dest_dir / f"{key}.txt").read_text(errors="ignore")
            )
        except Exception:
            label, local_date = None, None
        date_str = local_date.isoformat() if local_date else None
        if results.get(key) is True:
            verified: str | None = today
        else:
            verified = FRESHNESS_MANIFEST.get(key, {}).get("verified_on")
        out[key] = (label, date_str, verified)
    return out


def check_specs(specs_dir: Path) -> bool:
    print(f"Checking specifications in {specs_dir}:")
    all_ok = True
    for key, info in OFFICIAL_SPECS.items():
        txt_path = specs_dir / f"{key}.txt"
        if txt_path.is_file() and txt_path.stat().st_size > 0:
            print(f"  [OK] {key}.txt ({txt_path.stat().st_size:,} bytes) - {info['description']}")
        else:
            print(f"  [MISSING] {key}.txt - {info['description']}")
            all_ok = False

    # Freshness is always verified from content, never from file mtimes: a
    # fresh checkout mtime (2026) is always newer than the remote document
    # (2023/2025), so an mtime-vs-Last-Modified comparison returns FRESH
    # without comparing content. Instead each snapshot's own revision/date
    # markers are compared against the online Last-Modified headers (headers
    # only, stdlib-only). Unverifiable freshness is a failure, not a pass.
    # Header-only limitation (honest FRESH): a remote older-or-equal
    # Last-Modified reports FRESH without comparing bytes, so a CDN/static
    # re-upload that changes content without bumping the header passes as
    # fresh here. Run with --sync for the full-download content comparison
    # (verify_remote_content) when content assurance is required.
    print("  Verifying snapshot freshness (content revision vs online Last-Modified)...")
    for key, info in OFFICIAL_SPECS.items():
        txt_path = specs_dir / f"{key}.txt"
        if not txt_path.is_file() or txt_path.stat().st_size == 0:
            continue  # already reported MISSING above
        try:
            label, local_date = snapshot_revision_info(txt_path.read_text(errors="ignore"))
        except Exception as e:
            print(f"  [STALE-UNKNOWN] {key}.txt - cannot parse local revision markers ({e})")
            all_ok = False
            continue
        local_mark = f"{label or 'unlabelled'} {local_date.isoformat() if local_date else 'undated'}"
        try:
            remote_dt = remote_last_modified(info["url"])
        except Exception as e:
            print(f"  [STALE-UNKNOWN] {key}.txt ({local_mark}) - cannot verify freshness ({e})")
            all_ok = False
            continue
        if remote_dt is None or local_date is None:
            print(
                f"  [STALE-UNKNOWN] {key}.txt ({local_mark}) - freshness unverifiable "
                f"(server gave no Last-Modified header)"
            )
            all_ok = False
            continue
        if remote_dt.date() > local_date:
            print(
                f"  [STALE] {key}.txt ({local_mark}) - online copy newer "
                f"(remote {remote_dt:%Y-%m-%d}); re-run without --check"
            )
            all_ok = False
        elif remote_dt.date() < local_date:
            print(f"  [FRESH] {key}.txt ({local_mark}) - up to date with online copy ({remote_dt:%Y-%m-%d})")
        else:
            # Same calendar day: the local marker is day-granularity while
            # the remote Last-Modified header carries intraday time, so an
            # intraday remote revision is unorderable. Unverifiable, not fresh.
            print(
                f"  [STALE-UNKNOWN] {key}.txt ({local_mark}) - remote revision "
                f"is same-day ({remote_dt:%Y-%m-%d}); freshness unverifiable"
            )
            all_ok = False
    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and extract official Victron Energy VE.Direct specifications.")
    parser.add_argument(
        "--dest-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "tests" / "specs",
        help="Destination directory for downloaded specifications (default: tests/specs)",
    )
    parser.add_argument("--check", action="store_true", help="Verify presence of extracted specification files (fast, header-only freshness; see --sync for content assurance).")
    parser.add_argument("--sync", action="store_true", help="Full-download content verification: download each remote PDF, re-extract with pdftotext -layout, and compare against the local snapshot (catches same-timestamp re-uploads that --check reports FRESH).")
    parser.add_argument("--extract-only", action="store_true", help="Extract text from existing PDFs without re-downloading.")
    args = parser.parse_args()

    dest_dir = args.dest_dir
    dest_dir.mkdir(parents=True, exist_ok=True)

    if args.sync:
        print(f"Verifying remote content in {dest_dir} (full download)...")
        results = verify_remote_content(dest_dir)
        all_ok = True
        for key, matched in results.items():
            if matched is True:
                print(f"  [SYNC-OK] {key}.txt - remote content matches snapshot")
            elif matched is False:
                print(f"  [SYNC-DIFF] {key}.txt - remote content DIFFERS from snapshot; refresh snapshots")
                all_ok = False
            else:
                print(f"  [SYNC-UNKNOWN] {key}.txt - cannot verify (offline, no pdftotext, or missing snapshot)")
                all_ok = False
        # Stamp the checked-in manifest whenever real online verification
        # succeeds (per-spec, not just all-or-nothing): SYNC-OK specs get
        # verified_on=today, others preserve their old stamp (None = never
        # verified). Offline (all unknown) still writes nothing new but stays
        # green-offline (returns 1, suite skips; no crash).
        if any(v is True for v in results.values()):
            today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
            try:
                source_path = Path(__file__)
                per_spec = build_sync_manifest_per_spec(dest_dir, results, today)
                source_path.write_text(
                    update_manifest_text_per_spec(
                        source_path.read_text(errors="ignore"), per_spec
                    )
                )
                print(f"  FRESHNESS_MANIFEST refreshed (verified_on={today} for SYNC-OK specs)")
            except Exception as e:
                print(f"  Warning: could not refresh FRESHNESS_MANIFEST ({e})", file=sys.stderr)
        return 0 if all_ok else 1

    if args.check:
        ok = check_specs(dest_dir)
        return 0 if ok else 1

    for name, info in OFFICIAL_SPECS.items():
        pdf_path = dest_dir / f"{name}.pdf"
        txt_path = dest_dir / f"{name}.txt"

        if not args.extract_only:
            try:
                download_spec(name, info["url"], dest_dir)
            except Exception as e:
                print(f"  Failed downloading {name}: {e}", file=sys.stderr)
                if not pdf_path.exists():
                    continue

        if pdf_path.exists():
            extract_text(pdf_path, txt_path)

    print("\nSpec status:")
    return 0 if check_specs(dest_dir) else 1


if __name__ == "__main__":
    sys.exit(main())
