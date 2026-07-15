#!/usr/bin/env python3
"""
eagle_to_jlcpcb.py

Converts an Eagle .brd file into two JLCPCB-compatible .xlsx files:
  - <name>-JLCPCB-BOM.xlsx   (Comment, Designator, Footprint, JLCPCB Part #)
  - <name>-JLCPCB-CPL.xlsx   (Designator, Mid X, Mid Y, Layer, Rotation)

Usage:
    python3 eagle_to_jlcpcb.py MyBoard.brd
    python3 eagle_to_jlcpcb.py MyBoard.brd --outdir /path/to/output

Requirements:
    pip install openpyxl
"""

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

try:
    import openpyxl
except ImportError:
    sys.exit("Error: openpyxl is required.  Install with:  pip install openpyxl")


# ---------------------------------------------------------------------------
# Value normaliser — convert shorthand to JLC-friendly style
# ---------------------------------------------------------------------------
def normalise_value(val):
    """
    Normalise component values into JLC-preferred human-readable form.

    Resistors  (R = decimal point for ohms, K/M for multipliers):
        0R   -> 0R          1K0  -> 1K         4K7  -> 4.7K
        100R -> 100R        10K  -> 10K        2M2  -> 2.2M
        51R  -> 51R         5K1  -> 5.1K

    Capacitors (P/N/U = pF/nF/uF decimal point):
        100N -> 100nF       3N3  -> 3.3nF      1N5  -> 1.5nF
        10P  -> 10pF        4P7  -> 4.7pF
        4U7  -> 4.7uF       10U  -> 10uF

    Inductors (same pattern with H):
        100N already handled as nF; explicit nH/uH left alone.

    Values already in standard form (100nF, 1uF, 10K) pass through.
    Non-passive values (IC part numbers etc.) are returned unchanged.
    """
    if not val:
        return val

    s = val.strip()

    # Already standard form with explicit unit suffix — leave alone
    # e.g. 100nF, 4.7uF, 100pF, 100nH, 10K, 2.2M
    if re.match(r'^[\d.]+\s*(pF|nF|uF|µF|mF|pH|nH|uH|µH|mH|[kK]?[Ωo]hm)$', s, re.IGNORECASE):
        return s

    # --- Capacitor shorthand: XNY -> X.YnF, XPY -> X.YpF, XUY -> X.YuF ---
    # e.g. 3N3 -> 3.3nF, 100N -> 100nF, 1N5 -> 1.5nF, 4P7 -> 4.7pF
    cap_map = {'N': 'nF', 'P': 'pF', 'U': 'uF'}
    for letter, unit in cap_map.items():
        # Pattern: digits, letter, digits  (e.g. 3N3, 1P5)
        m = re.match(rf'^(\d+){letter}(\d+)$', s, re.IGNORECASE)
        if m:
            return f"{m.group(1)}.{m.group(2)}{unit}"

        # Pattern: digits then letter alone  (e.g. 100N, 10P, 22U)
        m = re.match(rf'^(\d+){letter}$', s, re.IGNORECASE)
        if m:
            return f"{m.group(1)}{unit}"

    # --- Resistor shorthand: XRY -> X.YR, XKY -> X.YK, XMY -> X.YM ---
    # e.g. 4K7 -> 4.7K, 1K0 -> 1K, 0R -> 0R, 100R -> 100R, 2M2 -> 2.2M
    res_map = {'R': 'R', 'K': 'K', 'M': 'M'}
    for letter, unit in res_map.items():
        # With fractional part: 4K7 -> 4.7K, 1R5 -> 1.5R
        m = re.match(rf'^(\d+){letter}(\d+)$', s, re.IGNORECASE)
        if m:
            frac = m.group(2)
            if frac == '0':
                return f"{m.group(1)}{unit}"
            return f"{m.group(1)}.{frac}{unit}"

        # Plain: 100R, 10K, 0R
        m = re.match(rf'^(\d+){letter}$', s, re.IGNORECASE)
        if m:
            return f"{m.group(1)}{unit}"

    # --- Inductor shorthand with L: XLY -> X.YuH ---
    m = re.match(r'^(\d+)L(\d+)$', s, re.IGNORECASE)
    if m:
        frac = m.group(2)
        if frac == '0':
            return f"{m.group(1)}uH"
        return f"{m.group(1)}.{frac}uH"

    # No match — return as-is (IC part numbers, connectors, etc.)
    return val


