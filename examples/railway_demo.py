"""Railway plan + picket table demo without requiring AutoCAD.

Demonstrates the fork's modernization features end to end on the ezdxf
backend: layer scheme, clothoid transition curve (Euler spiral), picket
(chainage) table, inquiry measurements, and a dimension style — the same
shape of workflow the `skills/railway-design` skill drives through MCP tools.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path

from ezdxf.math import EulerSpiral

from autocad_mcp.backends.ezdxf_backend import EzdxfBackend

RAILWAY_LAYERS = [
    ("RAIL-AXIS", 1, "CONTINUOUS", 0.35),      # ось пути — красный
    ("RAIL-EXISTING", 8, "HIDDEN", 0.18),       # существующие сети — серый
    ("RAIL-DESIGN", 3, "CONTINUOUS", 0.30),     # проектируемые — зелёный
    ("RAIL-PICKET", 2, "CONTINUOUS", 0.13),     # пикетаж — жёлтый
    ("RAIL-TABLE", 7, "CONTINUOUS", 0.13),      # таблицы — белый
]


def clothoid_polyline(radius: float, length: float, segments: int = 24):
    """Approximate a transition (clothoid) curve from infinity to `radius`."""
    spiral = EulerSpiral(radius * length)
    return [
        (point[0], point[1])
        for point in spiral.approximate(length, segments)
    ]


async def main() -> None:
    output_root = Path(
        os.environ.get("AUTOCAD_MCP_OUTPUT_ROOT", Path.cwd() / "demo-output")
    ).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    backend = EzdxfBackend()
    await backend.initialize()

    # 1. Railway layer scheme
    for name, color, linetype, lineweight in RAILWAY_LAYERS:
        result = await backend.layer_create(
            name, color=color, linetype=linetype, lineweight=lineweight
        )
        if not result.ok:
            raise RuntimeError(result.to_dict())

    # 2. Track axis: straight — clothoid — circular arc — clothoid — straight
    radius = 1200.0  # m, typical for mainline track
    transition = 120.0  # m transition length
    straight = [(0.0, 0.0), (300.0, 0.0)]
    entry = clothoid_polyline(radius, transition)
    axis_points = straight + [
        (300.0 + x, y) for x, y in entry[1:]
    ]
    axis = await backend.create_polyline(axis_points, closed=False, layer="RAIL-AXIS")
    if not axis.ok:
        raise RuntimeError(axis.to_dict())

    # 3. Picket table (chainage every 100 m)
    picks = [f"ПК{i}" for i in range(0, 4)]
    marks = [f"{i * 100:+d}.00" for i in range(0, 4)]
    table = await backend.table_create(
        x=0.0,
        y=-80.0,
        rows=2,
        cols=len(picks),
        row_height=8.0,
        col_width=30.0,
        title="Ведомость пикетажа оси пути",
        cells=[picks, marks],
        layer="RAIL-TABLE",
    )
    if not table.ok:
        raise RuntimeError(table.to_dict())

    # 4. Dimension style in drawing units
    style = await backend.dimstyle_create("SPDS-RAIL", {"dimtxt": 3.5, "dimasz": 1.8, "dimtad": 1})
    current = await backend.dimstyle_set_current("SPDS-RAIL")
    if not (style.ok and current.ok):
        raise RuntimeError(style.to_dict())

    # 5. Measure what we drew (inquiry tool operations)
    length = await backend.inquiry_length(axis.payload["handle"])
    bbox = await backend.inquiry_bbox()
    summary = await backend.inquiry_summary()
    if not (length.ok and bbox.ok and summary.ok):
        raise RuntimeError(length.to_dict())

    # 6. Save + render evidence
    dxf_path = output_root / "railway-demo.dxf"
    save = await backend.drawing_save(str(dxf_path))
    preview = await backend.drawing_render_preview(str(output_root / "railway-demo.png"))
    if not save.ok:
        raise RuntimeError(save.to_dict())

    print(json.dumps({
        "ok": True,
        "axis_handle": axis.payload["handle"],
        "axis_length_m": round(length.payload["length"], 3),
        "table_anchor": table.payload["anchor"],
        "extents": {"min": bbox.payload["min"], "max": bbox.payload["max"]},
        "entity_counts": summary.payload["by_type"],
        "dxf": str(dxf_path),
        "preview": preview.payload.get("path") if preview.ok else None,
        "preview_sha256": preview.payload.get("sha256") if preview.ok else None,
        "clothoid": {"radius_m": radius, "transition_m": transition,
                     "parameter_c": radius * transition},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
