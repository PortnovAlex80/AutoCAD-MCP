"""Tests for the v4.1 modernization: inquiry, styles, layouts, tables, xrefs,
splines, selection, explode/stretch, and DXF version handling."""

import math

import ezdxf
import pytest

from autocad_mcp.backends.ezdxf_backend import EzdxfBackend


@pytest.fixture
async def backend():
    b = EzdxfBackend()
    result = await b.initialize()
    assert result.ok
    return b


# ---------------------------------------------------------------------------
# Spline
# ---------------------------------------------------------------------------


class TestSpline:
    async def test_create_spline_returns_handle_and_readback(self, backend):
        points = [[0, 0], [10, 15], [25, 5], [40, 20]]
        r = await backend.create_spline(points, degree=3)
        assert r.ok, r.error
        handle = r.payload["handle"]
        assert r.payload["entity_type"] == "SPLINE"
        readback = await backend.entity_get(handle)
        assert readback.ok
        assert readback.payload["type"] == "SPLINE"
        assert len(readback.payload["points"]) == 4
        assert readback.payload["degree"] == 3

    async def test_create_spline_clamps_degree(self, backend):
        r = await backend.create_spline([[0, 0], [5, 5], [10, 0]], degree=5)
        assert r.ok
        assert r.payload["degree"] == 2

    async def test_create_spline_requires_three_points(self, backend):
        r = await backend.create_spline([[0, 0], [10, 10]])
        assert not r.ok

    async def test_spline_contract_rejects_two_points(self):
        from autocad_mcp.contracts import build_entity_expectation

        with pytest.raises(ValueError):
            build_entity_expectation("spline", {"points": [[0, 0], [1, 1]]})


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


class TestSelect:
    async def test_select_by_type(self, backend):
        await backend.create_line(0, 0, 10, 0)
        await backend.create_line(0, 1, 10, 1)
        await backend.create_circle(50, 50, 5)
        r = await backend.entity_select({"type": "line"})
        assert r.ok
        assert r.payload["count"] == 2
        assert all(e["type"] == "LINE" for e in r.payload["entities"])

    async def test_select_by_layer(self, backend):
        await backend.layer_create("SEL")
        await backend.create_line(0, 0, 1, 0, layer="SEL")
        await backend.create_line(0, 0, 1, 0)
        r = await backend.entity_select({"layer": "SEL"})
        assert r.ok
        assert r.payload["count"] == 1
        assert r.payload["entities"][0]["layer"] == "SEL"

    async def test_select_by_window(self, backend):
        await backend.create_line(0, 0, 1, 0)
        await backend.create_line(1000, 1000, 1001, 1000)
        r = await backend.entity_select({"type": "line", "window": [-10, -10, 10, 10]})
        assert r.ok
        assert r.payload["count"] == 1

    async def test_select_rejects_bad_window(self, backend):
        r = await backend.entity_select({"window": [1, 2, 3]})
        assert not r.ok
        assert r.error_code == "E_PARAMETER_REJECTED"


# ---------------------------------------------------------------------------
# Explode / Stretch
# ---------------------------------------------------------------------------


class TestExplodeStretch:
    async def test_explode_closed_polyline_to_lines(self, backend):
        rect = await backend.create_rectangle(0, 0, 10, 10)
        r = await backend.entity_explode(rect.payload["handle"])
        assert r.ok, r.error
        assert r.payload["created"] == 4
        gone = await backend.entity_get(rect.payload["handle"])
        assert not gone.ok

    async def test_explode_polyline_with_bulge_rejected(self, backend):
        e = backend._msp.add_lwpolyline(
            [(0, 0, 0, 0, 0.5), (10, 0, 0, 0, 0)], format="xyseb"
        )
        r = await backend.entity_explode(e.dxf.handle)
        assert not r.ok
        assert r.error_code == "E_EXPLODE_UNSUPPORTED"

    async def test_explode_block_reference(self, backend):
        await backend.block_define("BLK", [{"type": "LINE", "x1": 0, "y1": 0, "x2": 5, "y2": 0}])
        insert = await backend.block_insert("BLK", 10, 10)
        r = await backend.entity_explode(insert.payload["handle"])
        assert r.ok, r.error
        assert r.payload["created"] == 1

    async def test_stretch_moves_vertices_inside_window(self, backend):
        pl = await backend.create_polyline([[0, 0], [10, 0], [10, 10]])
        handle = pl.payload["handle"]
        r = await backend.entity_stretch(handle, [9, -1, 11, 1], 5, 5)
        assert r.ok, r.error
        assert r.payload["vertices_moved"] == 1
        readback = await backend.entity_get(handle)
        assert readback.payload["points"] == [[0, 0], [15, 5], [10, 10]]

    async def test_stretch_line_endpoint(self, backend):
        line = await backend.create_line(0, 0, 10, 0)
        r = await backend.entity_stretch(line.payload["handle"], [8, -1, 12, 1], 0, 3)
        assert r.ok
        assert r.payload["vertices_moved"] == 1
        readback = await backend.entity_get(line.payload["handle"])
        assert readback.payload["end"] == [10, 3]