# ---------------------------------------------------------------------------
# Eagle rotation parser
# ---------------------------------------------------------------------------
def parse_rotation(rot_str):
    """
    Parse Eagle rotation string (e.g. 'R90', 'R270', 'MR180', 'SR0').
    Returns (angle_degrees, is_mirrored).
      - M prefix  = component is on the bottom layer (mirrored)
      - S prefix  = spin flag (ignored for placement purposes)
      - R prefix  = rotation in degrees
    """
    if not rot_str:
        return 0.0, False

    mirrored = False
    s = rot_str

    if s.startswith("M"):
        mirrored = True
        s = s[1:]
    if s.startswith("S"):
        s = s[1:]
    if s.startswith("R"):
        s = s[1:]

    try:
        angle = float(s)
    except ValueError:
        angle = 0.0

    # Normalise to 0-360
    angle = angle % 360
    return angle, mirrored


# ---------------------------------------------------------------------------
# Extract components from Eagle .brd XML
# ---------------------------------------------------------------------------
def extract_components(brd_path):
    """
    Returns a list of dicts with keys:
        name, value, package, x, y, rotation, layer
    """
    tree = ET.parse(brd_path)
    root = tree.getroot()

    components = []
    for el in root.iter("element"):
        name = el.get("name", "")
        value = el.get("value", "")
        package = el.get("package", "")
        x = float(el.get("x", 0))
        y = float(el.get("y", 0))
        rot_str = el.get("rot", "R0")

        angle, mirrored = parse_rotation(rot_str)

        components.append({
            "name": name,
            "value": normalise_value(value),
            "package": package,
            "x": x,
            "y": y,
            "rotation": angle,
            "layer": "Bottom" if mirrored else "Top",
        })

    return components


# ---------------------------------------------------------------------------
# Write CPL
# ---------------------------------------------------------------------------
def write_cpl(components, out_path):
    """Write JLCPCB-format CPL (pick-and-place) xlsx."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])

    for c in sorted(components, key=lambda c: c["name"]):
        mid_x = f"{c['x']:.4f}mm"
        mid_y = f"{c['y']:.4f}mm"
        rot = f"{c['rotation']:g}"
        ws.append([c["name"], mid_x, mid_y, c["layer"], rot])

    wb.save(out_path)
    return len(components)


# ---------------------------------------------------------------------------
# Group & write BOM
# ---------------------------------------------------------------------------
def write_bom(components, out_path):
    """
    Write JLCPCB-format BOM xlsx.
    Components are grouped by (value, package) — same as Eagle BOM export.
    """
    groups = defaultdict(list)
    for c in components:
        key = (c["value"], c["package"])
        groups[key].append(c["name"])

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["Comment", "Designator", "Footprint", "JLCPCB Part #（optional）"])

    # Sort groups by first designator for consistency
    def sort_key(item):
        # Natural sort: C1 before C10
        first = item[1][0]
        parts = re.match(r"([A-Za-z]+)(\d+)", first)
        if parts:
            return (parts.group(1), int(parts.group(2)))
        return (first, 0)

    for (value, package), names in sorted(groups.items(), key=sort_key):
        # Sort designators naturally within each group
        def nat(n):
            m = re.match(r"([A-Za-z]+)(\d+)", n)
            return (m.group(1), int(m.group(2))) if m else (n, 0)

        names_sorted = sorted(names, key=nat)
        designators = ", ".join(names_sorted)
        ws.append([value, designators, package, ""])

    wb.save(out_path)
    return len(groups)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Convert Eagle .brd to JLCPCB BOM + CPL (.xlsx)"
    )
    parser.add_argument("brd_file", help="Path to Eagle .brd file")
    parser.add_argument(
        "--outdir", default=".", help="Output directory (default: current directory)"
    )
    args = parser.parse_args()

    if not os.path.isfile(args.brd_file):
        sys.exit(f"Error: file not found: {args.brd_file}")

    base = os.path.splitext(os.path.basename(args.brd_file))[0]
    os.makedirs(args.outdir, exist_ok=True)

    bom_path = os.path.join(args.outdir, f"{base}-JLCPCB-BOM.xlsx")
    cpl_path = os.path.join(args.outdir, f"{base}-JLCPCB-CPL.xlsx")

    print(f"Reading:  {args.brd_file}")
    components = extract_components(args.brd_file)
    print(f"Found {len(components)} components")

    n_cpl = write_cpl(components, cpl_path)
    print(f"CPL:      {cpl_path}  ({n_cpl} entries)")

    n_bom = write_bom(components, bom_path)
    print(f"BOM:      {bom_path}  ({n_bom} unique groups)")

    print("Done!")


if __name__ == "__main__":
    main()
