"""Python-side contract tests for the new live file_ipc operations.

Each new backend method must send exactly one IPC command that the LISP
dispatcher whitelists, with the exact param keys the LISP JSON helpers
parse. ``_dispatch`` is monkeypatched, so no AutoCAD instance is needed.
Pure-math inquiries (distance/angle/point area) are checked for real math.
"""

import math
import re
from pathlib import Path

import pytest

from autocad_mcp.backends.base import CommandResult
from autocad_mcp.backends.file_ipc import DIMSTYLE_NUMERIC_FIELDS, FileIPCBackend

LISP_DISPATCH = Path(__file__).resolve().parents[1] / "lisp-code" / "mcp_dispatch.lsp"


class RecordingDispatcher:
    """Stands in for _dispatch: records (command, params) and replays payloads."""

    def __init__(self, payloads=None, error=None):
        self.calls: list[tuple[str, dict]] = []
        self._payloads = payloads or {}
        self._error = error

    async def __call__(self, command, params):
        self.calls.append((command, params))
        if self._error is not None:
            return CommandResult(ok=False, error=self._error)
        return CommandResult(ok=True, payload=self._payloads.get(command, {}))


@pytest.fixture
def backend():
    return FileIPCBackend()


def install(backend, monkeypatch, payloads=None, error=None):
    dispatcher = RecordingDispatcher(payloads, error)
    monkeypatch.setattr(backend, "_dispatch", dispatcher)
    return dispatcher


def last(dispatcher):
    command, params = dispatcher.calls[-1]
    return command, params


def sent_params(dispatcher):
    """Params as they land in the JSON command file.

    ``_dispatch_unlocked`` strips None values because the LISP JSON parser
    cannot handle null; the recorder sits in front of that step.
    """
    _, params = dispatcher.calls[-1]
    return {key: value for key, value in params.items() if value is not None}


# ---------------------------------------------------------------------------
# Inquiry — pure Python math (no IPC round trip)
# ---------------------------------------------------------------------------


class TestInquiryPureMath:
    async def test_distance(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch)
        r = await backend.inquiry_distance([0, 0], [3, 4])
        assert r.ok
        assert r.payload["distance"] == 5.0
        assert r.payload["dx"] == 3.0
        assert r.payload["dy"] == 4.0
        assert r.payload["angle"] == pytest.approx(53.13010235415598)
        assert dispatcher.calls == []  # never touched AutoCAD

    async def test_distance_rejects_bad_points(self, backend, monkeypatch):
        install(backend, monkeypatch)
        r = await backend.inquiry_distance([0, 0], ["x", 1])
        assert not r.ok
        assert "must be [x, y]" in r.error

    async def test_angle_right_angle(self, backend, monkeypatch):
        install(backend, monkeypatch)
        r = await backend.inquiry_angle([0, 0], [1, 0], [0, 1])
        assert r.ok
        assert r.payload["angle"] == pytest.approx(90.0)
        assert r.payload["angle_acute"] == pytest.approx(90.0)

    async def test_angle_reflex_is_reported_acute(self, backend, monkeypatch):
        install(backend, monkeypatch)
        r = await backend.inquiry_angle([0, 0], [1, 0], [0, -1])
        assert r.ok
        assert r.payload["angle"] == pytest.approx(270.0)
        assert r.payload["angle_acute"] == pytest.approx(90.0)

    async def test_angle_rejects_bad_points(self, backend, monkeypatch):
        install(backend, monkeypatch)
        r = await backend.inquiry_angle([0, 0], [1, None], [0, 1])
        assert not r.ok

    async def test_area_from_points_shoelace(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch)
        r = await backend.inquiry_area(points=[[0, 0], [4, 0], [4, 4], [0, 4]])
        assert r.ok
        assert r.payload["source"] == "points"
        assert r.payload["area"] == pytest.approx(16.0)
        assert r.payload["count"] == 4
        assert dispatcher.calls == []

    async def test_area_from_points_l_shape(self, backend, monkeypatch):
        install(backend, monkeypatch)
        r = await backend.inquiry_area(points=[[0, 0], [2, 0], [2, 1], [1, 1], [1, 2], [0, 2]])
        assert r.ok
        assert r.payload["area"] == pytest.approx(3.0)

    async def test_area_requires_three_points(self, backend, monkeypatch):
        install(backend, monkeypatch)
        r = await backend.inquiry_area(points=[[0, 0], [1, 1]])
        assert not r.ok

    async def test_area_requires_points_or_entity(self, backend, monkeypatch):
        install(backend, monkeypatch)
        r = await backend.inquiry_area()
        assert not r.ok
        assert "entity_id or points" in r.error