# ---------------------------------------------------------------------------
# Inquiry
# ---------------------------------------------------------------------------


class TestInquiry:
    async def test_distance(self, backend):
        r = await backend.inquiry_distance([0, 0], [3, 4])
        assert r.ok
        assert r.payload["distance"] == pytest.approx(5.0)
        assert r.payload["dx"] == 3
        assert r.payload["dy"] == 4

    async def test_area_from_points(self, backend):
        # 10x20 rectangle via shoelace
        r = await backend.inquiry_area(points=[[0, 0], [10, 0], [10, 20], [0, 20]])
        assert r.ok
        assert r.payload["area"] == pytest.approx(200.0)

    async def test_area_from_closed_polyline(self, backend):
        rect = await backend.create_rectangle(0, 0, 5, 5)
        r = await backend.inquiry_area(entity_id=rect.payload["handle"])
        assert r.ok, r.error
        assert r.payload["area"] == pytest.approx(25.0, abs=0.05)

    async def test_area_from_circle(self, backend):
        c = await backend.create_circle(0, 0, 10)
        r = await backend.inquiry_area(entity_id=c.payload["handle"])
        assert r.ok
        assert r.payload["area"] == pytest.approx(math.pi * 100, rel=0.02)

    async def test_area_open_polyline_rejected(self, backend):
        pl = await backend.create_polyline([[0, 0], [10, 0], [10, 10]], closed=False)
        r = await backend.inquiry_area(entity_id=pl.payload["handle"])
        assert not r.ok
        assert r.error_code == "E_OPEN_BOUNDARY"

    async def test_angle(self, backend):
        r = await backend.inquiry_angle([0, 0], [10, 0], [0, 10])
        assert r.ok
        assert r.payload["angle"] == pytest.approx(90.0)

    async def test_length_line(self, backend):
        line = await backend.create_line(0, 0, 6, 8)
        r = await backend.inquiry_length(line.payload["handle"])
        assert r.ok
        assert r.payload["length"] == pytest.approx(10.0)

    async def test_length_circle(self, backend):
        c = await backend.create_circle(0, 0, 5)
        r = await backend.inquiry_length(c.payload["handle"])
        assert r.ok
        assert r.payload["length"] == pytest.approx(2 * math.pi * 5, rel=0.02)

    async def test_bbox_of_all(self, backend):
        await backend.create_line(0, 0, 10, 0)
        await backend.create_line(100, 200, 150, 250)
        r = await backend.inquiry_bbox()
        assert r.ok
        assert r.payload["min"] == [0, 0]
        assert r.payload["max"] == [150, 250]

    async def test_summary_counts(self, backend):
        await backend.layer_create("SUM")
        await backend.create_line(0, 0, 1, 0)
        await backend.create_line(0, 0, 1, 0, layer="SUM")
        await backend.create_circle(0, 0, 1, layer="SUM")
        r = await backend.inquiry_summary()
        assert r.ok
        assert r.payload["total"] == 3
        assert r.payload["by_type"]["LINE"] == 2
        assert r.payload["by_type"]["CIRCLE"] == 1
        assert r.payload["by_layer"]["SUM"] == 2


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------


