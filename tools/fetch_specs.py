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
    for d, mo, y in re.findall(r"(\d{2})-(\d{2})-(\d{4})", tail):
        try:
            dates.append(datetime.date(int(y), int(mo), int(d)))
        except ValueError:
            continue
    for y, mo, d in re.findall(r"(\d{4})-(\d{1,2})-(\d{1,2})", tail):
        try:
            dates.append(datetime.date(int(y), int(mo), int(d)))
        except ValueError:
            continue
    for d, mon, y in re.findall(
        r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
        tail,
    ):
        try:
            dates.append(datetime.date(int(y), months.index(mon) + 1, int(d)))
        except ValueError:
            continue
    # Month-first drift ("April 14, 2025") with optional ordinal/comma.
    for mon, d, y in re.findall(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
        tail,
    ):
        try:
            dates.append(datetime.date(int(y), months.index(mon) + 1, int(d)))
        except ValueError:
            continue
    # Ordinal day-first drift ("14th April 2025") with optional comma.
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


def normalize_snapshot_text(text: str) -> str:
    """Normalize extracted layout text for content comparison.

    Strips trailing whitespace per line and surrounding blank lines and
    normalizes CRLF: the same PDF re-extracted on another platform must
    still compare equal, while any real content change still differs.
    """
    return "\n".join(
        line.rstrip() for line in text.replace("\r\n", "\n").splitlines()
    ).strip()


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
    """Real-directory freshness gate for the test suite.

    Compares each snapshot's own revision/date markers against the online
    Last-Modified headers (headers only, stdlib-only; no download).

    Returns ``(status, detail)`` with status one of:

    * ``"stale"`` — at least one online copy is newer than its snapshot
      (suite must FAIL);
    * ``"fresh"`` — every spec verifiable and no online copy newer
      (suite passes);
    * ``"unknown"`` — anything unverifiable (offline/network error, no
      Last-Modified header, unparseable local markers, same-day
      intraday ambiguity, missing file): the suite must SKIP, never
      fail and never pass on partial evidence.

    Every spec is evaluated (no first-failure short-circuit: an early
    same-day ``unknown`` must not mask a later ``stale``); the worst
    status wins (stale > unknown > fresh) and the detail names every
    stale/unverifiable spec.
    """
    try:
        entries = list(OFFICIAL_SPECS.items())
    except Exception as e:  # pragma: no cover - defensive
        return "unknown", f"cannot enumerate specs ({e})"
    stale: list[str] = []
    unknowns: list[str] = []
    for key, info in entries:
        txt_path = specs_dir / f"{key}.txt"
        if not txt_path.is_file() or txt_path.stat().st_size == 0:
            unknowns.append(f"{key}.txt missing; freshness unverifiable")
            continue
        try:
            label, local_date = snapshot_revision_info(txt_path.read_text(errors="ignore"))
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
        if remote_dt.date() > local_date:
            stale.append(
                f"{key}.txt online copy newer (remote {remote_dt:%Y-%m-%d} > snapshot {local_date.isoformat()})"
            )
        elif remote_dt.date() == local_date:
            unknowns.append(
                f"{key}.txt remote revision is same-day ({remote_dt:%Y-%m-%d}); freshness unverifiable"
            )
    if stale:
        return "stale", "; ".join(stale + unknowns)
    if unknowns:
        return "unknown", "; ".join(unknowns)
    return "fresh", "all snapshots predate their online Last-Modified dates"


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
        if all_ok:
            # Refresh the checked-in last-verified manifest from the freshly
            # confirmed snapshots so the suite (and humans) can see that
            # online verification happened, and when.
            today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
            manifest = {}
            for key in OFFICIAL_SPECS:
                try:
                    label, local_date = snapshot_revision_info(
                        (dest_dir / f"{key}.txt").read_text(errors="ignore")
                    )
                except Exception:
                    label, local_date = None, None
                manifest[key] = (
                    label,
                    local_date.isoformat() if local_date else None,
                )
            try:
                source_path = Path(__file__)
                source_path.write_text(
                    update_manifest_text(
                        source_path.read_text(errors="ignore"), manifest, today
                    )
                )
                print(f"  FRESHNESS_MANIFEST refreshed (verified_on={today})")
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