# ---------------------------------------------------------------------------
# Inquiry — dispatched to the LISP dispatcher
# ---------------------------------------------------------------------------


class TestInquiryDispatched:
    async def test_length_sends_measure_length(self, backend, monkeypatch):
        dispatcher = install(
            backend, monkeypatch, {"measure-length": {"entity_id": "1A2", "type": "LINE", "length": 10.0}}
        )
        r = await backend.inquiry_length("1A2")
        assert r.ok
        command, params = last(dispatcher)
        assert command == "measure-length"
        assert params == {"entity_id": "1A2"}
        assert r.payload["length"] == 10.0

    async def test_length_error_passthrough(self, backend, monkeypatch):
        install(backend, monkeypatch, error="Entity has no measurable length: barf")
        r = await backend.inquiry_length("1A2")
        assert not r.ok
        assert "no measurable length" in r.error

    async def test_area_from_entity_sends_measure_area_and_tags_source(self, backend, monkeypatch):
        dispatcher = install(
            backend,
            monkeypatch,
            {"measure-area": {"entity_id": "1A2", "type": "CIRCLE", "area": 78.5398}},
        )
        r = await backend.inquiry_area(entity_id="1A2")
        assert r.ok
        command, params = last(dispatcher)
        assert command == "measure-area"
        assert params == {"entity_id": "1A2"}
        assert r.payload["source"] == "entity"
        assert r.payload["area"] == pytest.approx(78.5398)

    async def test_area_entity_error_has_no_source(self, backend, monkeypatch):
        install(backend, monkeypatch, error="LWPOLYLINE is not closed; enclosed area is undefined")
        r = await backend.inquiry_area(entity_id="1A2")
        assert not r.ok
        assert "not closed" in r.error

    async def test_bbox_sends_entity_and_layer(self, backend, monkeypatch):
        dispatcher = install(
            backend,
            monkeypatch,
            {"bbox": {"entity_id": "1A2", "layer": None, "count": 1,
                      "min": [0.0, 0.0], "max": [10.0, 5.0], "width": 10.0, "height": 5.0}},
        )
        r = await backend.inquiry_bbox(entity_id="1A2", layer=None)
        assert r.ok
        command, params = last(dispatcher)
        assert command == "bbox"
        assert params == {"entity_id": "1A2", "layer": None}
        assert r.payload["width"] == 10.0

    async def test_bbox_general_for_layer(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch, {"bbox": {"count": 3}})
        r = await backend.inquiry_bbox(layer="OUTLINE")
        assert r.ok
        command, params = last(dispatcher)
        assert command == "bbox"
        assert params == {"entity_id": None, "layer": "OUTLINE"}
        assert r.payload["count"] == 3

    async def test_summary(self, backend, monkeypatch):
        dispatcher = install(
            backend,
            monkeypatch,
            {"summary": {"total": 2, "by_type": {"LINE": 2}, "by_layer": {"0": 2}, "layers": ["0"]}},
        )
        r = await backend.inquiry_summary()
        assert r.ok
        command, params = last(dispatcher)
        assert command == "summary"
        assert params == {}
        assert r.payload["total"] == 2


# ---------------------------------------------------------------------------
# Spline / selection / explode
# ---------------------------------------------------------------------------


