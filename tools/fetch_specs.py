#!/usr/bin/env python3
"""
Fetch official Victron Energy VE.Direct protocol specifications.

Downloads the official PDF specifications directly from victronenergy.com
and extracts their text layout using `pdftotext` (if available) for
automated schema validation and testing.
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
    return label, (max(dates) if dates else None)


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
    parser.add_argument("--check", action="store_true", help="Verify presence of extracted specification files.")
    parser.add_argument("--extract-only", action="store_true", help="Extract text from existing PDFs without re-downloading.")
    args = parser.parse_args()

    dest_dir = args.dest_dir
    dest_dir.mkdir(parents=True, exist_ok=True)

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
