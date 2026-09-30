"""Headless DXF backend using ezdxf — no AutoCAD needed."""

from __future__ import annotations

import base64
import hashlib
import math
import os
from pathlib import Path
from typing import Any

import ezdxf
import structlog

from autocad_mcp.backends.base import AutoCADBackend, BackendCapabilities, CommandResult
from autocad_mcp.audit import INSUNITS_NAMES, audit_dxf_file, build_audit, normalize_ezdxf_entity
from autocad_mcp.drafting import lineweight_hundredths
from autocad_mcp.errors import LayerNotFoundError
from autocad_mcp.screenshot import MatplotlibScreenshotProvider
from autocad_mcp.variables import validate_variable_updates

log = structlog.get_logger()


def block_record_is_xref(block) -> bool:
    """True when a block layout is an external reference definition."""
    try:
        return bool(block.block_record.is_xref)
    except Exception:
        return False


def block_xref_path(block) -> str:
    """XRef file path stored on the BLOCK entity of a block layout."""
    try:
        return str(block.block.dxf.get("xref_path", "") or "")
    except Exception:
        return ""


class EzdxfBackend(AutoCADBackend):
    """Pure-Python DXF generation via ezdxf."""

    def __init__(self):
        self._doc: ezdxf.document.Drawing | None = None
        self._msp = None  # modelspace
        self._save_path: str | None = None
        self._screenshot = MatplotlibScreenshotProvider()
        self._entity_counter = 0
        self._audit_revision = 0
        self._audit_fingerprints: dict[str, str] | None = None
        self._dimstyle = "EZDXF"
        self._table_manifests: dict[str, dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return "ezdxf"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            can_read_drawing=True,
            can_modify_entities=True,
            can_create_entities=True,
            can_screenshot=True,
            can_save=True,
            can_plot_pdf=False,
            can_zoom=False,  # No viewport in headless
            can_query_entities=True,
            can_file_operations=True,
            can_undo=False,
        )

    async def initialize(self) -> CommandResult:
        self._doc = ezdxf.new("R2013")
        self._msp = self._doc.modelspace()
        self._screenshot.doc = self._doc
        self._audit_revision = 0
        self._audit_fingerprints = None
        self._dimstyle = "EZDXF"
        self._table_manifests = {}
        return CommandResult(ok=True, payload={"backend": "ezdxf", "version": ezdxf.__version__})

    @staticmethod
    def _new_document(version: str | None) -> ezdxf.document.Drawing:
        """Create a new DXF document for a requested version alias."""
        requested = str(version).strip().upper() if version else "R2013"
        return ezdxf.new(requested)

    async def status(self) -> CommandResult:
        entity_count = len(self._msp) if self._msp else 0
        return CommandResult(ok=True, payload={
            "backend": "ezdxf",
            "version": ezdxf.__version__,
            "has_document": self._doc is not None,
            "entity_count": entity_count,
            "save_path": self._save_path,
            "capabilities": {k: v for k, v in self.capabilities.__dict__.items()},
        })

    def _next_id(self) -> str:
        self._entity_counter += 1
        return f"ezdxf_{self._entity_counter}"

    def _ensure_layer(self, layer: str | None):
        if layer and layer not in self._doc.layers:
            raise LayerNotFoundError(f"Layer does not exist: {layer}")

    # --- Drawing management ---

    async def drawing_info(self) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open")
        layers = [l.dxf.name for l in self._doc.layers]
        entity_count = len(self._msp)
        blocks = [b.name for b in self._doc.blocks if not b.name.startswith("*")]
        return CommandResult(ok=True, payload={
            "entity_count": entity_count,
            "layers": layers,
            "blocks": blocks,
            "dxf_version": self._doc.dxfversion,
            "save_path": self._save_path,
        })

    async def drawing_save(self, path: str | None = None) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open")
        save_path = path or self._save_path
        if not save_path:
            return CommandResult(ok=False, error="No save path specified")
        self._doc.saveas(save_path)
        self._save_path = save_path
        return CommandResult(ok=True, payload={"path": save_path})

    async def drawing_save_as_dxf(self, path, version=None) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open")
        requested_version = str(version).strip().upper() if version else None
        current_version = self._doc.dxfversion
        try:
            if requested_version and requested_version != current_version:
                # Version conversion goes through the Importer add-on, which
                # also copies the required table resources (layers, styles...).
                from ezdxf.addons import Importer

                target = self._new_document(requested_version)
                importer = Importer(self._doc, target)
                importer.import_modelspace()
                importer.finalize()
                target.saveas(path)
                converted = True
                written_version = target.dxfversion
            else:
                self._doc.saveas(path)
                converted = False
                written_version = current_version
        except ezdxf.DXFVersionError as exc:
            return CommandResult(
                ok=False,
                error=f"Unsupported DXF version: {exc}",
                error_code="E_PARAMETER_REJECTED",
            )
        except (ezdxf.DXFError, ValueError, TypeError) as exc:
            return CommandResult(
                ok=False,
                error=f"DXF version conversion failed: {exc}",
                error_code="E_VERSION_CONVERSION_FAILED",
            )
        self._save_path = path
        return CommandResult(
            ok=True,
            payload={
                "path": path,
                "dxf_version": written_version,
                "requested_version": requested_version,
                "converted": converted,
            },
        )

    async def recover(self) -> CommandResult:
        return CommandResult(ok=True, payload={"backend": "ezdxf", "recovered": True})

    async def drawing_audit(
        self,
        limit=50,
        include_entities=True,
        changed_only=False,
        layer=None,
        space="model",
        rules=None,
    ) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open")
        if space.lower() != "model":
            return CommandResult(ok=False, error="ezdxf audit currently supports ModelSpace only")
        try:
            entities = [
                normalize_ezdxf_entity(entity)
                for entity in self._msp
                if not layer or entity.dxf.get("layer", "0") == layer
            ]
            semantics = self._semantic_store()
            entities = [
                {**entity, **({"semantics": semantics[str(entity.get("handle"))]} if str(entity.get("handle")) in semantics else {})}
                for entity in entities
            ]
            self._audit_revision += 1
            payload, fingerprints = build_audit(
                entities,
                limit=limit,
                include_entities=include_entities,
                changed_only=changed_only,
                previous_fingerprints=self._audit_fingerprints,
                revision=self._audit_revision,
                space="model",
                geometry_rules=rules,
            )
            self._audit_fingerprints = fingerprints
            units_code = int(self._doc.header.get("$INSUNITS", 0) or 0)
            payload["units"] = {
                "code": units_code,
                "name": INSUNITS_NAMES.get(units_code, "unknown"),
            }
            return CommandResult(ok=True, payload=payload)
        except Exception as exc:
            return CommandResult(ok=False, error=str(exc))

    async def drawing_audit_dxf(self, path, limit=50, include_entities=True) -> CommandResult:
        try:
            return CommandResult(
                ok=True,
                payload=audit_dxf_file(path, limit=limit, include_entities=include_entities),
            )
        except Exception as exc:
            return CommandResult(ok=False, error=str(exc))

    async def drawing_render_preview(
        self,
        path,
        paper="A4",
        orientation="auto",
        plot_style="monochrome.ctb",
        dpi=150,
        force=True,
        background="white",
        plot_type="extents",
        normalize_framing=False,
        framing_fill=0.82,
        visual_style=None,
        preserve_visual_style=True,
    ) -> CommandResult:
        if visual_style is not None:
            return CommandResult(
                ok=False,
                error="The ezdxf backend cannot verify or apply an AutoCAD visual style",
                error_code="E_VISUAL_STYLE_UNSUPPORTED",
                recoverable=False,
                recommended_action="use_file_ipc_or_native_render_backend_for_visual_style_evidence",
                payload={
                    "requested_visual_style": visual_style,
                    "material_render_verified": False,
                    "preserve_visual_style": bool(preserve_visual_style),
                },
            )
        output = Path(path).expanduser().resolve()
        if output.suffix.lower() != ".png":
            return CommandResult(ok=False, error="ezdxf preview output must use a .png extension")
        if int(dpi) < 72 or int(dpi) > 600:
            return CommandResult(ok=False, error="Preview DPI must be between 72 and 600")
        if output.exists() and not force:
            return CommandResult(ok=False, error=f"Preview already exists: {output}", error_code="E_OUTPUT_EXISTS")
        data = self._screenshot.render(dpi=int(dpi), background=str(background))
        if not data:
            return CommandResult(ok=False, error="Headless preview render failed")
        output.parent.mkdir(parents=True, exist_ok=True)
        if force:
            output.unlink(missing_ok=True)
        output.write_bytes(base64.b64decode(data))
        from PIL import Image

        with Image.open(output) as image:
            width, height = image.size
        return CommandResult(
            ok=True,
            payload={
                "path": str(output),
                "format": "png",
                "renderer": "ezdxf-matplotlib",
                "dpi": int(dpi),
                "background": str(background),
                "width": width,
                "height": height,
                "bytes": output.stat().st_size,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "force_overwrite": bool(force),
                "visual_style": None,
                "material_render": False,
                "material_render_verified": False,
                "render_truth": "matplotlib_linework_unverified_as_material_render",
            },
        )

    async def drawing_create(
        self, name: str | None = None, idempotency_key: str | None = None, version: str | None = None
    ) -> CommandResult:
        try:
            self._doc = self._new_document(version)
        except (ezdxf.DXFVersionError, ValueError) as exc:
            return CommandResult(
                ok=False,
                error=f"Unsupported DXF version: {exc}",
                error_code="E_PARAMETER_REJECTED",
            )
        self._msp = self._doc.modelspace()
        self._screenshot.doc = self._doc
        self._entity_counter = 0
        self._dimstyle = "EZDXF"
        self._table_manifests = {}
        # ``server.drawing(create)`` resolves a managed output target before
        # calling the backend, so ``name`` may already contain an extension
        # (and an absolute path).  Appending ``.dxf`` unconditionally used to
        # produce names such as ``part.dxf.dxf`` and made the returned
        # document identity disagree with the requested path.  Normalize once
        # at the backend boundary while preserving the historical stem-only
        # behaviour used by direct callers.
        if name:
            requested_path = Path(str(name)).expanduser()
            if requested_path.suffix.lower() != ".dxf":
                requested_path = requested_path.with_suffix(".dxf")
            self._save_path = str(requested_path)
        else:
            self._save_path = None
        self._audit_revision = 0
        self._audit_fingerprints = None
        self._semantic_store().clear()
        if hasattr(self, "_document_state"):
            delattr(self, "_document_state")
        context = (await self.document_context()).payload
        return CommandResult(
            ok=True,
            payload={
                "name": name or "untitled",
                "requested_name": name,
                "actual_name": self._save_path or "untitled.dxf",
                "name_honored": bool(name),
                **context,
            },
        )

    async def drawing_purge(self) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open")
        # ezdxf doesn't have a direct purge; just report
        return CommandResult(ok=True, payload={"purged": True})

    async def drawing_open(self, path: str) -> CommandResult:
        try:
            self._doc = ezdxf.readfile(path)
            self._msp = self._doc.modelspace()
            self._screenshot.doc = self._doc
            self._save_path = path
            self._audit_revision = 0
            self._audit_fingerprints = None
            self._semantic_store().clear()
            self._dimstyle = "EZDXF"
            self._table_manifests = {}
            if hasattr(self, "_document_state"):
                delattr(self, "_document_state")
            context = (await self.document_context()).payload
            return CommandResult(ok=True, payload={"path": path, **context})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def drawing_get_variables(self, names: list[str] | None = None) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open")
        result = {}
        header = self._doc.header
        for name in (names or []):
            result_name = str(name)
            header_name = f"${result_name.lstrip('$').upper()}"
            try:
                result[result_name] = header[header_name]
            except (KeyError, ezdxf.DXFKeyError):
                result[result_name] = None
        return CommandResult(ok=True, payload=result)

    async def drawing_set_variables(self, values: dict) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open")
        try:
            updates = validate_variable_updates(values)
        except ValueError as exc:
            return CommandResult(ok=False, error=str(exc), error_code="E_VARIABLE_REJECTED")
        previous = {}
        unsupported = []
        applied = {}
        for name, value in updates.items():
            header_name = f"${name}"
            try:
                previous[name] = self._doc.header.get(header_name)
                self._doc.header[header_name] = value
                applied[name] = value
            except ezdxf.DXFKeyError:
                unsupported.append(name)
        current = {name: self._doc.header.get(f"${name}") for name in applied}
        return CommandResult(
            ok=True,
            payload={
                "updated": current,
                "previous": previous,
                "unsupported": unsupported,
                "verified": all(current[name] == value for name, value in applied.items()),
            },
        )

    # --- Entity operations ---

    async def create_line(self, x1, y1, x2, y2, layer=None) -> CommandResult:
        self._ensure_layer(layer)
        e = self._msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": layer or "0"})
        return CommandResult(ok=True, payload={"entity_type": "LINE", "handle": e.dxf.handle})

    async def create_circle(self, cx, cy, radius, layer=None) -> CommandResult:
        self._ensure_layer(layer)
        e = self._msp.add_circle((cx, cy), radius, dxfattribs={"layer": layer or "0"})
        return CommandResult(ok=True, payload={"entity_type": "CIRCLE", "handle": e.dxf.handle})

    async def create_polyline(self, points, closed=False, layer=None) -> CommandResult:
        self._ensure_layer(layer)
        pts = [(p[0], p[1]) for p in points]
        e = self._msp.add_lwpolyline(pts, close=closed, dxfattribs={"layer": layer or "0"})
        return CommandResult(ok=True, payload={"entity_type": "LWPOLYLINE", "handle": e.dxf.handle})

    async def create_rectangle(self, x1, y1, x2, y2, layer=None) -> CommandResult:
        pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        return await self.create_polyline(pts, closed=True, layer=layer)

    async def create_arc(self, cx, cy, radius, start_angle, end_angle, layer=None) -> CommandResult:
        self._ensure_layer(layer)
        e = self._msp.add_arc((cx, cy), radius, start_angle, end_angle, dxfattribs={"layer": layer or "0"})
        return CommandResult(ok=True, payload={"entity_type": "ARC", "handle": e.dxf.handle})

    async def create_ellipse(self, cx, cy, major_x, major_y, ratio, layer=None) -> CommandResult:
        self._ensure_layer(layer)
        e = self._msp.add_ellipse(
            (cx, cy), major_axis=(major_x - cx, major_y - cy, 0), ratio=ratio,
            dxfattribs={"layer": layer or "0"},
        )
        return CommandResult(ok=True, payload={"entity_type": "ELLIPSE", "handle": e.dxf.handle})

    async def create_mtext(self, x, y, width, text, height=2.5, layer=None) -> CommandResult:
        self._ensure_layer(layer)
        e = self._msp.add_mtext(text, dxfattribs={
            "insert": (x, y),
            "char_height": height,
            "width": width,
            "layer": layer or "0",
        })
        return CommandResult(ok=True, payload={"entity_type": "MTEXT", "handle": e.dxf.handle})

    async def entity_list(self, layer=None) -> CommandResult:
        entities = []
        for e in self._msp:
            if layer and e.dxf.get("layer", "0") != layer:
                continue
            entities.append({
                "type": e.dxftype(),
                "handle": e.dxf.handle,
                "layer": e.dxf.get("layer", "0"),
            })
        return CommandResult(ok=True, payload={"entities": entities, "count": len(entities)})

    async def entity_count(self, layer=None) -> CommandResult:
        if layer:
            count = sum(1 for e in self._msp if e.dxf.get("layer", "0") == layer)
        else:
            count = len(self._msp)
        return CommandResult(ok=True, payload={"count": count})

    async def entity_get(self, entity_id) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found")
            info = {"type": e.dxftype(), "handle": e.dxf.handle, "layer": e.dxf.get("layer", "0")}
            # Add type-specific info
            if e.dxftype() == "LINE":
                info["start"] = list(e.dxf.start)[:2]
                info["end"] = list(e.dxf.end)[:2]
            elif e.dxftype() == "CIRCLE":
                info["center"] = list(e.dxf.center)[:2]
                info["radius"] = e.dxf.radius
            elif e.dxftype() == "ARC":
                info.update(
                    center=list(e.dxf.center)[:2],
                    radius=e.dxf.radius,
                    start_angle=e.dxf.start_angle,
                    end_angle=e.dxf.end_angle,
                )
            elif e.dxftype() == "ELLIPSE":
                info.update(
                    center=list(e.dxf.center)[:2],
                    major_axis=list(e.dxf.major_axis)[:2],
                    ratio=e.dxf.ratio,
                )
            elif e.dxftype() == "LWPOLYLINE":
                info["points"] = [[float(point[0]), float(point[1])] for point in e.get_points()]
                info["closed"] = bool(e.closed)
            elif e.dxftype() == "SPLINE":
                info["points"] = [
                    [float(point[0]), float(point[1])] for point in e.fit_points
                ]
                info["degree"] = int(e.dxf.degree)
            elif e.dxftype() == "MTEXT":
                info.update(
                    insert=list(e.dxf.insert)[:2],
                    text=e.text,
                    height=e.dxf.char_height,
                    width=e.dxf.width,
                )
            elif e.dxftype() == "TEXT":
                info.update(
                    insert=list(e.dxf.insert)[:2],
                    text=e.dxf.text,
                    height=e.dxf.height,
                    rotation=e.dxf.rotation,
                )
            elif e.dxftype() == "HATCH":
                info.update(
                    pattern=e.dxf.get("pattern_name", ""),
                    angle=e.dxf.get("pattern_angle", 0.0),
                    scale=e.dxf.get("pattern_scale", 1.0),
                )
            elif e.dxftype() == "VIEWPORT":
                info.update(
                    center=list(e.dxf.get("center", (0, 0)))[:2],
                    width=float(e.dxf.get("width", 0)),
                    height=float(e.dxf.get("height", 0)),
                    view_center_point=list(e.dxf.get("view_center_point", (0, 0)))[:2],
                    view_height=float(e.dxf.get("view_height", 0)),
                )
            normalized = normalize_ezdxf_entity(e)
            for field in ("bounds", "length", "area", "volume"):
                if normalized.get(field) is not None:
                    info[field] = normalized[field]
            semantics = self._semantic_store().get(str(entity_id))
            if semantics:
                info["semantics"] = dict(semantics)
            return CommandResult(ok=True, payload=info)
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def entity_erase(self, entity_id) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                # Try "last" keyword
                if entity_id == "last" and len(self._msp) > 0:
                    entities = list(self._msp)
                    e = entities[-1]
                else:
                    return CommandResult(ok=False, error=f"Entity {entity_id} not found")
            self._msp.delete_entity(e)
            self._semantic_store().pop(str(entity_id), None)
            return CommandResult(ok=True, payload={"erased": entity_id})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def entity_copy(self, entity_id, dx, dy) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found")
            copy = e.copy()
            self._msp.add_entity(copy)
            copy.translate(dx, dy, 0)
            return CommandResult(ok=True, payload={"handle": copy.dxf.handle})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def entity_move(self, entity_id, dx, dy) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found")
            e.translate(dx, dy, 0)
            return CommandResult(ok=True, payload={"moved": entity_id})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def entity_rotate(self, entity_id, cx, cy, angle) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found")
            from ezdxf.math import Matrix44
            m = Matrix44.z_rotate(math.radians(angle))
            # Translate to origin, rotate, translate back
            e.translate(-cx, -cy, 0)
            e.transform(m)
            e.translate(cx, cy, 0)
            return CommandResult(ok=True, payload={"rotated": entity_id})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def entity_scale(self, entity_id, cx, cy, factor) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found")
            from ezdxf.math import Matrix44
            m = Matrix44.scale(factor, factor, factor)
            e.translate(-cx, -cy, 0)
            e.transform(m)
            e.translate(cx, cy, 0)
            return CommandResult(ok=True, payload={"scaled": entity_id})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def entity_mirror(self, entity_id, x1, y1, x2, y2) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found")
            copy = e.copy()
            self._msp.add_entity(copy)
            # Mirror across line (x1,y1)-(x2,y2) using reflection matrix
            dx, dy = x2 - x1, y2 - y1
            length_sq = dx * dx + dy * dy
            if length_sq == 0:
                return CommandResult(ok=False, error="Mirror line has zero length")
            from ezdxf.math import Matrix44
            # Reflect: translate to origin, reflect, translate back
            # Reflection matrix across line through origin with direction (dx, dy):
            #   [[cos2a, sin2a], [sin2a, -cos2a]] where a = atan2(dy, dx)
            a = math.atan2(dy, dx)
            cos2a = math.cos(2 * a)
            sin2a = math.sin(2 * a)
            m = Matrix44([
                cos2a, sin2a, 0, 0,
                sin2a, -cos2a, 0, 0,
                0, 0, 1, 0,
                0, 0, 0, 1,
            ])
            copy.translate(-x1, -y1, 0)
            copy.transform(m)
            copy.translate(x1, y1, 0)
            return CommandResult(ok=True, payload={"handle": copy.dxf.handle})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def entity_offset(self, entity_id, distance) -> CommandResult:
        # ezdxf doesn't have a native offset command; approximate for simple cases
        return CommandResult(ok=False, error="Offset not supported on ezdxf backend")

    async def entity_array(self, entity_id, rows, cols, row_dist, col_dist) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found")
            handles = []
            for r in range(rows):
                for c in range(cols):
                    if r == 0 and c == 0:
                        continue  # Skip original position
                    copy = e.copy()
                    self._msp.add_entity(copy)
                    copy.translate(c * col_dist, r * row_dist, 0)
                    handles.append(copy.dxf.handle)
            return CommandResult(ok=True, payload={"copies": len(handles), "handles": handles})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def entity_fillet(self, entity_id1, entity_id2, radius) -> CommandResult:
        return CommandResult(ok=False, error="Fillet not supported on ezdxf backend")

    async def entity_chamfer(self, entity_id1, entity_id2, dist1, dist2) -> CommandResult:
        return CommandResult(ok=False, error="Chamfer not supported on ezdxf backend")

    async def create_hatch(
        self, entity_id, pattern="ANSI31", angle=0.0, scale=1.0, layer=None
    ) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found")
            self._ensure_layer(layer)
            hatch = self._msp.add_hatch(dxfattribs={"layer": layer or "0"})
            hatch.set_pattern_fill(pattern, scale=scale, angle=angle)
            # Try to use the entity as a boundary path
            hatch.paths.add_polyline_path(
                [(p[0], p[1]) for p in e.get_points(format="xy")],
                is_closed=True,
            )
            return CommandResult(
                ok=True,
                payload={
                    "entity_type": "HATCH",
                    "handle": hatch.dxf.handle,
                    "pattern": pattern,
                    "angle": angle,
                    "scale": scale,
                },
            )
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    # --- Layer operations ---

    async def layer_list(self) -> CommandResult:
        layers = []
        for l in self._doc.layers:
            layers.append({
                "name": l.dxf.name,
                "color": l.dxf.get("color", 7),
                "linetype": l.dxf.get("linetype", "Continuous"),
                "is_frozen": l.is_frozen(),
                "is_locked": l.is_locked(),
            })
        return CommandResult(ok=True, payload={"layers": layers})

    async def layer_exists(self, name: str) -> CommandResult:
        return CommandResult(
            ok=True,
            payload={"name": str(name), "exists": str(name) in self._doc.layers},
        )

    def _ensure_linetype(self, name: str) -> str:
        normalized = name.upper()
        if normalized in self._doc.linetypes:
            return normalized
        definitions = {
            "CENTER": (
                "Center ____ _ ____ _ ____ _ ____",
                [2.0, 1.25, -0.25, 0.25, -0.25],
            ),
            "HIDDEN": ("Hidden __ __ __ __ __ __ __", [0.75, 0.5, -0.25]),
        }
        definition = definitions.get(normalized)
        if definition is None:
            return "CONTINUOUS"
        description, pattern = definition
        self._doc.linetypes.add(normalized, pattern=pattern, description=description)
        return normalized

    async def layer_create(
        self, name, color="white", linetype="CONTINUOUS", lineweight=None
    ) -> CommandResult:
        color_int = self._color_to_int(color)
        actual_linetype = self._ensure_linetype(linetype)
        existed = name in self._doc.layers
        if existed:
            layer = self._doc.layers.get(name)
            layer.color = color_int
            layer.dxf.linetype = actual_linetype
        else:
            layer = self._doc.layers.add(name, color=color_int, linetype=actual_linetype)
        if lineweight is not None:
            layer.dxf.lineweight = lineweight_hundredths(lineweight)
        payload = {
            "name": name,
            "color": color_int,
            "linetype": actual_linetype,
            "lineweight": layer.dxf.get("lineweight", -3),
            "existed": existed,
        }
        if actual_linetype != linetype.upper():
            payload["warning"] = f"Linetype {linetype} unavailable; used CONTINUOUS"
        return CommandResult(ok=True, payload=payload)

    async def layer_set_current(self, name) -> CommandResult:
        if name not in self._doc.layers:
            return CommandResult(ok=False, error=f"Layer '{name}' does not exist")
        self._doc.header["$CLAYER"] = name
        return CommandResult(ok=True, payload={"current_layer": name})

    async def layer_set_properties(self, name, color=None, linetype=None, lineweight=None) -> CommandResult:
        if name not in self._doc.layers:
            return CommandResult(ok=False, error=f"Layer '{name}' does not exist")
        layer = self._doc.layers.get(name)
        if color is not None:
            layer.color = self._color_to_int(color)
        if linetype is not None:
            layer.dxf.linetype = self._ensure_linetype(linetype)
        if lineweight is not None:
            layer.dxf.lineweight = lineweight_hundredths(lineweight)
        return CommandResult(ok=True, payload={"name": name})

    async def layer_freeze(self, name) -> CommandResult:
        if name not in self._doc.layers:
            return CommandResult(ok=False, error=f"Layer '{name}' does not exist")
        self._doc.layers.get(name).freeze()
        return CommandResult(ok=True, payload={"name": name, "frozen": True})

    async def layer_thaw(self, name) -> CommandResult:
        if name not in self._doc.layers:
            return CommandResult(ok=False, error=f"Layer '{name}' does not exist")
        self._doc.layers.get(name).thaw()
        return CommandResult(ok=True, payload={"name": name, "frozen": False})

    async def layer_lock(self, name) -> CommandResult:
        if name not in self._doc.layers:
            return CommandResult(ok=False, error=f"Layer '{name}' does not exist")
        self._doc.layers.get(name).lock()
        return CommandResult(ok=True, payload={"name": name, "locked": True})

    async def layer_unlock(self, name) -> CommandResult:
        if name not in self._doc.layers:
            return CommandResult(ok=False, error=f"Layer '{name}' does not exist")
        self._doc.layers.get(name).unlock()
        return CommandResult(ok=True, payload={"name": name, "locked": False})

    # --- Block operations ---

    async def block_list(self) -> CommandResult:
        blocks = [b.name for b in self._doc.blocks if not b.name.startswith("*")]
        return CommandResult(ok=True, payload={"blocks": blocks})

    async def block_insert(self, name, x, y, scale=1.0, rotation=0.0, block_id=None) -> CommandResult:
        if name not in self._doc.blocks:
            return CommandResult(ok=False, error=f"Block '{name}' not defined")
        e = self._msp.add_blockref(name, (x, y), dxfattribs={
            "xscale": scale, "yscale": scale, "zscale": scale,
            "rotation": rotation,
        })
        if block_id:
            try:
                e.add_attrib("ID", block_id)
            except Exception:
                pass
        return CommandResult(ok=True, payload={"entity_type": "INSERT", "handle": e.dxf.handle})

    async def block_insert_with_attributes(self, name, x, y, scale=1.0, rotation=0.0, attributes=None) -> CommandResult:
        if name not in self._doc.blocks:
            return CommandResult(ok=False, error=f"Block '{name}' not defined")
        block = self._doc.blocks[name]
        e = self._msp.add_blockref(name, (x, y), dxfattribs={
            "xscale": scale, "yscale": scale, "zscale": scale,
            "rotation": rotation,
        })
        if attributes:
            # Try add_auto_attribs first (uses ATTDEF templates)
            try:
                e.add_auto_attribs(attributes)
            except Exception:
                # Fallback: add manual attribs
                for tag, value in attributes.items():
                    try:
                        e.add_attrib(tag, value, (x, y))
                    except Exception:
                        pass
        return CommandResult(ok=True, payload={"entity_type": "INSERT", "handle": e.dxf.handle})

    async def block_get_attributes(self, entity_id) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None or e.dxftype() != "INSERT":
                return CommandResult(ok=False, error="Not an INSERT entity")
            attribs = {}
            for attrib in e.attribs:
                attribs[attrib.dxf.tag] = attrib.dxf.text
            return CommandResult(ok=True, payload={"attributes": attribs})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def block_update_attribute(self, entity_id, tag, value) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None or e.dxftype() != "INSERT":
                return CommandResult(ok=False, error="Not an INSERT entity")
            for attrib in e.attribs:
                if attrib.dxf.tag.upper() == tag.upper():
                    attrib.dxf.text = value
                    return CommandResult(ok=True, payload={"tag": tag, "value": value})
            return CommandResult(ok=False, error=f"Attribute '{tag}' not found")
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def block_define(self, name, entities) -> CommandResult:
        block = self._doc.blocks.new(name=name)
        for ent_def in entities:
            etype = ent_def.get("type", "LINE")
            if etype == "LINE":
                block.add_line(
                    (ent_def.get("x1", 0), ent_def.get("y1", 0)),
                    (ent_def.get("x2", 0), ent_def.get("y2", 0)),
                )
            elif etype == "CIRCLE":
                block.add_circle(
                    (ent_def.get("cx", 0), ent_def.get("cy", 0)),
                    ent_def.get("radius", 1),
                )
            elif etype == "ATTDEF":
                block.add_attdef(
                    ent_def.get("tag", "TAG"),
                    (ent_def.get("x", 0), ent_def.get("y", 0)),
                    dxfattribs={"height": ent_def.get("height", 2.5)},
                )
        return CommandResult(ok=True, payload={"block": name, "entity_count": len(entities)})

    # --- Annotation ---

    async def create_text(self, x, y, text, height=2.5, rotation=0.0, layer=None) -> CommandResult:
        self._ensure_layer(layer)
        e = self._msp.add_text(text, dxfattribs={
            "insert": (x, y),
            "height": height,
            "rotation": rotation,
            "layer": layer or "0",
        })
        return CommandResult(ok=True, payload={"entity_type": "TEXT", "handle": e.dxf.handle})

    async def create_dimension_linear(self, x1, y1, x2, y2, dim_x, dim_y) -> CommandResult:
        try:
            dim = self._msp.add_linear_dim(
                base=(dim_x, dim_y),
                p1=(x1, y1),
                p2=(x2, y2),
                dimstyle=self._dimstyle,
            )
            dim.render()
            return CommandResult(ok=True, payload={"entity_type": "DIMENSION"})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def create_dimension_aligned(self, x1, y1, x2, y2, offset) -> CommandResult:
        try:
            dim = self._msp.add_aligned_dim(
                p1=(x1, y1),
                p2=(x2, y2),
                distance=offset,
                dimstyle=self._dimstyle,
            )
            dim.render()
            return CommandResult(ok=True, payload={"entity_type": "DIMENSION"})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def create_dimension_angular(self, cx, cy, x1, y1, x2, y2) -> CommandResult:
        try:
            # Calculate angle arc midpoint for dimension location
            a1 = math.atan2(y1 - cy, x1 - cx)
            a2 = math.atan2(y2 - cy, x2 - cx)
            amid = (a1 + a2) / 2
            r = max(math.hypot(x1 - cx, y1 - cy), math.hypot(x2 - cx, y2 - cy)) * 0.7
            dim = self._msp.add_angular_dim_cra(
                center=(cx, cy),
                radius=r,
                start_angle=math.degrees(a1),
                end_angle=math.degrees(a2),
                distance=r * 1.2,
            )
            dim.render()
            return CommandResult(ok=True, payload={"entity_type": "DIMENSION"})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def create_dimension_radius(self, cx, cy, radius, angle) -> CommandResult:
        try:
            rad = math.radians(angle)
            px = cx + radius * math.cos(rad)
            py = cy + radius * math.sin(rad)
            dim = self._msp.add_radius_dim(
                center=(cx, cy),
                mpoint=(px, py),
                dimstyle=self._dimstyle,
            )
            dim.render()
            return CommandResult(ok=True, payload={"entity_type": "DIMENSION"})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def create_leader(self, points, text) -> CommandResult:
        try:
            pts = [(p[0], p[1]) for p in points]
            leader = self._msp.add_leader(pts)
            # Add text at the last point
            last = pts[-1]
            self._msp.add_mtext(text, dxfattribs={
                "insert": (last[0] + 2, last[1]),
                "char_height": 2.5,
                "width": 30,
            })
            return CommandResult(ok=True, payload={"entity_type": "LEADER"})
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    # --- P&ID ---

    async def pid_setup_layers(self) -> CommandResult:
        pid_layers = [
            ("PID-EQUIPMENT", 6, "CONTINUOUS"),
            ("PID-PROCESS-PIPING", 4, "CONTINUOUS"),
            ("PID-UTILITY-PIPING", 3, "CONTINUOUS"),
            ("PID-INSTRUMENTS", 5, "CONTINUOUS"),
            ("PID-ELECTRICAL", 1, "CONTINUOUS"),
            ("PID-ANNOTATION", 7, "CONTINUOUS"),
            ("PID-VALVES", 2, "CONTINUOUS"),
        ]
        for name, color, lt in pid_layers:
            if name not in self._doc.layers:
                self._doc.layers.add(name, color=color, linetype=lt)
        return CommandResult(ok=True, payload={"layers_created": len(pid_layers)})

    async def pid_list_symbols(self, category) -> CommandResult:
        """List CTO symbols from disk or built-in catalog."""
        from autocad_mcp.pid.cto_library import CTO_ROOT, list_symbols
        symbols = list_symbols(category)
        return CommandResult(ok=True, payload={"category": category, "symbols": symbols, "count": len(symbols)})

    async def pid_insert_symbol(self, category, symbol, x, y, scale=1.0, rotation=0.0) -> CommandResult:
        """Insert a CTO symbol as a simple block placeholder."""
        self._ensure_layer("PID-EQUIPMENT")
        # In headless mode, create a placeholder rectangle with the symbol name
        half = 5 * scale
        pts = [(x - half, y - half), (x + half, y - half), (x + half, y + half), (x - half, y + half)]
        e = self._msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "PID-EQUIPMENT"})
        self._msp.add_text(symbol, dxfattribs={
            "insert": (x, y), "height": 1.5 * scale, "layer": "PID-ANNOTATION",
        })
        return CommandResult(ok=True, payload={"symbol": symbol, "handle": e.dxf.handle})

    async def pid_insert_valve(self, x, y, valve_type, rotation=0.0, attributes=None) -> CommandResult:
        """Insert a valve symbol (simplified for headless)."""
        self._ensure_layer("PID-VALVES")
        # Simplified diamond shape for valve
        size = 3.0
        pts = [(x - size, y), (x, y + size), (x + size, y), (x, y - size)]
        e = self._msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "PID-VALVES"})
        self._msp.add_text(valve_type, dxfattribs={
            "insert": (x, y - size - 2), "height": 1.5, "layer": "PID-ANNOTATION",
        })
        return CommandResult(ok=True, payload={"valve_type": valve_type, "handle": e.dxf.handle})

    async def pid_insert_instrument(self, x, y, instrument_type, rotation=0.0, tag_id="", range_value="") -> CommandResult:
        """Insert an instrument symbol (simplified for headless)."""
        self._ensure_layer("PID-INSTRUMENTS")
        # Circle with crosshair for instrument
        e = self._msp.add_circle((x, y), 4, dxfattribs={"layer": "PID-INSTRUMENTS"})
        self._msp.add_line((x - 4, y), (x + 4, y), dxfattribs={"layer": "PID-INSTRUMENTS"})
        label = tag_id if tag_id else instrument_type
        self._msp.add_text(label, dxfattribs={
            "insert": (x, y - 6), "height": 1.5, "layer": "PID-ANNOTATION",
        })
        return CommandResult(ok=True, payload={"instrument_type": instrument_type, "handle": e.dxf.handle})

    async def pid_insert_pump(self, x, y, pump_type, rotation=0.0, attributes=None) -> CommandResult:
        """Insert a pump symbol (simplified for headless)."""
        self._ensure_layer("PID-EQUIPMENT")
        # Circle with triangle for pump
        e = self._msp.add_circle((x, y), 6, dxfattribs={"layer": "PID-EQUIPMENT"})
        rad = math.radians(rotation)
        tip_x = x + 8 * math.cos(rad)
        tip_y = y + 8 * math.sin(rad)
        self._msp.add_lwpolyline(
            [(x + 6 * math.cos(rad + 0.5), y + 6 * math.sin(rad + 0.5)),
             (tip_x, tip_y),
             (x + 6 * math.cos(rad - 0.5), y + 6 * math.sin(rad - 0.5))],
            close=True,
            dxfattribs={"layer": "PID-EQUIPMENT"},
        )
        self._msp.add_text(pump_type, dxfattribs={
            "insert": (x, y - 8), "height": 1.5, "layer": "PID-ANNOTATION",
        })
        return CommandResult(ok=True, payload={"pump_type": pump_type, "handle": e.dxf.handle})

    async def pid_insert_tank(self, x, y, tank_type, scale=1.0, attributes=None) -> CommandResult:
        """Insert a tank symbol (simplified for headless)."""
        self._ensure_layer("PID-EQUIPMENT")
        w = 10 * scale
        h = 15 * scale
        pts = [(x - w, y), (x + w, y), (x + w, y + h), (x - w, y + h)]
        e = self._msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "PID-EQUIPMENT"})
        self._msp.add_text(tank_type, dxfattribs={
            "insert": (x, y + h + 2), "height": 2.0 * scale, "layer": "PID-ANNOTATION",
        })
        return CommandResult(ok=True, payload={"tank_type": tank_type, "handle": e.dxf.handle})

    async def pid_draw_process_line(self, x1, y1, x2, y2) -> CommandResult:
        self._ensure_layer("PID-PROCESS-PIPING")
        e = self._msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": "PID-PROCESS-PIPING"})
        return CommandResult(ok=True, payload={"entity_type": "LINE", "handle": e.dxf.handle})

    async def pid_connect_equipment(self, x1, y1, x2, y2) -> CommandResult:
        """Connect two points with orthogonal routing."""
        self._ensure_layer("PID-PROCESS-PIPING")
        mid_x = (x1 + x2) / 2
        pts = [(x1, y1), (mid_x, y1), (mid_x, y2), (x2, y2)]
        e = self._msp.add_lwpolyline(pts, dxfattribs={"layer": "PID-PROCESS-PIPING"})
        return CommandResult(ok=True, payload={"entity_type": "LWPOLYLINE", "handle": e.dxf.handle})

    async def pid_add_flow_arrow(self, x, y, rotation=0.0) -> CommandResult:
        self._ensure_layer("PID-ANNOTATION")
        # Simple triangle arrow
        rad = math.radians(rotation)
        size = 2.0
        p1 = (x + size * math.cos(rad), y + size * math.sin(rad))
        p2 = (x + size * 0.5 * math.cos(rad + 2.4), y + size * 0.5 * math.sin(rad + 2.4))
        p3 = (x + size * 0.5 * math.cos(rad - 2.4), y + size * 0.5 * math.sin(rad - 2.4))
        e = self._msp.add_lwpolyline([p1, p2, p3], close=True, dxfattribs={"layer": "PID-ANNOTATION"})
        return CommandResult(ok=True, payload={"entity_type": "LWPOLYLINE", "handle": e.dxf.handle})

    async def pid_add_equipment_tag(self, x, y, tag, description="") -> CommandResult:
        self._ensure_layer("PID-ANNOTATION")
        e = self._msp.add_text(tag, dxfattribs={
            "insert": (x, y), "height": 2.5, "layer": "PID-ANNOTATION",
        })
        result = {"entity_type": "TEXT", "handle": e.dxf.handle, "tag": tag}
        if description:
            e2 = self._msp.add_text(description, dxfattribs={
                "insert": (x, y - 3.5), "height": 1.8, "layer": "PID-ANNOTATION",
            })
            result["description_handle"] = e2.dxf.handle
        return CommandResult(ok=True, payload=result)

    async def pid_add_line_number(self, x, y, line_num, spec) -> CommandResult:
        self._ensure_layer("PID-ANNOTATION")
        text = f"{line_num}-{spec}"
        e = self._msp.add_text(text, dxfattribs={
            "insert": (x, y), "height": 2.0, "layer": "PID-ANNOTATION",
        })
        return CommandResult(ok=True, payload={"entity_type": "TEXT", "handle": e.dxf.handle})

    # --- Spline / selection / explode / stretch ---

    async def create_spline(self, points, layer=None, degree=3, closed=False) -> CommandResult:
        self._ensure_layer(layer)
        if not points or len(points) < 3:
            return CommandResult(ok=False, error="spline requires at least three fit points")
        safe_degree = max(1, min(int(degree), len(points) - 1))
        e = self._msp.add_spline(
            fit_points=[(p[0], p[1]) for p in points],
            degree=safe_degree,
            dxfattribs={"layer": layer or "0"},
        )
        return CommandResult(
            ok=True,
            payload={
                "entity_type": "SPLINE",
                "handle": e.dxf.handle,
                "degree": safe_degree,
            },
        )

    async def entity_select(self, filters) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open")
        entity_type = str(filters.get("type", "") or "").upper()
        layer = filters.get("layer")
        window = filters.get("window")
        try:
            limit = int(filters.get("limit", 200))
        except (TypeError, ValueError):
            return CommandResult(ok=False, error="limit must be an integer")
        if limit < 1 or limit > 1000:
            return CommandResult(ok=False, error="limit must be in [1, 1000]")
        window_box = None
        if window:
            if not isinstance(window, (list, tuple)) or len(window) != 4:
                return CommandResult(
                    ok=False,
                    error="window must be [x1, y1, x2, y2]",
                    error_code="E_PARAMETER_REJECTED",
                )
            window_box = (
                min(window[0], window[2]),
                min(window[1], window[3]),
                max(window[0], window[2]),
                max(window[1], window[3]),
            )
        matches = []
        for e in self._msp:
            if entity_type and e.dxftype() != entity_type:
                continue
            if layer and e.dxf.get("layer", "0") != layer:
                continue
            if window_box is not None and not self._entity_intersects_window(e, window_box):
                continue
            matches.append(
                {
                    "handle": e.dxf.handle,
                    "type": e.dxftype(),
                    "layer": e.dxf.get("layer", "0"),
                }
            )
            if len(matches) >= limit:
                break
        total_after_type_layer = sum(
            1
            for e in self._msp
            if (not entity_type or e.dxftype() == entity_type)
            and (not layer or e.dxf.get("layer", "0") == layer)
        )
        return CommandResult(
            ok=True,
            payload={
                "entities": matches,
                "count": len(matches),
                "total_matching": total_after_type_layer,
                "truncated": len(matches) < total_after_type_layer,
                "filters": {
                    "type": entity_type or None,
                    "layer": layer,
                    "window": window,
                },
            },
        )

    @staticmethod
    def _entity_intersects_window(entity, window_box) -> bool:
        wx1, wy1, wx2, wy2 = window_box
        try:
            from ezdxf import bbox as ezdxf_bbox

            bounds = ezdxf_bbox.extents([entity])
            if not bounds.has_data:
                return False
            ex1, ey1 = float(bounds.extmin.x), float(bounds.extmin.y)
            ex2, ey2 = float(bounds.extmax.x), float(bounds.extmax.y)
        except Exception:
            return False
        return not (ex2 < wx1 or ex1 > wx2 or ey2 < wy1 or ey1 > wy2)

    async def entity_explode(self, entity_id) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open")
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found")
            entity_type = e.dxftype()
            if entity_type == "INSERT":
                from ezdxf.explode import virtual_block_reference_entities

                virtual = list(virtual_block_reference_entities(e))
                handles = []
                for virtual_entity in virtual:
                    self._msp.add_entity(virtual_entity)
                    handles.append(virtual_entity.dxf.handle)
                self._msp.delete_entity(e)
                return CommandResult(
                    ok=True,
                    payload={
                        "exploded": entity_id,
                        "created": len(handles),
                        "handles": handles,
                    },
                )
            if entity_type == "LWPOLYLINE":
                if any(abs(bulge) > 1e-9 for _, _, bulge in e.get_points(format="xyb")):
                    return CommandResult(
                        ok=False,
                        error="LWPOLYLINE with bulges cannot be exploded to exact arcs offline",
                        error_code="E_EXPLODE_UNSUPPORTED",
                    )
                points = [(p[0], p[1]) for p in e.get_points(format="xy")]
                segments = list(zip(points, points[1:]))
                if e.closed and len(points) > 2:
                    segments.append((points[-1], points[0]))
                handles = []
                for start, end in segments:
                    line = self._msp.add_line(start, end, dxfattribs={"layer": e.dxf.layer})
                    handles.append(line.dxf.handle)
                self._msp.delete_entity(e)
                return CommandResult(
                    ok=True,
                    payload={
                        "exploded": entity_id,
                        "created": len(handles),
                        "handles": handles,
                    },
                )
            return CommandResult(
                ok=False,
                error=f"explode is not supported for {entity_type} on the ezdxf backend",
                error_code="E_EXPLODE_UNSUPPORTED",
            )
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def entity_stretch(self, entity_id, window, dx, dy) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open")
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found")
            if not isinstance(window, (list, tuple)) or len(window) != 4:
                return CommandResult(
                    ok=False,
                    error="stretch window must be [x1, y1, x2, y2]",
                    error_code="E_PARAMETER_REJECTED",
                )
            wx1, wy1 = min(window[0], window[2]), min(window[1], window[3])
            wx2, wy2 = max(window[0], window[2]), max(window[1], window[3])

            def inside(x, y) -> bool:
                return wx1 <= x <= wx2 and wy1 <= y <= wy2

            moved = 0
            entity_type = e.dxftype()
            if entity_type == "LINE":
                start = e.dxf.start
                end = e.dxf.end
                if inside(start.x, start.y):
                    e.dxf.start = (start.x + dx, start.y + dy, start.z)
                    moved += 1
                if inside(end.x, end.y):
                    e.dxf.end = (end.x + dx, end.y + dy, end.z)
                    moved += 1
            elif entity_type == "LWPOLYLINE":
                points = e.get_points(format="xyseb")
                stretched = []
                for x, y, sw, ew, bulge in points:
                    if inside(x, y):
                        stretched.append((x + dx, y + dy, sw, ew, bulge))
                        moved += 1
                    else:
                        stretched.append((x, y, sw, ew, bulge))
                e.set_points(stretched, format="xyseb")
            elif entity_type == "CIRCLE":
                center = e.dxf.center
                if inside(center.x, center.y):
                    e.dxf.center = (center.x + dx, center.y + dy, center.z)
                    moved += 1
            else:
                return CommandResult(
                    ok=False,
                    error=f"stretch is not supported for {entity_type} on the ezdxf backend",
                    error_code="E_STRETCH_UNSUPPORTED",
                )
            if moved == 0:
                return CommandResult(
                    ok=True,
                    payload={"stretched": entity_id, "vertices_moved": 0},
                )
            return CommandResult(
                ok=True,
                payload={"stretched": entity_id, "vertices_moved": moved},
            )
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    # --- Inquiry (read-only measurements) ---

    @staticmethod
    def _shoelace_area(points) -> float:
        total = 0.0
        count = len(points)
        for index in range(count):
            x1, y1 = points[index][0], points[index][1]
            x2, y2 = points[(index + 1) % count][0], points[(index + 1) % count][1]
            total += x1 * y2 - x2 * y1
        return abs(total) / 2.0

    def _flattened_points(self, entity) -> list[tuple[float, float]]:
        from ezdxf import path as ezdxf_path

        route = ezdxf_path.make_path(entity)
        return [(p.x, p.y) for p in route.flattening(0.01)]

    async def inquiry_distance(self, p1, p2) -> CommandResult:
        try:
            dx = float(p2[0]) - float(p1[0])
            dy = float(p2[1]) - float(p1[1])
        except (TypeError, ValueError, IndexError):
            return CommandResult(ok=False, error="p1 and p2 must be [x, y] points")
        return CommandResult(
            ok=True,
            payload={
                "p1": [float(p1[0]), float(p1[1])],
                "p2": [float(p2[0]), float(p2[1])],
                "dx": dx,
                "dy": dy,
                "distance": math.hypot(dx, dy),
                "angle": math.degrees(math.atan2(dy, dx)) % 360,
            },
        )

    async def inquiry_area(self, entity_id=None, points=None) -> CommandResult:
        if points:
            if len(points) < 3:
                return CommandResult(ok=False, error="area requires at least three points")
            return CommandResult(
                ok=True,
                payload={
                    "source": "points",
                    "count": len(points),
                    "area": self._shoelace_area(points),
                },
            )
        if not entity_id:
            return CommandResult(ok=False, error="area requires entity_id or points")
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found")
            entity_type = e.dxftype()
            if entity_type in {"LWPOLYLINE", "POLYLINE"} and not e.closed:
                return CommandResult(
                    ok=False,
                    error=f"{entity_type} is not closed; enclosed area is undefined",
                    error_code="E_OPEN_BOUNDARY",
                )
            flat = self._flattened_points(e)
            if len(flat) < 3:
                return CommandResult(ok=False, error="Entity has no measurable area")
            return CommandResult(
                ok=True,
                payload={
                    "source": "entity",
                    "entity_id": str(entity_id),
                    "type": entity_type,
                    "area": self._shoelace_area(flat),
                },
            )
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def inquiry_angle(self, vertex, p1, p2) -> CommandResult:
        try:
            a1 = math.atan2(float(p1[1]) - float(vertex[1]), float(p1[0]) - float(vertex[0]))
            a2 = math.atan2(float(p2[1]) - float(vertex[1]), float(p2[0]) - float(vertex[0]))
        except (TypeError, ValueError, IndexError):
            return CommandResult(ok=False, error="vertex, p1, p2 must be [x, y] points")
        angle = math.degrees(a2 - a1) % 360
        return CommandResult(
            ok=True,
            payload={
                "vertex": [float(vertex[0]), float(vertex[1])],
                "angle": angle,
                "angle_acute": min(angle, 360 - angle),
            },
        )

    async def inquiry_length(self, entity_id) -> CommandResult:
        try:
            e = self._doc.entitydb.get(entity_id)
            if e is None:
                return CommandResult(ok=False, error=f"Entity {entity_id} not found")
            flat = self._flattened_points(e)
            length = sum(
                math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(flat, flat[1:])
            )
            return CommandResult(
                ok=True,
                payload={
                    "entity_id": str(entity_id),
                    "type": e.dxftype(),
                    "length": length,
                },
            )
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def inquiry_bbox(self, entity_id=None, layer=None) -> CommandResult:
        from ezdxf import bbox as ezdxf_bbox

        try:
            if entity_id:
                e = self._doc.entitydb.get(entity_id)
                if e is None:
                    return CommandResult(ok=False, error=f"Entity {entity_id} not found")
                entities = [e]
            else:
                entities = [
                    e for e in self._msp if not layer or e.dxf.get("layer", "0") == layer
                ]
            if not entities:
                return CommandResult(ok=False, error="No entities to measure")
            box = ezdxf_bbox.extents(entities)
            if not box.has_data:
                return CommandResult(ok=False, error="Entities have no geometry bounds")
            return CommandResult(
                ok=True,
                payload={
                    "entity_id": str(entity_id) if entity_id else None,
                    "layer": layer,
                    "count": len(entities),
                    "min": [float(box.extmin.x), float(box.extmin.y)],
                    "max": [float(box.extmax.x), float(box.extmax.y)],
                    "width": float(box.extmax.x - box.extmin.x),
                    "height": float(box.extmax.y - box.extmin.y),
                },
            )
        except Exception as ex:
            return CommandResult(ok=False, error=str(ex))

    async def inquiry_summary(self) -> CommandResult:
        if not self._doc:
            return CommandResult(ok=False, error="No document open")
        by_type: dict[str, int] = {}
        by_layer: dict[str, int] = {}
        for e in self._msp:
            entity_type = e.dxftype()
            layer = e.dxf.get("layer", "0")
            by_type[entity_type] = by_type.get(entity_type, 0) + 1
            by_layer[layer] = by_layer.get(layer, 0) + 1
        payload = {
            "total": sum(by_type.values()),
            "by_type": dict(sorted(by_type.items())),
            "by_layer": dict(sorted(by_layer.items())),
            "layers": [l.dxf.name for l in self._doc.layers],
        }
        bbox_result = await self.inquiry_bbox()
        if bbox_result.ok:
            payload["extents"] = {
                "min": bbox_result.payload["min"],
                "max": bbox_result.payload["max"],
            }
        return CommandResult(ok=True, payload=payload)

    # --- Styles (text, dimension, linetype) ---

    async def textstyle_list(self) -> CommandResult:
        styles = [
            {
                "name": s.dxf.name,
                "font": s.dxf.get("font", ""),
                "fixed_height": float(s.dxf.get("height", 0) or 0),
            }
            for s in self._doc.styles
        ]
        return CommandResult(ok=True, payload={"text_styles": styles})

    async def textstyle_create(self, name, font="arial.ttf", fixed_height=None) -> CommandResult:
        existed = name in self._doc.styles
        style = self._doc.styles.get(name) if existed else self._doc.styles.add(name, font=font)
        style.dxf.font = font
        if fixed_height is not None:
            if fixed_height < 0:
                return CommandResult(ok=False, error="fixed_height must be non-negative")
            style.dxf.height = fixed_height
        return CommandResult(
            ok=True,
            payload={
                "name": name,
                "font": font,
                "fixed_height": float(fixed_height) if fixed_height is not None else 0.0,
                "existed": existed,
            },
        )

    async def textstyle_set_current(self, name) -> CommandResult:
        if name not in self._doc.styles:
            return CommandResult(ok=False, error=f"Text style '{name}' does not exist")
        self._doc.header["$TEXTSTYLE"] = name
        return CommandResult(ok=True, payload={"current_text_style": name})

    _DIMSTYLE_NUMERIC_FIELDS = {
        "dimtxt", "dimasz", "dimexe", "dimexo", "dimgap", "dimtad", "dimjust",
        "dimdec", "dimlfac", "dimscale", "dimclrd", "dimclre", "dimclrt",
        "dimtih", "dimtoh", "dimsd1", "dimsd2", "dimlwd", "dimlwe",
    }

    async def dimstyle_list(self) -> CommandResult:
        styles = []
        for s in self._doc.dimstyles:
            entry = {"name": s.dxf.name}
            for field in self._DIMSTYLE_NUMERIC_FIELDS:
                value = s.dxf.get(field, None)
                if value is not None:
                    entry[field] = value
            styles.append(entry)
        return CommandResult(ok=True, payload={"dim_styles": styles, "current": self._dimstyle})

    async def dimstyle_create(self, name, values=None) -> CommandResult:
        existed = name in self._doc.dimstyles
        style = self._doc.dimstyles.get(name) if existed else self._doc.dimstyles.add(name)
        applied = {}
        rejected = []
        for key, value in (values or {}).items():
            normalized = str(key).lower()
            if normalized not in self._DIMSTYLE_NUMERIC_FIELDS:
                rejected.append(key)
                continue
            try:
                style.dxf.set(normalized, float(value))
                applied[normalized] = float(value)
            except (TypeError, ValueError):
                rejected.append(key)
        payload = {"name": name, "applied": applied, "existed": existed}
        if rejected:
            payload["rejected"] = rejected
        return CommandResult(ok=True, payload=payload)

    async def dimstyle_set_current(self, name) -> CommandResult:
        if name not in self._doc.dimstyles:
            return CommandResult(ok=False, error=f"Dimension style '{name}' does not exist")
        self._dimstyle = name
        self._doc.header["$DIMSTYLE"] = name
        return CommandResult(ok=True, payload={"current_dim_style": name})

    async def linetype_list(self) -> CommandResult:
        linetypes = [
            {"name": lt.dxf.name, "description": lt.dxf.get("description", "")}
            for lt in self._doc.linetypes
        ]
        return CommandResult(ok=True, payload={"linetypes": linetypes})

    async def linetype_create(self, name, pattern=None, description="") -> CommandResult:
        existed = name.upper() in self._doc.linetypes
        if existed:
            return CommandResult(
                ok=True,
                payload={"name": name, "existed": True, "created": False},
            )
        if pattern is None:
            pattern = [1.0, 0.5, -0.25]
        try:
            self._doc.linetypes.add(name, pattern=pattern, description=description)
        except (ValueError, TypeError, ezdxf.DXFError) as exc:
            return CommandResult(ok=False, error=f"Invalid linetype pattern: {exc}")
        return CommandResult(
            ok=True,
            payload={"name": name, "created": True, "existed": False},
        )

    # --- Layouts (paper space) ---

    async def layout_list(self) -> CommandResult:
        names = list(self._doc.layouts.names_in_taborder())
        return CommandResult(ok=True, payload={"layouts": names})

    async def layout_create(self, name) -> CommandResult:
        try:
            self._doc.layouts.new(name)
        except ezdxf.DXFValueError as exc:
            return CommandResult(ok=False, error=str(exc))
        return CommandResult(ok=True, payload={"name": name, "created": True})

    async def layout_set_current(self, name) -> CommandResult:
        if name not in self._doc.layouts:
            return CommandResult(ok=False, error=f"Layout '{name}' does not exist")
        try:
            self._doc.layouts.set_active_layout(name)
        except AttributeError:
            return CommandResult(
                ok=False,
                error="Setting the active layout requires a newer ezdxf",
                error_code="E_LAYOUT_ACTIVATION_UNSUPPORTED",
            )
        return CommandResult(ok=True, payload={"current_layout": name})

    async def layout_add_viewport(
        self,
        layout,
        center,
        width,
        height,
        view_center,
        view_height,
        layer=None,
    ) -> CommandResult:
        try:
            target = self._doc.layouts.get(layout)
        except (KeyError, ezdxf.DXFKeyError):
            return CommandResult(ok=False, error=f"Layout '{layout}' does not exist")
        if width <= 0 or height <= 0 or view_height <= 0:
            return CommandResult(ok=False, error="width, height and view_height must be positive")
        try:
            self._ensure_layer(layer)
            vp = target.add_viewport(
                center=(center[0], center[1]),
                size=(width, height),
                view_center_point=(view_center[0], view_center[1]),
                view_height=view_height,
                dxfattribs={"layer": layer or "0"},
            )
            scale = height / view_height
        except Exception as exc:
            return CommandResult(ok=False, error=f"Viewport creation failed: {exc}")
        return CommandResult(
            ok=True,
            payload={
                "layout": layout,
                "handle": vp.dxf.handle,
                "center": [center[0], center[1]],
                "width": width,
                "height": height,
                "view_center": [view_center[0], view_center[1]],
                "view_height": view_height,
                "scale": round(scale, 8),
            },
        )

    # --- Tables (composite grid + text representation) ---

    def _draw_composite_table(
        self,
        x: float,
        y: float,
        rows: int,
        cols: int,
        row_heights: list[float],
        col_widths: list[float],
        texts: list[list[str]],
        layer: str | None,
        text_height: float | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Draw a table as grid lines + text cells; returns a manifest."""
        from ezdxf.enums import TextEntityAlignment

        grid_handles: list[str] = []
        cell_handles: list[list[str]] = []
        default_h = row_heights[0] if row_heights else 1.0
        total_width = sum(col_widths)
        total_height = sum(row_heights)
        top = y + total_height
        layer_name = layer or "0"

        # Horizontal grid lines (rows + 1)
        offset_y = y
        for row_index in range(rows + 1):
            line = self._msp.add_line(
                (x, offset_y), (x + total_width, offset_y), dxfattribs={"layer": layer_name}
            )
            grid_handles.append(line.dxf.handle)
            offset_y += row_heights[min(row_index, rows - 1)] if rows else 0

        # Vertical grid lines (cols + 1)
        offset_x = x
        for col_index in range(cols + 1):
            line = self._msp.add_line(
                (offset_x, y), (offset_x, top), dxfattribs={"layer": layer_name}
            )
            grid_handles.append(line.dxf.handle)
            offset_x += col_widths[min(col_index, cols - 1)] if cols else 0

        cell_text_height = text_height or (default_h * 0.6)
        for row_index in range(rows):
            row_cells: list[str] = []
            row_y_top = y + sum(row_heights[: row_index + 1])
            row_y_bottom = y + sum(row_heights[:row_index])
            cell_y = (row_y_top + row_y_bottom) / 2 - cell_text_height / 2
            offset_x = x
            for col_index in range(cols):
                text_value = ""
                if texts and row_index < len(texts) and col_index < len(texts[row_index]):
                    text_value = str(texts[row_index][col_index])
                cell_x = offset_x + col_widths[col_index] / 2
                text = self._msp.add_text(
                    text_value,
                    dxfattribs={
                        "insert": (cell_x, cell_y),
                        "height": cell_text_height,
                        "layer": layer_name,
                    },
                )
                text.set_placement(
                    (cell_x, cell_y), align=TextEntityAlignment.MIDDLE_CENTER
                )
                row_cells.append(text.dxf.handle)
                offset_x += col_widths[col_index]
            cell_handles.append(row_cells)

        title_handle = None
        if title:
            title_text = self._msp.add_mtext(
                title,
                dxfattribs={
                    "insert": (x + total_width / 2, top + 2 * cell_text_height),
                    "char_height": cell_text_height * 1.2,
                    "width": total_width,
                    "layer": layer_name,
                },
            )
            title_handle = title_text.dxf.handle

        return {
            "grid": grid_handles,
            "cells": cell_handles,
            "rows": rows,
            "cols": cols,
            "row_heights": list(row_heights),
            "col_widths": list(col_widths),
            "texts": [list(row) for row in texts] if texts else [],
            "x": x,
            "y": y,
            "title": title,
            "title_handle": title_handle,
            "layer": layer_name,
            "text_height": cell_text_height,
        }

    def _table_anchor(self, manifest: dict[str, Any]) -> str:
        return manifest["grid"][0]

    async def table_create(
        self,
        x,
        y,
        rows,
        cols,
        row_height=1.0,
        col_width=10.0,
        title=None,
        cells=None,
        layer=None,
    ) -> CommandResult:
        if rows < 1 or rows > 100 or cols < 1 or cols > 26:
            return CommandResult(
                ok=False,
                error="table size must be rows in [1, 100] and cols in [1, 26]",
                error_code="E_PARAMETER_REJECTED",
            )
        if row_height <= 0 or col_width <= 0:
            return CommandResult(ok=False, error="row_height and col_width must be positive")
        if cells and len(cells) > rows:
            return CommandResult(ok=False, error=f"cells has {len(cells)} rows, table has {rows}")
        self._ensure_layer(layer)
        manifest = self._draw_composite_table(
            float(x),
            float(y),
            rows,
            cols,
            [row_height] * rows,
            [col_width] * cols,
            cells or [],
            layer,
            title=title,
        )
        anchor = self._table_anchor(manifest)
        self._table_manifests[anchor] = manifest
        return CommandResult(
            ok=True,
            payload={
                "representation": "composite_grid",
                "anchor": anchor,
                "rows": rows,
                "cols": cols,
                "grid_handles": manifest["grid"],
                "cell_handles": manifest["cells"],
                "layer": manifest["layer"],
            },
        )

    def _manifest_for(self, entity_id) -> dict[str, Any] | None:
        manifest = self._table_manifests.get(str(entity_id))
        if manifest is None:
            return None
        return manifest

    async def table_set_cell(self, entity_id, row, col, text) -> CommandResult:
        manifest = self._manifest_for(entity_id)
        if manifest is None:
            return CommandResult(
                ok=False,
                error=f"Entity {entity_id} is not a composite table anchor",
                error_code="E_NOT_A_TABLE",
            )
        if not (0 <= row < manifest["rows"]) or not (0 <= col < manifest["cols"]):
            return CommandResult(
                ok=False,
                error=f"Cell [{row}, {col}] is outside the {manifest['rows']}x{manifest['cols']} table",
                error_code="E_PARAMETER_REJECTED",
            )
        handle = manifest["cells"][row][col]
        entity = self._doc.entitydb.get(handle)
        if entity is None:
            return CommandResult(ok=False, error=f"Table cell {handle} no longer exists")
        entity.dxf.text = str(text)
        while len(manifest["texts"]) <= row:
            manifest["texts"].append([])
        while len(manifest["texts"][row]) <= col:
            manifest["texts"][row].append("")
        manifest["texts"][row][col] = str(text)
        return CommandResult(
            ok=True,
            payload={"anchor": str(entity_id), "row": row, "col": col, "text": str(text)},
        )

    async def table_set_col_widths(self, entity_id, widths) -> CommandResult:
        return await self._rebuild_table_with(
            entity_id, col_widths=[float(w) for w in widths]
        )

    async def table_set_row_heights(self, entity_id, heights) -> CommandResult:
        return await self._rebuild_table_with(
            entity_id, row_heights=[float(h) for h in heights]
        )

    async def _rebuild_table_with(self, entity_id, col_widths=None, row_heights=None) -> CommandResult:
        manifest = self._manifest_for(entity_id)
        if manifest is None:
            return CommandResult(
                ok=False,
                error=f"Entity {entity_id} is not a composite table anchor",
                error_code="E_NOT_A_TABLE",
            )
        new_widths = col_widths or manifest["col_widths"]
        new_heights = row_heights or manifest["row_heights"]
        if len(new_widths) != manifest["cols"]:
            return CommandResult(
                ok=False,
                error=f"Expected {manifest['cols']} column widths, got {len(new_widths)}",
            )
        if len(new_heights) != manifest["rows"]:
            return CommandResult(
                ok=False,
                error=f"Expected {manifest['rows']} row heights, got {len(new_heights)}",
            )
        if any(w <= 0 for w in new_widths) or any(h <= 0 for h in new_heights):
            return CommandResult(ok=False, error="widths and heights must be positive")
        # Erase the old grid and cells, then redraw with new geometry.
        stale_handles = list(manifest["grid"]) + [
            handle for row in manifest["cells"] for handle in row
        ]
        if manifest.get("title_handle"):
            stale_handles.append(manifest["title_handle"])
        for handle in stale_handles:
            entity = self._doc.entitydb.get(handle)
            if entity is not None:
                try:
                    self._msp.delete_entity(entity)
                except Exception:
                    pass
        rebuilt = self._draw_composite_table(
            manifest["x"],
            manifest["y"],
            manifest["rows"],
            manifest["cols"],
            new_heights,
            new_widths,
            manifest["texts"],
            manifest["layer"],
            text_height=manifest["text_height"],
            title=manifest["title"],
        )
        self._table_manifests.pop(str(entity_id), None)
        anchor = self._table_anchor(rebuilt)
        self._table_manifests[anchor] = rebuilt
        return CommandResult(
            ok=True,
            payload={
                "anchor": anchor,
                "representation": "composite_grid",
                "col_widths": new_widths,
                "row_heights": new_heights,
            },
        )

    # --- External references ---

    async def xref_list(self) -> CommandResult:
        references = []
        for block in self._doc.blocks:
            if not block_record_is_xref(block):
                continue
            references.append(
                {
                    "name": block.name,
                    "path": block_xref_path(block),
                }
            )
        return CommandResult(ok=True, payload={"xrefs": references})

    async def xref_attach(self, path, x=0.0, y=0.0, name=None) -> CommandResult:
        from ezdxf import xref as ezdxf_xref

        source = Path(str(path)).expanduser()
        if not source.exists():
            return CommandResult(
                ok=False,
                error=f"External reference file not found: {source}",
                error_code="E_FILE_NOT_FOUND",
            )
        block_name = name or source.stem
        try:
            insert = ezdxf_xref.attach(
                self._doc,
                block_name=block_name,
                filename=str(source),
                insert=(x, y),
            )
        except Exception as exc:
            return CommandResult(
                ok=False,
                error=f"XRef attach failed: {exc}",
                error_code="E_XREF_ATTACH_FAILED",
            )
        return CommandResult(
            ok=True,
            payload={
                "name": block_name,
                "path": str(source),
                "insert": [x, y],
                "handle": insert.dxf.handle if insert is not None else None,
            },
        )

    async def xref_detach(self, name) -> CommandResult:
        block = self._find_xref_block(name)
        if block is None:
            return CommandResult(
                ok=False,
                error=f"XRef '{name}' is not attached",
                error_code="E_XREF_NOT_FOUND",
            )
        removed_inserts = self._delete_block_references(name)
        try:
            self._doc.blocks.delete_block(name, safe=False)
            self._doc.entitydb.purge()
        except Exception as exc:
            return CommandResult(
                ok=False,
                error=f"XRef detach failed: {exc}",
                error_code="E_XREF_DETACH_FAILED",
                payload={"removed_inserts": removed_inserts},
            )
        return CommandResult(
            ok=True,
            payload={"name": name, "detached": True, "removed_inserts": removed_inserts},
        )

    def _delete_block_references(self, name) -> int:
        """Remove every modelspace INSERT of a block; returns the count."""
        inserts = [
            e
            for e in self._msp
            if e.dxftype() == "INSERT" and e.dxf.get("name", "") == name
        ]
        for entity in inserts:
            self._msp.delete_entity(entity)
        return len(inserts)

    async def xref_reload(self, name) -> CommandResult:
        from ezdxf import xref as ezdxf_xref

        block = self._find_xref_block(name)
        if block is None:
            return CommandResult(
                ok=False,
                error=f"XRef '{name}' is not attached",
                error_code="E_XREF_NOT_FOUND",
            )
        source = self._resolve_xref_path(block_xref_path(block))
        if source is None:
            return CommandResult(
                ok=False,
                error=f"XRef source file not found for '{name}'",
                error_code="E_FILE_NOT_FOUND",
            )
        # Capture insert placements, drop the definition, re-read the file.
        placements = [
            (
                float(e.dxf.insert.x),
                float(e.dxf.insert.y),
                float(e.dxf.get("xscale", 1.0)),
                float(e.dxf.get("yscale", 1.0)),
                float(e.dxf.get("rotation", 0.0)),
            )
            for e in self._msp
            if e.dxftype() == "INSERT" and e.dxf.get("name", "") == name
        ]
        self._delete_block_references(name)
        try:
            self._doc.blocks.delete_block(name, safe=False)
            self._doc.entitydb.purge()
            ezdxf_xref.define(self._doc, block_name=name, filename=str(source))
        except Exception as exc:
            return CommandResult(
                ok=False,
                error=f"XRef reload failed: {exc}",
                error_code="E_XREF_RELOAD_FAILED",
            )
        for x, y, xscale, yscale, rotation in placements:
            self._msp.add_blockref(
                name,
                (x, y),
                dxfattribs={"xscale": xscale, "yscale": yscale, "rotation": rotation},
            )
        return CommandResult(
            ok=True,
            payload={
                "name": name,
                "reloaded": True,
                "path": str(source),
                "restored_inserts": len(placements),
            },
        )

    def _resolve_xref_path(self, filename: str):
        if not filename:
            return None
        candidate = Path(filename)
        if candidate.exists():
            return candidate
        if not candidate.is_absolute() and self._save_path:
            beside = Path(self._save_path).parent / candidate
            if beside.exists():
                return beside
        return None

    def _find_xref_block(self, name):
        for block in self._doc.blocks:
            if block.name == name and block_record_is_xref(block):
                return block
        return None

    # --- View ---

    async def get_screenshot(self) -> CommandResult:
        data = self._screenshot.capture()
        if data:
            return CommandResult(ok=True, payload=data)
        return CommandResult(ok=False, error="Screenshot render failed")

    # --- Helpers ---

    @staticmethod
    def _color_to_int(color: str | int) -> int:
        if isinstance(color, int):
            return color
        color_map = {
            "red": 1, "yellow": 2, "green": 3, "cyan": 4,
            "blue": 5, "magenta": 6, "white": 7, "grey": 8, "gray": 8,
        }
        return color_map.get(color.lower(), 7)
