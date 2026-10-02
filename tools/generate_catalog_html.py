#!/usr/bin/env python3
"""Generate a searchable, filterable HTML catalog of all options + descriptions.

Covers:
  * HEX registers (``ve_direct.registers.REGISTERS``) — name, wire ID, class,
    access, dtype, unit, scale, flavors, enum table + decoded enum/bitmask values.
  * Text-mode labels (``ve_direct.tables.LABEL_INFO``) — wire unit + description
    plus decoded value tables (CS/ERR/MODE/MPPT/MON/AR/OR/CAP_BLE).
  * Product IDs (``ve_direct.tables.PID_MAP``).

Output is a single self-contained HTML file (inline CSS + vanilla JS, no
external dependencies) with a global search box and per-table filters.

Usage:
    python tools/generate_catalog_html.py [--output docs/catalog.html]
    python tools/generate_catalog_html.py -o /tmp/catalog.html --title "My catalog"
"""
from __future__ import annotations

import argparse
import datetime
import html
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ve_direct.registers import ENUM_MAPS, REGISTERS  # noqa: E402
from ve_direct.tables import (  # noqa: E402
    AR_MAP,
    CAP_BLE_MAP,
    CS_MAP,
    ERR_MAP,
    LABEL_INFO,
    MODE_MAP,
    MON_MAP,
    MPPT_MAP,
    OR_MAP,
    PID_MAP,
)