class TestSplineSelectExplode:
    async def test_create_spline_sends_points_str(self, backend, monkeypatch):
        dispatcher = install(
            backend, monkeypatch, {"create-spline": {"entity_type": "SPLINE", "handle": "2B1", "degree": 3}}
        )
        r = await backend.create_spline([[0, 0], [10, 15], [25, 5], [40, 20]], layer="CURVE", closed=True)
        assert r.ok
        command, params = last(dispatcher)
        assert command == "create-spline"
        assert params["points_str"] == "0,0;10,15;25,5;40,20"
        assert params["layer"] == "CURVE"
        assert params["closed"] == "1"
        assert r.payload["handle"] == "2B1"

    async def test_create_spline_open_encodes_zero(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch, {"create-spline": {"handle": "2B1"}})
        await backend.create_spline([[0, 0], [5, 5], [10, 0]])
        assert sent_params(dispatcher) == {
            "points_str": "0,0;5,5;10,0",
            "closed": "0",
        }  # layer=None is stripped before writing the command file

    async def test_create_spline_requires_three_points(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch)
        r = await backend.create_spline([[0, 0], [10, 10]])
        assert not r.ok
        assert dispatcher.calls == []

    async def test_select_defaults_and_uppercases_type(self, backend, monkeypatch):
        dispatcher = install(
            backend,
            monkeypatch,
            {"entity-select": {"entities": [], "count": 0, "total_matching": 0,
                               "truncated": False, "filters": {}}},
        )
        r = await backend.entity_select({"type": "line"})
        assert r.ok
        command, params = last(dispatcher)
        assert command == "entity-select"
        assert params["type"] == "LINE"
        assert params["layer"] is None
        assert params["window_str"] is None
        assert params["limit"] == 200

    async def test_select_window_is_normalized_to_min_max(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch, {"entity-select": {}})
        await backend.entity_select({"window": [10, 20, 0, 5], "limit": 5})
        _, params = last(dispatcher)
        assert params["window_str"] == "0,5,10,20"
        assert params["limit"] == 5

    async def test_select_rejects_bad_window(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch)
        r = await backend.entity_select({"window": [1, 2, 3]})
        assert not r.ok
        assert r.error_code == "E_PARAMETER_REJECTED"
        assert dispatcher.calls == []

    async def test_select_rejects_limit_out_of_range(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch)
        assert not (await backend.entity_select({"limit": 0})).ok
        assert not (await backend.entity_select({"limit": 1001})).ok
        assert not (await backend.entity_select({"limit": "many"})).ok
        assert dispatcher.calls == []

    async def test_explode_dispatches_handle(self, backend, monkeypatch):
        dispatcher = install(
            backend,
            monkeypatch,
            {"entity-explode": {"exploded": "1A2", "created": 4, "handles": ["2B", "2C", "2D", "2E"]}},
        )
        r = await backend.entity_explode("1A2")
        assert r.ok
        command, params = last(dispatcher)
        assert command == "entity-explode"
        assert params == {"entity_id": "1A2"}
        assert r.payload["created"] == 4
        assert len(r.payload["handles"]) == 4


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------