class TestStyles:
    async def test_textstyle_create_and_list(self, backend):
        r = await backend.textstyle_create("GOST-2.5", font="gosttypeb.ttf", fixed_height=2.5)
        assert r.ok, r.error
        listing = await backend.textstyle_list()
        names = [s["name"] for s in listing.payload["text_styles"]]
        assert "GOST-2.5" in names
        entry = next(s for s in listing.payload["text_styles"] if s["name"] == "GOST-2.5")
        assert entry["font"] == "gosttypeb.ttf"

    async def test_textstyle_set_current(self, backend):
        await backend.textstyle_create("CUR")
        r = await backend.textstyle_set_current("CUR")
        assert r.ok
        unknown = await backend.textstyle_set_current("NOPE")
        assert not unknown.ok

    async def test_dimstyle_create_list_and_use(self, backend):
        r = await backend.dimstyle_create("SPDS", {"dimtxt": 2.5, "dimasz": 1.0, "bogus": 1})
        assert r.ok
        assert r.payload["applied"] == {"dimtxt": 2.5, "dimasz": 1.0}
        assert r.payload["rejected"] == ["bogus"]
        set_current = await backend.dimstyle_set_current("SPDS")
        assert set_current.ok
        dim = await backend.create_dimension_linear(0, 0, 50, 0, 25, 10)
        assert dim.ok
        listing = await backend.dimstyle_list()
        assert listing.payload["current"] == "SPDS"

    async def test_linetype_create(self, backend):
        r = await backend.linetype_create("RAIL-DASH", [2.0, 1.25, -0.25, 0.25, -0.25], "Rail dash")
        assert r.ok
        listing = await backend.linetype_list()
        assert "RAIL-DASH" in [lt["name"] for lt in listing.payload["linetypes"]]


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------


class TestLayouts:
    async def test_layout_create_list_set_current(self, backend):
        created = await backend.layout_create("Profile-A3")
        assert created.ok
        listing = await backend.layout_list()
        assert "Profile-A3" in listing.payload["layouts"]
        current = await backend.layout_set_current("Profile-A3")
        assert current.ok

    async def test_layout_create_duplicate_fails_cleanly(self, backend):
        await backend.layout_create("DUP")
        r = await backend.layout_create("DUP")
        assert not r.ok

    async def test_add_viewport(self, backend):
        await backend.layout_create("VP")
        r = await backend.layout_add_viewport(
            "VP", center=[148, 105], width=270, height=180,
            view_center=[0, 0], view_height=1800, layer="0",
        )
        assert r.ok, r.error
        assert r.payload["scale"] == pytest.approx(0.1)
        readback = await backend.entity_get(r.payload["handle"])
        assert readback.ok
        assert readback.payload["type"] == "VIEWPORT"
        assert readback.payload["view_height"] == pytest.approx(1800)

    async def test_add_viewport_unknown_layout(self, backend):
        r = await backend.layout_add_viewport(
            "NOPE", center=[0, 0], width=10, height=10, view_center=[0, 0], view_height=10
        )
        assert not r.ok


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class TestTables:
    async def test_table_create_grid_and_cells(self, backend):
        cells = [["ПК", "Отметка"], ["0+00", "100.00"], ["1+00", "100.50"]]
        r = await backend.table_create(0, 0, rows=3, cols=2, row_height=8, col_width=30, cells=cells)
        assert r.ok, r.error
        assert r.payload["representation"] == "composite_grid"
        anchor = r.payload["anchor"]
        # (rows+1) horizontal + (cols+1) vertical lines
        assert len(r.payload["grid_handles"]) == 7
        cell_1_1 = await backend.entity_get(r.payload["cell_handles"][1][1])
        assert cell_1_1.payload["text"] == "100.00"

    async def test_table_set_cell(self, backend):
        r = await backend.table_create(0, 0, rows=2, cols=2)
        anchor = r.payload["anchor"]
        update = await backend.table_set_cell(anchor, 0, 0, "HEADER")
        assert update.ok
        readback = await backend.entity_get(r.payload["cell_handles"][0][0])
        assert readback.payload["text"] == "HEADER"

    async def test_table_set_cell_out_of_range(self, backend):
        r = await backend.table_create(0, 0, rows=2, cols=2)
        anchor = r.payload["anchor"]
        update = await backend.table_set_cell(anchor, 5, 5, "X")
        assert not update.ok
        assert update.error_code == "E_PARAMETER_REJECTED"

    async def test_table_set_col_widths_rebuilds(self, backend):
        r = await backend.table_create(0, 0, rows=2, cols=2, cells=[["a", "b"], ["c", "d"]])
        old_anchor = r.payload["anchor"]
        rebuilt = await backend.table_set_col_widths(old_anchor, [40, 20])
        assert rebuilt.ok, rebuilt.error
        new_anchor = rebuilt.payload["anchor"]
        assert new_anchor != old_anchor
        cell_0_0 = await backend.entity_get(
            (await self._manifest_cell(backend, new_anchor, 0, 0))
        )
        assert cell_0_0.payload["text"] == "a"
        # Old anchor is no longer a table
        stale = await backend.table_set_cell(old_anchor, 0, 0, "X")
        assert not stale.ok

    @staticmethod
    async def _manifest_cell(backend, anchor, row, col):
        manifest = backend._table_manifests[anchor]
        return manifest["cells"][row][col]

    async def test_table_set_row_heights(self, backend):
        r = await backend.table_create(0, 0, rows=2, cols=1)
        rebuilt = await backend.table_set_row_heights(r.payload["anchor"], [5, 15])
        assert rebuilt.ok
        assert rebuilt.payload["row_heights"] == [5.0, 15.0]

    async def test_table_size_bounds(self, backend):
        r = await backend.table_create(0, 0, rows=0, cols=2)
        assert not r.ok
        assert r.error_code == "E_PARAMETER_REJECTED"


