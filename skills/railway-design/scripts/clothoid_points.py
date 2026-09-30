#!/usr/bin/env python3
"""Discretize a clothoid (Euler spiral / transition curve) into polyline vertices.

A railway transition curve has curvature growing linearly with arc length:

    k(l) = l / C,   C = A**2 = R * L

where ``R`` is the radius at the spiral end (m), ``L`` the spiral length (m)
and ``C`` the clothoid parameter (m^2).  With the spiral origin at the point
of zero curvature and the local tangent along +x, the exact geometry is given
by the Fresnel integrals expanded as power series:

    x(l) = sum_k (-1)^k * l^(4k+1) / ((4k+1) * (2k)!   * (2C)^(2k))
    y(l) = sum_k (-1)^k * l^(4k+3) / ((4k+3) * (2k+1)! * (2C)^(2k+1))

The tangent angle at arc length ``l`` is ``psi(l) = l**2 / (2C)`` radians.

The JSON payload produced on stdout is shaped so that its ``points`` array can
be passed to the AutoCAD MCP call ``entity`` with
``operation="create_polyline", points=<payload.points>`` (model units: metres).
Pure stdlib: no ezdxf, no network.

CLI example
-----------
    uv run python skills/railway-design/scripts/clothoid_points.py \
        --radius 500 --length 80 --points 33 --origin-x 1200 --origin-y 3400 \
        --direction 42.5 --side left

Output JSON (abridged)::

    {
      "ok": true,
      "input": {"R": 500.0, "L": 80.0, "C": 40000.0, "n": 32, ...},
      "end_tangent_deg": 94.58,
      "chord_m": 79.95,
      "points": [[1200.0, 3400.0], ...]
    }

Exit codes: 0 success, 1 invalid input.
"""

from __future__ import annotations

import argparse
import json
import math
import sys

# Series convergence settings.
_TERM_EPS = 1e-18
_MAX_TERMS = 16


def fresnel_xy(arc_length: float, param_c: float) -> tuple[float, float]:
    """Return the exact clothoid point at ``arc_length`` for parameter ``param_c``.

    The point is measured from the spiral origin (zero curvature) with the
    local tangent along +x.  Pure float math; converges for the tangent angles
    used in railway design (psi < ~90 deg).
    """
    if not math.isfinite(param_c) or param_c <= 0.0:
        raise ValueError("param_c must be a positive finite number")
    if not math.isfinite(arc_length) or arc_length < 0.0:
        raise ValueError("arc_length must be a non-negative finite number")
    x_sum = 0.0
    y_sum = 0.0
    for k in range(_MAX_TERMS):
        sign = -1.0 if k % 2 else 1.0
        two_c_pow = (2.0 * param_c) ** (2 * k)
        term_x = sign * arc_length ** (4 * k + 1) / (
            math.factorial(2 * k) * (4 * k + 1) * two_c_pow
        )
        term_y = sign * arc_length ** (4 * k + 3) / (
            math.factorial(2 * k + 1) * (4 * k + 3) * (2.0 * param_c) ** (2 * k + 1)
        )
        x_sum += term_x
        y_sum += term_y
        if abs(term_x) < _TERM_EPS and abs(term_y) < _TERM_EPS:
            break
    return x_sum, y_sum


def spiral_angle_end(radius: float, spiral_length: float) -> float:
    """Return the tangent angle at the spiral end in radians (beta = L / 2R)."""
    if radius <= 0.0:
        raise ValueError("radius must be positive")
    if spiral_length < 0.0:
        raise ValueError("spiral_length must be non-negative")
    return spiral_length / (2.0 * radius)


def spiral_local_points(radius: float, spiral_length: float, segments: int) -> list[list[float]]:
    """Return ``segments + 1`` clothoid vertices in the local frame (metres).

    Point 0 is the spiral origin ``(0, 0)``; the last point is the spiral end
    where curvature equals ``1 / radius``.  ``y`` is positive for a left turn.
    """
    if radius <= 0.0 or spiral_length <= 0.0:
        raise ValueError("radius and spiral_length must be positive")
    if segments < 2:
        raise ValueError("segments must be >= 2")
    param_c = radius * spiral_length
    points: list[list[float]] = []
    for i in range(segments + 1):
        arc = spiral_length * i / segments
        x, y = fresnel_xy(arc, param_c)
        points.append([x, y])
    return points


def transform_points(
    points: list[list[float]],
    origin_x: float,
    origin_y: float,
    direction_deg: float,
    side: str,
) -> list[list[float]]:
    """Rotate/translate local spiral points into the global frame.

    ``side`` is ``"left"`` or ``"right"`` for the turn direction seen along the
    alignment; a right turn mirrors local y before rotation.
    """
    if side not in {"left", "right"}:
        raise ValueError("side must be 'left' or 'right'")
    angle = math.radians(direction_deg)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    mirror = -1.0 if side == "right" else 1.0
    result: list[list[float]] = []
    for x, y in points:
        y_local = mirror * y
        result.append([
            round(origin_x + x * cos_a - y_local * sin_a, 6),
            round(origin_y + x * sin_a + y_local * cos_a, 6),
        ])
    return result