class TestStyles:
    async def test_textstyle_list(self, backend, monkeypatch):
        dispatcher = install(
            backend,
            monkeypatch,
            {"textstyle-list": {"text_styles": [{"name": "Standard", "font": "txt.shx", "fixed_height": 0.0}]}},
        )
        r = await backend.textstyle_list()
        assert r.ok
        command, _ = last(dispatcher)
        assert command == "textstyle-list"
        assert r.payload["text_styles"][0]["name"] == "Standard"

    async def test_textstyle_create_sends_name_font_height(self, backend, monkeypatch):
        dispatcher = install(
            backend,
            monkeypatch,
            {"textstyle-create": {"name": "NOTES", "font": "arial.ttf", "fixed_height": 2.5, "existed": False}},
        )
        r = await backend.textstyle_create("NOTES", font="arial.ttf", fixed_height=2.5)
        assert r.ok
        command, params = last(dispatcher)
        assert command == "textstyle-create"
        assert params == {"name": "NOTES", "font": "arial.ttf", "fixed_height": 2.5}

    async def test_textstyle_create_rejects_negative_fixed_height(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch)
        r = await backend.textstyle_create("NOTES", fixed_height=-1.0)
        assert not r.ok
        assert dispatcher.calls == []

    async def test_textstyle_set_current(self, backend, monkeypatch):
        dispatcher = install(
            backend, monkeypatch, {"textstyle-set-current": {"current_text_style": "NOTES"}}
        )
        r = await backend.textstyle_set_current("NOTES")
        assert r.ok
        command, params = last(dispatcher)
        assert (command, params) == ("textstyle-set-current", {"name": "NOTES"})

    async def test_dimstyle_list(self, backend, monkeypatch):
        dispatcher = install(
            backend,
            monkeypatch,
            {"dimstyle-list": {"dim_styles": [{"name": "Standard", "dimtxt": 2.5}], "current": "Standard"}},
        )
        r = await backend.dimstyle_list()
        assert r.ok
        command, _ = last(dispatcher)
        assert command == "dimstyle-list"
        assert r.payload["current"] == "Standard"

    async def test_dimstyle_create_whitelists_fields_and_encodes_values(self, backend, monkeypatch):
        dispatcher = install(
            backend,
            monkeypatch,
            {"dimstyle-create": {"name": "GB", "applied": {"dimtxt": 2.5}, "existed": False}},
        )
        r = await backend.dimstyle_create("GB", {"dimtxt": 2.5, "not_a_field": 1, "dimgap": "x"})
        assert r.ok
        command, params = last(dispatcher)
        assert command == "dimstyle-create"
        assert params["name"] == "GB"
        assert params["values_str"] == "dimtxt=2.5"
        assert r.payload["rejected"] == sorted(["not_a_field", "dimgap"])

    async def test_dimstyle_create_none_values_omits_values_str(self, backend, monkeypatch):
        dispatcher = install(
            backend, monkeypatch, {"dimstyle-create": {"name": "GB", "applied": {}, "existed": False}}
        )
        await backend.dimstyle_create("GB", None)
        assert sent_params(dispatcher) == {"name": "GB"}

    async def test_dimstyle_create_merges_remote_rejections(self, backend, monkeypatch):
        install(
            backend,
            monkeypatch,
            {"dimstyle-create": {"name": "GB", "applied": {}, "existed": True, "rejected": ["dimlwd"]}},
        )
        r = await backend.dimstyle_create("GB", {"dimlwd": 3})
        assert r.ok
        assert r.payload["rejected"] == ["dimlwd"]
        assert r.payload["existed"] is True

    async def test_dimstyle_set_current(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch, {"dimstyle-set-current": {"current_dim_style": "GB"}})
        r = await backend.dimstyle_set_current("GB")
        assert r.ok
        command, params = last(dispatcher)
        assert (command, params) == ("dimstyle-set-current", {"name": "GB"})

    async def test_linetype_list(self, backend, monkeypatch):
        dispatcher = install(
            backend,
            monkeypatch,
            {"linetype-list": {"linetypes": [{"name": "CENTER", "description": "Center ____ _ ____"}]}},
        )
        r = await backend.linetype_list()
        assert r.ok
        command, _ = last(dispatcher)
        assert command == "linetype-list"

    async def test_linetype_create_stays_unsupported(self, backend, monkeypatch):
        """Linetype creation stays on the honest base stub by design."""
        dispatcher = install(backend, monkeypatch)
        r = await backend.linetype_create("DASHED", pattern=[2.0, 1.0, -0.5])
        assert not r.ok
        assert "Not supported" in r.error
        assert dispatcher.calls == []

    def test_dimstyle_fields_mirror_ezdxf(self):
        from autocad_mcp.backends.ezdxf_backend import EzdxfBackend

        assert DIMSTYLE_NUMERIC_FIELDS == EzdxfBackend._DIMSTYLE_NUMERIC_FIELDS


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------


