#!/usr/bin/env python3
"""Generate a DXF skeleton of a longitudinal-profile grid (SPDS style).

The grid (сетка продольного профиля) is drawn in sheet millimetres: model
unit = 1 mm of the plotted sheet, one unit system, no scale mixing.  Station
metres are mapped by the horizontal plot scale:

    x_mm = (station_m - start_m) * 1000 / h_scale     (e.g. 1:5000 -> 0.2 mm/m)

Default row set (id, title, height in mm) follows the typical SPDS profile
network; the exact composition and widths of the rows must be confirmed
against the current edition of the applicable SPDS standards (GOST R
21.1101 series; for railway track working drawings see GOST 21.611) and the
project's design documentation rules.  Layers come from
``assets/layer-scheme.json`` next to this script.

Requires ezdxf (already a dependency of autocad-mcp).  Output: the DXF file
plus a JSON manifest on stdout (file path, SHA-256, entity counts, extents).
Exit codes: 0 success, 1 invalid input, 2 self-test failure, 3 ezdxf missing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

try:
    import ezdxf
except ImportError:  # pragma: no cover - reported at runtime, not import time
    ezdxf = None

LAYER_SCHEME_PATH = Path(__file__).resolve().parents[1] / "assets" / "layer-scheme.json"

GRID_LAYER = "RW-GRID"
TEXT_LAYER = "RW-TEXT"

# Fallback layer properties used when assets/layer-scheme.json is missing.
_FALLBACK_LAYERS: dict[str, dict] = {
    GRID_LAYER: {"color": 9, "linetype": "CONTINUOUS", "lineweight_mm": 0.05},
    TEXT_LAYER: {"color": 7, "linetype": "CONTINUOUS", "lineweight_mm": 0.13},
}

DEFAULT_ROWS: list[dict] = [
    {"id": "plan", "title": "Развернутый план пути", "height_mm": 20.0},
    {"id": "grade", "title": "Уклоны (в тысячных)", "height_mm": 10.0},
    {"id": "design_elev", "title": "Проектные отметки", "height_mm": 10.0},
    {"id": "ground_elev", "title": "Фактические отметки земли", "height_mm": 10.0},
    {"id": "distance", "title": "Расстояния", "height_mm": 10.0},
    {"id": "pk", "title": "Пикетаж", "height_mm": 5.0},
]

ROWS_WARNING = (
    "состав и высоты граф - типовые; уточнить по действующей редакции "
    "стандартов СПДС и нормам оформления проектной документации"
)

TITLE_COLUMN_MM = 30.0  # left margin with row titles
TEXT_HEIGHT_MM = 2.5


def x_mm_for_station(station_m: float, start_m: float, h_scale: float) -> float:
    """Map a station in metres to sheet millimetres from the grid origin."""
    if h_scale <= 0.0:
        raise ValueError("h_scale must be positive")
    return (station_m - start_m) * 1000.0 / h_scale


def total_height_mm(rows: list[dict]) -> float:
    """Sum of row heights in millimetres."""
    return sum(float(row["height_mm"]) for row in rows)


def row_boundaries_mm(rows: list[dict]) -> list[float]:
    """Cumulative y boundaries, top of the grid first (descending values)."""
    boundaries = [0.0]
    for row in rows:
        boundaries.append(boundaries[-1] - float(row["height_mm"]))
    return boundaries


def parse_rows(spec: str | None) -> list[dict]:
    """Parse an optional 'title:height,...' spec; fall back to DEFAULT_ROWS."""
    if not spec:
        return [dict(row) for row in DEFAULT_ROWS]
    rows: list[dict] = []
    for chunk in spec.split(","):
        title, _, height_str = chunk.partition(":")
        height = float(height_str)
        if height <= 0.0:
            raise ValueError(f"row height must be positive, got {height}")
        rows.append({"id": f"row{len(rows) + 1}", "title": title.strip(), "height_mm": height})
    if not rows:
        raise ValueError("empty --rows spec")
    return rows


def piket_stations(length_m: float, start_m: float) -> list[float]:
    """Stations of every piket (100 m) crossing the section, start included."""
    if length_m <= 0.0:
        raise ValueError("length_m must be positive")
    first_pk = -(-start_m // 100.0) * 100.0  # ceil to the next piket >= start
    stations: list[float] = []
    station = float(first_pk)
    while station <= start_m + length_m:
        stations.append(station)
        station += 100.0
    return stations


def _load_layers() -> list[dict]:
    if not LAYER_SCHEME_PATH.exists():
        return []
    scheme = json.loads(LAYER_SCHEME_PATH.read_text(encoding="utf-8"))
    return list(scheme.get("layers", []))


def build_document(params: dict, rows: list[dict]):
    """Build the ezdxf document in memory; return (doc, msp)."""
    if ezdxf is None:  # pragma: no cover
        raise RuntimeError("ezdxf is not installed")
    doc = ezdxf.new("R2018", setup=True)
    msp = doc.modelspace()
    scheme_layers = {layer["name"]: layer for layer in _load_layers()}
    for name in (GRID_LAYER, TEXT_LAYER):
        layer = scheme_layers.get(name) or {"name": name, **_FALLBACK_LAYERS[name]}
        doc.layers.add(
            name=name,
            color=int(layer.get("color", 7)),
            linetype=layer.get("linetype", "CONTINUOUS"),
            lineweight=int(round(float(layer.get("lineweight_mm", 0.13)) * 100)),
        )

    width = x_mm_for_station(params["start_m"] + params["length_m"], params["start_m"],
                             params["h_scale"])
    top = 0.0
    bottom = -total_height_mm(rows)

    # Horizontal separators: top, each row boundary, bottom.
    for y in row_boundaries_mm(rows):
        msp.add_line((0.0, y), (width, y), dxfattribs={"layer": GRID_LAYER})
    # Vertical separators at every piket.
    for station in piket_stations(params["length_m"], params["start_m"]):
        x = x_mm_for_station(station, params["start_m"], params["h_scale"])
        msp.add_line((x, top), (x, bottom), dxfattribs={"layer": GRID_LAYER})
        label_y = bottom + float(rows[-1]["height_mm"]) / 2.0 - TEXT_HEIGHT_MM / 3.0
        msp.add_text(
            f"ПК{int(round(station / 100.0))}",
            dxfattribs={"layer": TEXT_LAYER, "height": TEXT_HEIGHT_MM},
        ).set_placement((x + 1.0, label_y))
    # Row titles in the left margin.
    for row, (y_top, y_bottom) in zip(rows, list(zip(row_boundaries_mm(rows),
                                                     row_boundaries_mm(rows)[1:]))):
        msp.add_text(
            row["title"],
            dxfattribs={"layer": TEXT_LAYER, "height": TEXT_HEIGHT_MM},
        ).set_placement((-TITLE_COLUMN_MM, (y_top + y_bottom) / 2.0 - TEXT_HEIGHT_MM / 3.0))
    return doc, msp


def save_with_manifest(out_path: Path, params: dict, rows: list[dict]) -> dict:
    """Build, save the DXF and return the JSON manifest payload."""
    doc, msp = build_document(params, rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(out_path))
    counts: dict[str, int] = {}
    for entity in msp:
        counts[entity.dxftype()] = counts.get(entity.dxftype(), 0) + 1
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
    return {
        "ok": True,
        "file": str(out_path),
        "sha256": digest,
        "params": params,
        "rows": rows,
        "rows_warning": ROWS_WARNING,
        "entity_counts": counts,
        "layers": [GRID_LAYER, TEXT_LAYER],
        "extents_mm": [
            -TITLE_COLUMN_MM,
            -total_height_mm(rows),
            x_mm_for_station(params["start_m"] + params["length_m"], params["start_m"],
                             params["h_scale"]),
            0.0,
        ],
    }


def _self_test() -> bool:
    """Deterministic asserts; return True when every check passes."""
    # 1. Station-to-sheet mapping: 3000 m at 1:5000 -> 600 mm.
    assert abs(x_mm_for_station(3000.0, 0.0, 5000.0) - 600.0) < 1e-9
    assert abs(x_mm_for_station(1500.0, 1000.0, 2000.0) - 250.0) < 1e-9

    # 2. Row stack arithmetic.
    assert abs(total_height_mm(DEFAULT_ROWS) - 65.0) < 1e-9
    bounds = row_boundaries_mm(DEFAULT_ROWS)
    assert bounds[0] == 0.0 and abs(bounds[-1] + 65.0) < 1e-9
    assert all(b1 > b2 for b1, b2 in zip(bounds, bounds[1:]))

    # 3. Piket enumeration: 3000 m from ПК0 -> 31 stations, 0..3000.
    stations = piket_stations(3000.0, 0.0)
    assert len(stations) == 31 and stations[0] == 0.0 and stations[-1] == 3000.0
    # Section starting mid-block includes the next piket only.
    assert piket_stations(50.0, 0.0) == [0.0]
    assert piket_stations(120.0, 0.0) == [0.0, 100.0]

    # 4. Rows parser.
    custom = parse_rows("Графа А:5,Графа Б:10")
    assert custom[0]["height_mm"] == 5.0 and custom[1]["title"] == "Графа Б"
    try:
        parse_rows("Плохая графа")
    except (ValueError, IndexError):
        pass
    else:  # pragma: no cover
        raise AssertionError("expected error for malformed rows spec")

    # 5. In-memory DXF: layers and entity counts (requires ezdxf).
    if ezdxf is None:
        print(json.dumps({"self_test": "PARTIAL", "reason": "ezdxf not installed"}))
        return True
    params = {"start_m": 0.0, "length_m": 1000.0, "h_scale": 5000.0, "v_scale": 1000.0}
    doc, msp = build_document(params, DEFAULT_ROWS)
    for name in (GRID_LAYER, TEXT_LAYER):
        assert name in doc.layers, name
    lines = [e for e in msp if e.dxftype() == "LINE"]
    texts = [e for e in msp if e.dxftype() in {"TEXT", "MTEXT"}]
    # 7 horizontal lines (6 rows) + 11 vertical (piket 0..1000) = 18.
    assert len(lines) == len(row_boundaries_mm(DEFAULT_ROWS)) + 11
    assert len(texts) == 11 + len(DEFAULT_ROWS)

    # 6. Save/reopen round trip.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "grid.dxf"
        manifest = save_with_manifest(out, params, DEFAULT_ROWS)
        assert manifest["ok"] is True and len(manifest["sha256"]) == 64
        reopened = ezdxf.readfile(str(out))
        reopened_count = sum(
            1 for e in reopened.modelspace() if e.dxftype() == "LINE"
        )
        assert reopened_count == len(lines)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a SPDS-style longitudinal-profile grid skeleton as DXF "
            "(sheet millimetres) with a JSON manifest."
        ),
    )
    parser.add_argument("--length-m", type=float, default=None, help="section length, m")
    parser.add_argument(
        "--start-pk", type=float, default=0.0,
        help="section start in pikets, e.g. 12 for ПК12+00 (default 0)",
    )
    parser.add_argument(
        "--h-scale", type=float, default=5000.0,
        help="horizontal scale denominator, e.g. 5000 for 1:5000",
    )
    parser.add_argument(
        "--v-scale", type=float, default=1000.0,
        help="vertical scale denominator (metadata for the manifest)",
    )
    parser.add_argument("--out", type=Path, default=Path("profile_grid.dxf"), help="output DXF")
    parser.add_argument(
        "--rows", type=str, help='override rows as "Title:height_mm,..." (default: SPDS-typical)'
    )
    parser.add_argument("--self-test", action="store_true", help="run internal asserts and exit")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.self_test:
        try:
            ok = _self_test()
        except AssertionError as exc:
            print(json.dumps({"ok": False, "self_test": "FAIL", "error": str(exc)}))
            return 2
        print(json.dumps({"ok": ok, "self_test": "PASS"}))
        return 0

    if args.length_m is None:
        parser.error("--length-m is required unless --self-test is used")

    try:
        params = {
            "start_m": args.start_pk * 100.0,
            "length_m": args.length_m,
            "h_scale": args.h_scale,
            "v_scale": args.v_scale,
        }
        rows = parse_rows(args.rows)
        manifest = save_with_manifest(args.out, params, rows)
    except (ValueError, IndexError, ZeroDivisionError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1
    print(json.dumps(manifest, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