# ---------------------------------------------------------------------------
# XRefs
# ---------------------------------------------------------------------------


@pytest.fixture
def xref_source(tmp_path):
    doc = ezdxf.new("R2013")
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0))
    msp.add_circle((50, 50), 25)
    path = tmp_path / "survey-base.dxf"
    doc.saveas(path)
    return path


class TestXref:
    async def test_attach_list_detach(self, backend, xref_source):
        r = await backend.xref_attach(str(xref_source), x=0, y=0)
        assert r.ok, r.error
        assert r.payload["name"] == "survey-base"
        listing = await backend.xref_list()
        names = [x["name"] for x in listing.payload["xrefs"]]
        assert "survey-base" in names
        detach = await backend.xref_detach("survey-base")
        assert detach.ok, detach.error
        after = await backend.xref_list()
        assert all(x["name"] != "survey-base" for x in after.payload["xrefs"])

    async def test_attach_missing_file(self, backend):
        r = await backend.xref_attach("Z:/nowhere/missing.dxf")
        assert not r.ok
        assert r.error_code == "E_FILE_NOT_FOUND"

    async def test_reload(self, backend, xref_source):
        await backend.xref_attach(str(xref_source))
        r = await backend.xref_reload("survey-base")
        assert r.ok, r.error

    async def test_detach_unknown(self, backend):
        r = await backend.xref_detach("ghost")
        assert not r.ok
        assert r.error_code == "E_XREF_NOT_FOUND"


# ---------------------------------------------------------------------------
# DXF version handling
# ---------------------------------------------------------------------------


class TestDxfVersions:
    async def test_create_with_version(self, backend):
        r = await backend.drawing_create("V2018", version="R2018")
        assert r.ok
        assert backend._doc.dxfversion == "AC1032"

    async def test_create_rejects_unknown_version(self, backend):
        r = await backend.drawing_create("BAD", version="R3000")
        assert not r.ok
        assert r.error_code == "E_PARAMETER_REJECTED"

    async def test_save_as_dxf_converts_to_r2018(self, backend, tmp_path):
        await backend.create_line(0, 0, 10, 0)
        target = tmp_path / "converted.dxf"
        r = await backend.drawing_save_as_dxf(str(target), version="R2018")
        assert r.ok, r.error
        assert r.payload["converted"] is True
        reopened = ezdxf.readfile(str(target))
        assert reopened.dxfversion == "AC1032"
        assert len(reopened.modelspace()) == 1

    async def test_save_as_dxf_same_version(self, backend, tmp_path):
        await backend.create_line(0, 0, 10, 0)
        target = tmp_path / "same.dxf"
        r = await backend.drawing_save_as_dxf(str(target))
        assert r.ok
        assert r.payload["converted"] is False