class TestLayouts:
    async def test_layout_list(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch, {"layout-list": {"layouts": ["A3"]}})
        r = await backend.layout_list()
        assert r.ok
        command, params = last(dispatcher)
        assert (command, params) == ("layout-list", {})
        assert r.payload["layouts"] == ["A3"]

    async def test_layout_create(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch, {"layout-create": {"name": "A3", "created": True}})
        r = await backend.layout_create("A3")
        assert r.ok
        command, params = last(dispatcher)
        assert (command, params) == ("layout-create", {"name": "A3"})

    async def test_layout_set_current(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch, {"layout-set-current": {"current_layout": "A3"}})
        r = await backend.layout_set_current("A3")
        assert r.ok
        command, params = last(dispatcher)
        assert (command, params) == ("layout-set-current", {"name": "A3"})

    async def test_add_viewport_sends_point_params(self, backend, monkeypatch):
        dispatcher = install(
            backend,
            monkeypatch,
            {"layout-add-viewport": {"layout": "A3", "handle": "4D", "scale": 0.5}},
        )
        r = await backend.layout_add_viewport(
            "A3", [140, 100], 200.0, 140.0, [50, 50], 280.0, layer="VIEWPORTS"
        )
        assert r.ok
        command, params = last(dispatcher)
        assert command == "layout-add-viewport"
        assert params == {
            "layout": "A3",
            "center_x": 140,
            "center_y": 100,
            "width": 200.0,
            "height": 140.0,
            "view_center_x": 50,
            "view_center_y": 50,
            "view_height": 280.0,
            "layer": "VIEWPORTS",
        }
        assert r.payload["handle"] == "4D"

    async def test_add_viewport_rejects_non_positive_sizes(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch)
        for kwargs in (
            {"width": 0, "height": 10, "view_center": [0, 0], "view_height": 10},
            {"width": 10, "height": -1, "view_center": [0, 0], "view_height": 10},
            {"width": 10, "height": 10, "view_center": [0, 0], "view_height": 0},
        ):
            r = await backend.layout_add_viewport("A3", [0, 0], **kwargs)
            assert not r.ok
        assert dispatcher.calls == []


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class TestTables:
    async def test_table_create_sends_encoded_cells(self, backend, monkeypatch):
        dispatcher = install(
            backend,
            monkeypatch,
            {"table-create": {"representation": "native_table", "anchor": "5A", "rows": 2, "cols": 2}},
        )
        r = await backend.table_create(
            0, 0, 2, 2, row_height=5.0, col_width=20.0,
            title="BILL", cells=[["A", "B"], ["C", "D"]], layer="TABLE",
        )
        assert r.ok
        command, params = last(dispatcher)
        assert command == "table-create"
        assert params["x"] == 0
        assert params["rows"] == 2
        assert params["row_height"] == 5.0
        assert params["title"] == "BILL"
        assert params["cells_str"] == "A|B;C|D"
        assert params["layer"] == "TABLE"
        assert r.payload["representation"] == "native_table"
        assert r.payload["anchor"] == "5A"

    async def test_table_create_without_cells_omits_key(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch, {"table-create": {"anchor": "5A"}})
        await backend.table_create(0, 0, 2, 2)
        assert sent_params(dispatcher) == {
            "x": 0,
            "y": 0,
            "rows": 2,
            "cols": 2,
            "row_height": 1.0,
            "col_width": 10.0,
        }  # title/cells_str/layer are all None and get stripped

    async def test_table_create_rejects_bad_geometry(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch)
        assert not (await backend.table_create(0, 0, 0, 2)).ok
        assert not (await backend.table_create(0, 0, 101, 2)).ok
        assert not (await backend.table_create(0, 0, 2, 27)).ok
        assert not (await backend.table_create(0, 0, 2, 2, row_height=0)).ok
        assert not (await backend.table_create(0, 0, 2, 2, col_width=-1)).ok
        assert dispatcher.calls == []

    async def test_table_create_rejects_extra_cell_rows(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch)
        r = await backend.table_create(0, 0, 1, 2, cells=[["A", "B"], ["C", "D"]])
        assert not r.ok
        assert "rows" in r.error
        assert dispatcher.calls == []

    async def test_table_set_cell(self, backend, monkeypatch):
        dispatcher = install(
            backend, monkeypatch, {"table-set-cell": {"anchor": "5A", "row": 1, "col": 2, "text": "X"}}
        )
        r = await backend.table_set_cell("5A", 1, 2, "X")
        assert r.ok
        command, params = last(dispatcher)
        assert (command, params) == ("table-set-cell", {"entity_id": "5A", "row": 1, "col": 2, "text": "X"})

    async def test_table_set_col_widths_encodes_string(self, backend, monkeypatch):
        dispatcher = install(
            backend, monkeypatch, {"table-set-col-widths": {"anchor": "5A", "col_widths": [10.0, 30.0]}}
        )
        r = await backend.table_set_col_widths("5A", [10, 30])
        assert r.ok
        command, params = last(dispatcher)
        assert command == "table-set-col-widths"
        assert params == {"entity_id": "5A", "widths_str": "10.0;30.0"}

    async def test_table_set_row_heights_encodes_string(self, backend, monkeypatch):
        dispatcher = install(
            backend, monkeypatch, {"table-set-row-heights": {"anchor": "5A", "row_heights": [5.0, 8.0]}}
        )
        r = await backend.table_set_row_heights("5A", [5, 8])
        assert r.ok
        command, params = last(dispatcher)
        assert command == "table-set-row-heights"
        assert params == {"entity_id": "5A", "heights_str": "5.0;8.0"}