def polyline_length(points: list[list[float]]) -> float:
    """Total chord length of a vertex list (metres)."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        total += math.hypot(x2 - x1, y2 - y1)
    return total


def build_payload(
    radius: float,
    spiral_length: float,
    segments: int = 32,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
    direction_deg: float = 0.0,
    side: str = "left",
) -> dict:
    """Build the full JSON-serialisable payload for MCP polyline creation."""
    param_c = radius * spiral_length
    beta = spiral_angle_end(radius, spiral_length)
    local = spiral_local_points(radius, spiral_length, segments)
    points = transform_points(local, origin_x, origin_y, direction_deg, side)
    end_tangent = direction_deg + (math.degrees(beta) if side == "left" else -math.degrees(beta))
    end_tangent = (end_tangent + 180.0) % 360.0 - 180.0
    x_end, y_end = local[-1]
    return {
        "ok": True,
        "input": {
            "R_m": radius,
            "L_m": spiral_length,
            "C_m2": param_c,
            "segments": segments,
            "origin": [origin_x, origin_y],
            "direction_deg": direction_deg,
            "side": side,
        },
        "end_tangent_deg": round(end_tangent, 6),
        "local_end": [round(x_end, 6), round(y_end, 6)],
        "chord_m": round(polyline_length(points), 6),
        "points": points,
        "mcp_usage": {
            "tool": "entity",
            "operation": "create_polyline",
            "note": "pass payload.points as the points argument, closed=false",
        },
    }


def _self_test() -> bool:
    """Deterministic asserts; return True when every check passes."""
    # 1. Series equals numeric Fresnel integration (Simpson) at a mid point.
    big_c = 30000.0  # R=300, L=100
    arc = 73.0
    sx, sy = fresnel_xy(arc, big_c)
    steps = 4000
    h = arc / steps
    cx = 0.0
    cy = 0.0
    for i in range(steps + 1):
        s = i * h
        wc = 1.0 if i in (0, steps) else (4.0 if i % 2 else 2.0)
        cx += wc * math.cos(s * s / (2.0 * big_c))
        cy += wc * math.sin(s * s / (2.0 * big_c))
    cx *= h / 3.0
    cy *= h / 3.0
    assert abs(sx - cx) < 1e-9, f"x series {sx} vs integral {cx}"
    assert abs(sy - cy) < 1e-9, f"y series {sy} vs integral {cy}"

    # 2. Closed-form first terms: x = L - L^3/(40 R^2), y = L^2/(6R) - L^4/(336 R^3).
    radius, length = 1000.0, 100.0
    x_end, y_end = fresnel_xy(length, radius * length)
    x_ref = length - length**3 / (40.0 * radius**2) + length**5 / (3456.0 * radius**4)
    y_ref = (
        length**2 / (6.0 * radius)
        - length**4 / (336.0 * radius**3)
        + length**6 / (42240.0 * radius**5)
    )
    assert abs(x_end - x_ref) < 1e-9, f"x_end {x_end} vs ref {x_ref}"
    assert abs(y_end - y_ref) < 1e-9, f"y_end {y_end} vs ref {y_ref}"

    # 3. Numeric end tangent equals beta = L / 2R.
    d = 1e-4
    x1, y1 = fresnel_xy(length - d, radius * length)
    x2, y2 = fresnel_xy(length, radius * length)
    numeric_angle = math.atan2(y2 - y1, x2 - x1)
    assert abs(numeric_angle - spiral_angle_end(radius, length)) < 1e-6

    # 4. Discretized polyline length converges to the arc length.
    pts = spiral_local_points(radius, length, 50)
    assert abs(polyline_length(pts) - length) / length < 0.001

    # 5. Mirror consistency: right-turn y equals minus left-turn y before
    # rotation; after transform with direction 0 only y flips sign.
    left = transform_points(pts, 10.0, 20.0, 0.0, "left")
    right = transform_points(pts, 10.0, 20.0, 0.0, "right")
    for (lx, ly), (rx, ry) in zip(left, right):
        assert abs(lx - rx) < 1e-6 and abs(ly + ry - 40.0) < 1e-6

    # 6. Payload sanity: first point is the origin, count matches segments + 1.
    payload = build_payload(500.0, 80.0, segments=16, origin_x=5.0, origin_y=6.0)
    assert payload["points"][0] == [5.0, 6.0]
    assert len(payload["points"]) == 17
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Discretize a railway clothoid into MCP polyline vertices (JSON).",
    )
    parser.add_argument("--radius", type=float, help="radius at the spiral end R, m")
    parser.add_argument(
        "--parameter",
        type=float,
        help="clothoid parameter C = A^2, m^2 (alternative to --radius)",
    )
    parser.add_argument("--length", type=float, default=None, help="spiral length L, m")
    parser.add_argument(
        "--points", type=int, default=33, help="number of output vertices (default 33)"
    )
    parser.add_argument("--origin-x", type=float, default=0.0, help="global x of spiral start, m")
    parser.add_argument("--origin-y", type=float, default=0.0, help="global y of spiral start, m")
    parser.add_argument(
        "--direction", type=float, default=0.0, help="initial tangent bearing, degrees"
    )
    parser.add_argument(
        "--side", choices=("left", "right"), default="left", help="turn direction"
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

    if args.length is None:
        parser.error("--length is required unless --self-test is used")

    try:
        if (args.radius is None) == (args.parameter is None):
            raise ValueError("provide exactly one of --radius or --parameter")
        radius = args.radius if args.radius is not None else args.parameter / args.length
        payload = build_payload(
            radius,
            args.length,
            segments=max(2, args.points - 1),
            origin_x=args.origin_x,
            origin_y=args.origin_y,
            direction_deg=args.direction,
            side=args.side,
        )
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1
    print(json.dumps(payload, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