def _esc(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _enum_values_label(enum_name: str | None) -> str:
    """Human-readable 'k=label, ...' list for an ENUM/BITMASK map."""
    if not enum_name:
        return ""
    mapping = ENUM_MAPS.get(enum_name)
    if not mapping:
        return ""
    items = sorted(mapping.items())
    return ", ".join(f"{k}={v}" for k, v in items)


def _text_decoded_values(label: str) -> str:
    if label == "CS":
        src = CS_MAP
    elif label == "ERR":
        src = ERR_MAP
    elif label == "MODE":
        src = MODE_MAP
    elif label == "MPPT":
        src = MPPT_MAP
    elif label == "MON":
        src = MON_MAP
    elif label in ("AR", "WARN"):
        src = AR_MAP
    elif label == "OR":
        src = OR_MAP
    elif label == "CAP_BLE":
        src = CAP_BLE_MAP
    else:
        return ""
    return ", ".join(f"{k}={v}" for k, v in sorted(src.items()))


def _flavor_options() -> list[str]:
    flavors: set[str] = set()
    for reg in REGISTERS.values():
        flavors.update(reg.flavors)
    # Stable order with ANY first, then alphabetical.
    ordered = sorted(flavors - {"ANY"})
    return ["ANY"] + ordered


def build_html(title: str) -> str:
    regs = sorted(REGISTERS.values(), key=lambda r: (r.name, r.id))
    labels = sorted(LABEL_INFO.items(), key=lambda kv: kv[0].lower())
    pids = sorted(PID_MAP.items())
    flavors = _flavor_options()
    classes = sorted({r.cls for r in regs})
    accesses = sorted({r.access for r in regs})

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        from ve_direct import __version__ as _ver  # type: ignore
    except Exception:
        _ver = "unknown"

    def reg_row(r) -> str:
        flavor_str = "/".join(sorted(r.flavors))
        enum_vals = _enum_values_label(r.enum)
        search_blob = " ".join(
            [
                r.name,
                f"0x{r.id:04X}",
                str(r.id),
                r.cls,
                r.access,
                r.dtype,
                r.unit,
                flavor_str,
                r.enum or "",
                enum_vals,
            ]
        ).lower()
        return (
            f'<tr data-search="{_esc(search_blob)}" '
            f'data-flavors="{_esc("|".join(sorted(r.flavors)))}" '
            f'data-class="{_esc(r.cls)}" data-access="{_esc(r.access)}">'
            f"<td><code>{_esc(r.name)}</code></td>"
            f"<td><code>0x{r.id:04X}</code><br><span class='muted'>{r.id}</span></td>"
            f"<td>{_esc(r.cls)}</td>"
            f"<td>{_esc(r.access)}</td>"
            f"<td><code>{_esc(r.dtype)}</code></td>"
            f"<td>{_esc(r.unit or '—')}</td>"
            f"<td>{_esc(r.scale)}</td>"
            f"<td>{_esc(flavor_str)}</td>"
            f"<td><code>{_esc(r.enum or '—')}</code>"
            + (f"<div class='enumvals'>{_esc(enum_vals)}</div>" if enum_vals else "")
            + "</td></tr>"
        )

    def label_row(label: str, info: dict) -> str:
        unit = info.get("unit") or "—"
        desc = info.get("desc") or ""
        decoded = _text_decoded_values(label)
        search_blob = f"{label} {unit} {desc} {decoded}".lower()
        decoded_html = _esc(decoded) if decoded else "<span class='muted'>—</span>"
        return (
            f'<tr data-search="{_esc(search_blob)}">'
            f"<td><code>{_esc(label)}</code></td>"
            f"<td>{_esc(unit)}</td>"
            f"<td>{_esc(desc)}</td>"
            f"<td>{decoded_html}</td></tr>"
        )

    def pid_row(pid: int, name: str) -> str:
        search_blob = f"{pid:#06x} {pid} {name}".lower()
        return (
            f'<tr data-search="{_esc(search_blob)}">'
            f"<td><code>0x{pid:04X}</code></td>"
            f"<td>{pid}</td>"
            f"<td>{_esc(name)}</td></tr>"
        )

    hex_rows = "\n".join(reg_row(r) for r in regs)
    text_rows = "\n".join(label_row(k, v) for k, v in labels)
    pid_rows = "\n".join(pid_row(pid, name) for pid, name in pids)
    flavor_opts = "\n".join(
        f'<option value="{_esc(f)}">{_esc(f)}</option>' for f in flavors
    )
    class_opts = "\n".join(
        f'<option value="{_esc(c)}">{_esc(c)}</option>' for c in classes
    )
    access_opts = "\n".join(
        f'<option value="{_esc(a)}">{_esc(a)}</option>' for a in accesses
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  margin: 0 auto; max-width: 1200px; padding: 1rem 1.25rem 3rem; line-height: 1.45; }}
header {{ position: sticky; top: 0; background: inherit; padding: .75rem 0;
  border-bottom: 1px solid #8884; z-index: 10; }}
h1 {{ font-size: 1.4rem; margin: .25rem 0; }}
h2 {{ margin-top: 2rem; }}
.controls {{ display: flex; flex-wrap: wrap; gap: .5rem; align-items: end; }}
.controls label {{ display: flex; flex-direction: column; font-size: .8rem; gap: .2rem; }}
input[type=search], select {{ font-size: .95rem; padding: .4rem .5rem; min-width: 10rem; }}
#q {{ min-width: 18rem; flex: 1; }}
.counts {{ font-size: .85rem; opacity: .8; margin-top: .4rem; }}
table {{ border-collapse: collapse; width: 100%; font-size: .85rem; margin-top: .75rem; }}
th, td {{ border: 1px solid #8884; padding: .35rem .45rem; text-align: left;
  vertical-align: top; }}
th {{ position: sticky; top: 0; background: #8882; }}
tbody tr:nth-child(even) {{ background: #8881; }}
.muted {{ opacity: .65; font-size: .8em; }}
.enumvals {{ font-size: .78em; opacity: .85; margin-top: .2rem; max-width: 22rem; }}
.tabs {{ display: flex; gap: .5rem; margin-top: .5rem; }}
.tabs button {{ padding: .35rem .7rem; cursor: pointer; }}
section[data-section].hidden {{ display: none; }}
footer {{ margin-top: 2rem; font-size: .8rem; opacity: .75; }}
code {{ font-size: .85em; }}
</style>
</head>
<body>
<header>
<h1>{_esc(title)}</h1>
<div class="controls">
<label>Search all tables<input type="search" id="q" placeholder="e.g. panel voltage, 0xEDBB, SOC, alarm&hellip;" autocomplete="off"></label>
<label>Flavor (HEX)<select id="fFlavor"><option value="">All flavors</option>
{flavor_opts}</select></label>
<label>Class (HEX)<select id="fClass"><option value="">All classes</option>
{class_opts}</select></label>
<label>Access (HEX)<select id="fAccess"><option value="">All access</option>
{access_opts}</select></label>
<button id="clear" type="button">Clear</button>
</div>
<div class="tabs" role="tablist">
<button data-tab="hex" type="button">HEX registers (<span id="nHex">{len(regs)}</span>)</button>
<button data-tab="text" type="button">Text labels (<span id="nText">{len(labels)}</span>)</button>
<button data-tab="pid" type="button">Product IDs (<span id="nPid">{len(pids)}</span>)</button>
<button data-tab="all" type="button">Show all</button>
</div>
<div class="counts" id="counts" aria-live="polite"></div>
</header>

<section data-section="hex">
<h2>HEX registers &mdash; <code>hex_get</code> / <code>hex_set</code> catalog</h2>
<p class="muted">Every entry of <code>REGISTERS</code>. Enum column lists the value table used for
decode/encode; flavors show which device families expose the register.</p>
<table id="tHex">
<thead><tr><th>Name</th><th>ID</th><th>Class</th><th>Access</th><th>Type</th>
<th>Unit</th><th>Scale</th><th>Flavors</th><th>Enum / values</th></tr></thead>
<tbody>
{hex_rows}
</tbody>
</table>
</section>

<section data-section="text">
<h2>Text-mode labels</h2>
<p class="muted">From <code>LABEL_INFO</code> plus decoded value tables
(CS/ERR/MODE/MPPT/MON/AR/OR/CAP_BLE).</p>
<table id="tText">
<thead><tr><th>Label</th><th>Wire unit</th><th>Description</th><th>Decoded values</th></tr></thead>
<tbody>
{text_rows}
</tbody>
</table>
</section>

<section data-section="pid">
<h2>Product IDs</h2>
<p class="muted">From <code>PID_MAP</code>.</p>
<table id="tPid">
<thead><tr><th>PID</th><th>Decimal</th><th>Model</th></tr></thead>
<tbody>
{pid_rows}
</tbody>
</table>
</section>

<footer>Generated { _esc(now)} from ve-direct {_esc(_ver)} &mdash;
{len(regs)} HEX registers, {len(labels)} text labels, {len(pids)} product IDs.</footer>

<script>
const q = document.getElementById('q');
const fFlavor = document.getElementById('fFlavor');
const fClass = document.getElementById('fClass');
const fAccess = document.getElementById('fAccess');
const counts = document.getElementById('counts');

function visibleSections() {{
  const active = document.body.dataset.tab || 'all';
  return active;
}}

function applyFilters() {{
  const needle = q.value.trim().toLowerCase();
  const flavor = fFlavor.value;
  const cls = fClass.value;
  const access = fAccess.value;
  let nHex = 0, nHexTotal = 0, nText = 0, nTextTotal = 0, nPid = 0, nPidTotal = 0;

  document.querySelectorAll('#tHex tbody tr').forEach(tr => {{
    nHexTotal++;
    let show = true;
    if (needle && !tr.dataset.search.includes(needle)) show = false;
    if (show && flavor) {{
      const fl = (tr.dataset.flavors || '').split('|');
      if (!fl.includes(flavor) && !fl.includes('ANY')) show = false;
      // ANY registers are visible to every flavor; a concrete flavor filter
      // keeps them. Filtering by ANY shows only ANY-scoped rows.
      if (flavor === 'ANY' && !fl.includes('ANY')) show = false;
    }}
    if (show && cls && tr.dataset.class !== cls) show = false;
    if (show && access && tr.dataset.access !== access) show = false;
    tr.style.display = show ? '' : 'none';
    if (show) nHex++;
  }});
  document.querySelectorAll('#tText tbody tr').forEach(tr => {{
    nTextTotal++;
    const show = !needle || tr.dataset.search.includes(needle);
    tr.style.display = show ? '' : 'none';
    if (show) nText++;
  }});
  document.querySelectorAll('#tPid tbody tr').forEach(tr => {{
    nPidTotal++;
    const show = !needle || tr.dataset.search.includes(needle);
    tr.style.display = show ? '' : 'none';
    if (show) nPid++;
  }});
  document.getElementById('nHex').textContent = nHex + '/' + nHexTotal;
  document.getElementById('nText').textContent = nText + '/' + nTextTotal;
  document.getElementById('nPid').textContent = nPid + '/' + nPidTotal;
  counts.textContent = `Showing ${{nHex}} HEX registers, ${{nText}} text labels, ${{nPid}} product IDs.`;
}}

document.querySelectorAll('[data-tab]').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const tab = btn.dataset.tab;
    document.body.dataset.tab = tab;
    document.querySelectorAll('section[data-section]').forEach(s => {{
      s.classList.toggle('hidden', tab !== 'all' && s.dataset.section !== tab);
    }});
  }});
}});

['input', 'change'].forEach(ev => {{
  q.addEventListener(ev, applyFilters);
}});
[fFlavor, fClass, fAccess].forEach(el => el.addEventListener('change', applyFilters));
document.getElementById('clear').addEventListener('click', () => {{
  q.value = ''; fFlavor.value = ''; fClass.value = ''; fAccess.value = '';
  applyFilters();
  q.focus();
}});
applyFilters();
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a searchable/filterable HTML catalog of "
        "HEX registers, Text labels, and product IDs."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=REPO_ROOT / "docs" / "catalog.html",
        help="Output HTML file (default: docs/catalog.html)",
    )
    parser.add_argument(
        "--title",
        default="ve.direct — register & label catalog",
        help="HTML page title",
    )
    args = parser.parse_args(argv)

    out: Path = args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(args.title), encoding="utf-8")
    print(f"Wrote {out} ({len(REGISTERS)} registers, "
          f"{len(LABEL_INFO)} labels, {len(PID_MAP)} PIDs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