# ---------------------------------------------------------------------------
# External references
# ---------------------------------------------------------------------------


class TestXrefs:
    async def test_xref_list(self, backend, monkeypatch):
        dispatcher = install(
            backend, monkeypatch, {"xref-list": {"xrefs": [{"name": "brace", "path": "C:/p/brace.dwg"}]}}
        )
        r = await backend.xref_list()
        assert r.ok
        command, params = last(dispatcher)
        assert (command, params) == ("xref-list", {})
        assert r.payload["xrefs"][0]["name"] == "brace"

    async def test_xref_attach_sends_path_and_insert(self, backend, monkeypatch):
        dispatcher = install(
            backend,
            monkeypatch,
            {"xref-attach": {"name": "brace", "path": "C:/p/brace.dwg", "insert": [10.0, 20.0], "via": "command"}},
        )
        r = await backend.xref_attach("C:/p/brace.dwg", 10, 20, name="brace")
        assert r.ok
        command, params = last(dispatcher)
        assert command == "xref-attach"
        assert params == {"path": "C:/p/brace.dwg", "x": 10, "y": 20, "name": "brace"}
        assert r.payload["via"] == "command"

    async def test_xref_attach_defaults_strip_none(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch, {"xref-attach": {}})
        await backend.xref_attach("C:/p/brace.dwg")
        assert sent_params(dispatcher) == {"path": "C:/p/brace.dwg", "x": 0.0, "y": 0.0}

    async def test_xref_detach(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch, {"xref-detach": {"name": "brace", "detached": True}})
        r = await backend.xref_detach("brace")
        assert r.ok
        command, params = last(dispatcher)
        assert (command, params) == ("xref-detach", {"name": "brace"})

    async def test_xref_reload(self, backend, monkeypatch):
        dispatcher = install(backend, monkeypatch, {"xref-reload": {"name": "brace", "reloaded": True}})
        r = await backend.xref_reload("brace")
        assert r.ok
        command, params = last(dispatcher)
        assert (command, params) == ("xref-reload", {"name": "brace"})


# ---------------------------------------------------------------------------
# drawing.save_as_dxf version parameter
# ---------------------------------------------------------------------------


class TestSaveAsDxfVersion:
    async def test_accepts_version_kwarg(self, backend, monkeypatch, tmp_path):
        target = tmp_path / "drawing.dxf"
        monkeypatch.setattr(backend, "_export_dxf_via_com", lambda path: {"path": str(path)})
        r = await backend.drawing_save_as_dxf(str(target), version="R2018")
        assert r.ok
        assert r.payload["requested"]["version"] == "R2018"
        assert r.payload["requested"]["format"] == "dxf"

    async def test_version_is_optional(self, backend, monkeypatch, tmp_path):
        target = tmp_path / "drawing.dxf"
        monkeypatch.setattr(backend, "_export_dxf_via_com", lambda path: {"path": str(path)})
        r = await backend.drawing_save_as_dxf(str(target))
        assert r.ok
        assert r.payload["requested"]["version"] is None


# ---------------------------------------------------------------------------
# LISP whitelist wiring (the file the live AutoCAD loads)
# ---------------------------------------------------------------------------

NEW_COMMANDS = [
    "measure-length",
    "measure-area",
    "bbox",
    "summary",
    "entity-select",
    "create-spline",
    "entity-explode",
    "textstyle-list",
    "textstyle-create",
    "textstyle-set-current",
    "dimstyle-list",
    "dimstyle-create",
    "dimstyle-set-current",
    "linetype-list",
    "layout-list",
    "layout-create",
    "layout-set-current",
    "layout-add-viewport",
    "table-create",
    "table-set-cell",
    "table-set-col-widths",
    "table-set-row-heights",
    "xref-list",
    "xref-attach",
    "xref-detach",
    "xref-reload",
]


class TestLispWhitelistWiring:
    """Every dispatched command must be whitelisted in mcp_dispatch.lsp."""

    def test_dispatch_text_is_loadable(self):
        assert LISP_DISPATCH.is_file(), f"missing dispatcher: {LISP_DISPATCH}"

    def test_every_new_command_is_whitelisted(self):
        text = LISP_DISPATCH.read_text(encoding="utf-8")
        for command in NEW_COMMANDS:
            assert f'(= cmd-name "{command}")' in text, f"not whitelisted: {command}"

    def test_every_whitelisted_new_command_has_a_defun(self):
        text = LISP_DISPATCH.read_text(encoding="utf-8")
        for command in NEW_COMMANDS:
            defun_name = "mcp-cmd-" + command
            assert f"(defun {defun_name} " in text, f"missing implementation: {defun_name}"

    def test_python_new_commands_match_lisp_whitelist(self):
        text = LISP_DISPATCH.read_text(encoding="utf-8")
        whitelisted = set(re.findall(r'\(= cmd-name "([a-z-]+)"\)', text))
        for command in NEW_COMMANDS:
            assert command in whitelisted

    def test_lisp_impls_use_catch_all_wrapping(self):
        """vla-* calls must stay wrapped so failures surface as JSON errors."""
        text = LISP_DISPATCH.read_text(encoding="utf-8")
        for defun in ("mcp-cmd-create-spline", "mcp-cmd-entity-explode", "mcp-cmd-table-create"):
            start = text.index(f"(defun {defun} ")
            body = text[start : text.index("\n\n(defun", start + 1)]
            assert "(vl-catch-all-apply" in body, f"{defun} lacks vl-catch-all-apply"


# ---------------------------------------------------------------------------
# Dispatch-map hygiene for the new commands
# ---------------------------------------------------------------------------


class TestNewCommandHygiene:
    def test_new_commands_use_hyphen_convention(self):
        for command in NEW_COMMANDS:
            assert "_" not in command

    def test_new_commands_are_unique(self):
        assert len(NEW_COMMANDS) == len(set(NEW_COMMANDS))

    def test_backend_method_exists_for_every_new_command(self):
        mapping = {
            "measure-length": "inquiry_length",
            "measure-area": "inquiry_area",
            "bbox": "inquiry_bbox",
            "summary": "inquiry_summary",
            "entity-select": "entity_select",
            "create-spline": "create_spline",
            "entity-explode": "entity_explode",
            "textstyle-list": "textstyle_list",
            "textstyle-create": "textstyle_create",
            "textstyle-set-current": "textstyle_set_current",
            "dimstyle-list": "dimstyle_list",
            "dimstyle-create": "dimstyle_create",
            "dimstyle-set-current": "dimstyle_set_current",
            "linetype-list": "linetype_list",
            "layout-list": "layout_list",
            "layout-create": "layout_create",
            "layout-set-current": "layout_set_current",
            "layout-add-viewport": "layout_add_viewport",
            "table-create": "table_create",
            "table-set-cell": "table_set_cell",
            "table-set-col-widths": "table_set_col_widths",
            "table-set-row-heights": "table_set_row_heights",
            "xref-list": "xref_list",
            "xref-attach": "xref_attach",
            "xref-detach": "xref_detach",
            "xref-reload": "xref_reload",
        }
        assert {command for command, _ in mapping.items()} == set(NEW_COMMANDS)
        for method in mapping.values():
            assert callable(getattr(FileIPCBackend, method)), f"{method} missing"


# ---------------------------------------------------------------------------
# math module guard (distance/angle must not depend on ezdxf)
# ---------------------------------------------------------------------------


def test_distance_matches_math_hypot():
    assert math.hypot(3.0, 4.0) == 5.0
