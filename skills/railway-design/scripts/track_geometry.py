#!/usr/bin/env python3
"""Circular curve with transition spirals: elements, coordinates, chainage.

Given one intersection point of the alignment (vertex of the deflection
angle, ВУ) described by the deflection angle ``theta``, the circular radius
``R`` and the transition spiral length ``L`` (0 for a simple curve), compute:

* tangent ``T``, curve length ``K``, external bisector ``Б``, and domer ``Д``;
* with spirals: parameter ``C = R*L``, spiral angle ``beta = L/(2R)``,
  tangent shift ``m``, inner shift ``p``, augmented tangent ``Tp``,
  circular part ``K0``, full curve ``Kfull``, augmented bisector ``Bp``;
* coordinates of НК, НКК, ККК, КК, СЦ (mid of circular arc) and the curve
  centre in the tangent frame with origin at НК, +x along the incoming
  tangent toward ВУ (left turn: +y; right turn mirrors y);
* chainage (пикетаж) of НК/НКК/ККК/КК/СЦ from the chainage of ВУ.

Coordinate and element math is exact (clothoid series); no ezdxf, no network.

CLI example
-----------
    uv run python skills/railway-design/scripts/track_geometry.py \
        --angle-deg 24.35 --radius 800 --spiral 80 --station-vu 2456.78 --turn left

Output JSON contains ``elements`` (``simple`` always, ``augmented`` when
spirals exist), ``coordinates`` and ``chainage`` (see ``--help``).
Exit codes: 0 success, 1 invalid input, 2 self-test failure.
"""

from __future__ import annotations

import argparse
import json
import math
import sys

from clothoid_points import fresnel_xy


def circular_curve(radius: float, theta_rad: float) -> dict[str, float]:
    """Elements of a simple circular curve (no spirals).

    T = R*tan(θ/2), K = R*θ, Б = R*(sec(θ/2) - 1), Д = 2T - K.
    """
    if radius <= 0.0:
        raise ValueError("radius must be positive")
    if not 0.0 < theta_rad < math.pi:
        raise ValueError("deflection angle must be in (0, 180) degrees")
    tangent = radius * math.tan(theta_rad / 2.0)
    curve = radius * theta_rad
    bisector = radius * (1.0 / math.cos(theta_rad / 2.0) - 1.0)
    return {
        "T": tangent,
        "K": curve,
        "B": bisector,
        "D": 2.0 * tangent - curve,
    }


def spiral_shifts(radius: float, spiral_length: float) -> dict[str, float]:
    """Exact clothoid shifts at the spiral end.

    ``beta``  spiral angle at the end, rad (L / 2R);
    ``m``     tangent shift along the main tangent:  X_s - R*sin(beta);
    ``p``     inner shift toward the curve centre:   Y_s - R*(1-cos(beta));
    ``X_s``, ``Y_s`` local clothoid end coordinates (Fresnel series).
    """
    if spiral_length < 0.0:
        raise ValueError("spiral length must be non-negative")
    beta = spiral_length / (2.0 * radius)
    x_end, y_end = fresnel_xy(spiral_length, radius * spiral_length)
    return {
        "beta": beta,
        "m": x_end - radius * math.sin(beta),
        "p": y_end - radius * (1.0 - math.cos(beta)),
        "X_s": x_end,
        "Y_s": y_end,
    }


def augmented_curve(radius: float, theta_rad: float, spiral_length: float) -> dict[str, float]:
    """Elements of a circular curve with equal transition spirals.

    Tp = (R + p)*tan(θ/2) + m;  K0 = R*(θ - 2β);  Kfull = K0 + 2L;
    Бp = (R + p)*sec(θ/2) - R;  Д = 2Tp - Kfull.
    """
    if spiral_length <= 0.0:
        raise ValueError("spiral_length must be positive")
    shifts = spiral_shifts(radius, spiral_length)
    beta = shifts["beta"]
    if 2.0 * beta >= theta_rad:
        raise ValueError(
            "transition spirals do not fit: 2*beta must be smaller than the deflection angle"
        )
    p, m = shifts["p"], shifts["m"]
    tangent_p = (radius + p) * math.tan(theta_rad / 2.0) + m
    k_circular = radius * (theta_rad - 2.0 * beta)
    return {
        "C": radius * spiral_length,
        "beta": beta,
        "m": m,
        "p": p,
        "Tp": tangent_p,
        "K0": k_circular,
        "Kfull": k_circular + 2.0 * spiral_length,
        "Bp": (radius + p) * (1.0 / math.cos(theta_rad / 2.0)) - radius,
        "D": 2.0 * tangent_p - (k_circular + 2.0 * spiral_length),
    }


def curve_coordinates(
    radius: float, theta_rad: float, spiral_length: float, side: str = "left"
) -> dict[str, list[float]]:
    """Coordinates of НК, ВУ, НКК, ККК, КК, СЦ and the curve centre.

    Frame: origin at НК, +x along the incoming tangent toward ВУ, +y to the
    left of the alignment.  ``side`` is the turn direction seen along the
    alignment; for a right turn every y is mirrored.  Units: metres.
    """
    if side not in {"left", "right"}:
        raise ValueError("side must be 'left' or 'right'")
    if spiral_length > 0.0:
        aug = augmented_curve(radius, theta_rad, spiral_length)
        beta = aug["beta"]
        shifts = spiral_shifts(radius, spiral_length)
        x_s, y_s = shifts["X_s"], shifts["Y_s"]
        nk = [0.0, 0.0]
        vu = [aug["Tp"], 0.0]
        nkk = [x_s, y_s]
        center = [x_s - radius * math.sin(beta), y_s + radius * math.cos(beta)]
        mid = [
            center[0] + radius * math.sin(theta_rad / 2.0),
            center[1] - radius * math.cos(theta_rad / 2.0),
        ]
        # The augmented curve is symmetric about the bisector line ВУ-центр.
        # ККК and КК are exact reflections of НКК and НК about that axis; this
        # guarantees circle membership and tangency of the second spiral.
        dx, dy = math.sin(theta_rad / 2.0), -math.cos(theta_rad / 2.0)
        mxx, mxy, myy = dx * dx - dy * dy, 2.0 * dx * dy, dy * dy - dx * dx

        def _reflect(point: list[float]) -> list[float]:
            rx, ry = point[0] - vu[0], point[1] - vu[1]
            return [
                vu[0] + mxx * rx + mxy * ry,
                vu[1] + mxy * rx + myy * ry,
            ]

        kkk = _reflect(nkk)
        kk = _reflect(nk)
    else:
        simple = circular_curve(radius, theta_rad)
        nk = [0.0, 0.0]
        vu = [simple["T"], 0.0]
        nkk = list(nk)
        center = [0.0, radius]
        # Circle parametrisation: P(phi) = center + R*(sin(phi), -cos(phi)).
        kkk = [
            center[0] + radius * math.sin(theta_rad),
            center[1] - radius * math.cos(theta_rad),
        ]
        kk = list(kkk)
        mid = [
            center[0] + radius * math.sin(theta_rad / 2.0),
            center[1] - radius * math.cos(theta_rad / 2.0),
        ]
    points = {
        "nk": nk,
        "vu": vu,
        "nkk": nkk,
        "kkk": kkk,
        "kk": kk,
        "sc": mid,
        "center": center,
    }
    if side == "right":
        points = {name: [x, -y] for name, (x, y) in points.items()}
    return _rounded(points)


def _rounded(points: dict[str, list[float]], digits: int = 6) -> dict[str, list[float]]:
    return {name: [round(value, digits) for value in xy] for name, xy in points.items()}


def chainage(station_vu: float, theta_rad: float, radius: float, spiral_length: float) -> dict:
    """Chainage (пикетаж) of the characteristic points from the chainage of ВУ.

    For a simple curve: ПК НК = ПК ВУ - T; ПК КК = ПК НК + K.
    With spirals: ПК НКК = ПК НК + L; ПК ККК = ПК НКК + K0; ПК КК = ПК ККК + L.
    The domer Д is reported separately: chainage continued along the outgoing
    tangent by s metres past ВУ equals (station_vu + s - Д) on the alignment.
    """
    elements = (
        augmented_curve(radius, theta_rad, spiral_length)
        if spiral_length > 0.0
        else circular_curve(radius, theta_rad)
    )
    tangent = elements["Tp"] if spiral_length > 0.0 else elements["T"]
    full_curve = elements["Kfull"] if spiral_length > 0.0 else elements["K"]
    domer = elements["D"]
    start = station_vu - tangent
    values: dict[str, float | None] = {
        "nk": start,
        "vu": station_vu,
        "domer": domer,
    }
    if spiral_length > 0.0:
        values["nkk"] = start + spiral_length
        values["kkk"] = values["nkk"] + elements["K0"]
        values["kk"] = values["kkk"] + spiral_length
        values["sc"] = values["nkk"] + elements["K0"] / 2.0
    else:
        values["nkk"] = start
        values["kkk"] = start + full_curve
        values["kk"] = start + full_curve
        values["sc"] = start + full_curve / 2.0
    formatted = {
        key: format_pk(val) for key, val in values.items() if isinstance(val, float)
    }
    return {"meters": {k: round(v, 4) if isinstance(v, float) else v for k, v in values.items()},
            "formatted": formatted}


def format_pk(meters: float) -> str:
    """Format metres as a Russian chainage label: 2456.78 -> 'ПК24+56.78'."""
    if meters < 0:
        raise ValueError("chainage must be non-negative")
    picets = int(meters // 100)
    rest = meters - picets * 100
    return f"ПК{picets}+{rest:05.2f}"


def compute(
    angle_deg: float,
    radius: float,
    spiral_length: float = 0.0,
    station_vu: float | None = None,
    side: str = "left",
) -> dict:
    """Full payload: elements, coordinates and (optionally) chainage."""
    theta = math.radians(angle_deg)
    result: dict = {
        "ok": True,
        "input": {
            "angle_deg": angle_deg,
            "theta_rad": round(theta, 9),
            "R_m": radius,
            "L_m": spiral_length,
            "side": side,
        },
    }
    simple = circular_curve(radius, theta)
    result["elements"] = {
        "simple": {k: round(v, 6) for k, v in simple.items()},
    }
    if spiral_length > 0.0:
        aug = augmented_curve(radius, theta, spiral_length)
        result["elements"]["augmented"] = {
            "C_m2": round(aug["C"], 4),
            "beta_deg": round(math.degrees(aug["beta"]), 6),
            "m_m": round(aug["m"], 6),
            "p_m": round(aug["p"], 6),
            "Tp_m": round(aug["Tp"], 6),
            "K0_m": round(aug["K0"], 6),
            "Kfull_m": round(aug["Kfull"], 6),
            "Bp_m": round(aug["Bp"], 6),
            "D_m": round(aug["D"], 6),
        }
    result["coordinates"] = curve_coordinates(radius, theta, spiral_length, side)
    if station_vu is not None:
        if station_vu < 0:
            raise ValueError("station_vu must be non-negative")
        result["chainage"] = chainage(station_vu, theta, radius, spiral_length)
    return result


def _self_test() -> bool:
    """Deterministic asserts; return True when every check passes."""
    theta30 = math.radians(30.0)

    # 1. Golden values for a simple curve R=1000, theta=30 deg.
    simple = circular_curve(1000.0, theta30)
    assert abs(simple["T"] - 267.9491924) < 1e-6, simple
    assert abs(simple["K"] - 523.5987756) < 1e-6, simple
    assert abs(simple["B"] - 35.2761804) < 1e-6, simple
    assert abs(simple["D"] - 12.2996093) < 1e-6, simple

    # 2. Simple-curve coordinates: |vu-center| = R + Б; |vu - sc| = Б.
    coords = curve_coordinates(1000.0, theta30, 0.0, "left")
    dist = lambda a, b: math.hypot(a[0] - b[0], a[1] - b[1])  # noqa: E731
    assert abs(dist(coords["vu"], coords["center"]) - (1000.0 + simple["B"])) < 1e-6
    assert abs(dist(coords["vu"], coords["sc"]) - simple["B"]) < 1e-6
    assert dist(coords["nkk"], coords["center"]) - 1000.0 < 1e-9

    # 3. Spiral curve R=1000, L=100, theta=30 deg: textbook cross-check.
    aug = augmented_curve(1000.0, theta30, 100.0)
    m_ref = 50.0 - 100.0**3 / (240.0 * 1000.0**2)
    p_ref = 100.0**2 / (24.0 * 1000.0) - 100.0**4 / (2688.0 * 1000.0**3)
    tp_ref = (1000.0 + p_ref) * math.tan(theta30 / 2.0) + m_ref
    assert abs(aug["Tp"] - tp_ref) < 1e-3, (aug["Tp"], tp_ref)
    assert abs(aug["K0"] - 423.5987756) < 1e-6
    assert abs(aug["Kfull"] - 623.5987756) < 1e-6
    assert aug["D"] > 0.0

    # 4. Spiral coordinates stay on the circle and close tangentially.
    coords = curve_coordinates(1000.0, theta30, 100.0, "left")
    center = coords["center"]
    for name in ("nkk", "kkk", "sc"):
        assert abs(dist(coords[name], center) - 1000.0) < 1e-6, name
    theta = theta30
    expected_kk = [
        coords["vu"][0] + aug["Tp"] * math.cos(theta),
        coords["vu"][1] + aug["Tp"] * math.sin(theta),
    ]
    assert dist(coords["kk"], expected_kk) < 1e-6
    assert abs(dist(coords["vu"], coords["sc"]) - aug["Bp"]) < 1e-6

    # 5. Right turn mirrors y only.
    right = curve_coordinates(1000.0, theta30, 100.0, "right")
    for name, point in coords.items():
        if name == "vu":
            continue
        assert abs(point[1] + right[name][1]) < 1e-6, name

    # 6. Chainage arithmetic and formatting (values rounded to 4 decimals).
    ch = chainage(2456.78, theta30, 1000.0, 100.0)
    m = ch["meters"]
    assert abs((m["kk"] - m["nk"]) - aug["Kfull"]) < 1e-3
    assert abs((m["nkk"] - m["nk"]) - 100.0) < 1e-3
    assert abs(m["nk"] - (2456.78 - aug["Tp"])) < 1e-3
    assert format_pk(2456.78) == "ПК24+56.78"
    assert format_pk(0.0) == "ПК0+00.00"

    # 7. Validation errors.
    for bad in (
        lambda: circular_curve(-1.0, theta30),
        lambda: circular_curve(1000.0, 0.0),
        lambda: augmented_curve(1000.0, math.radians(2.0), 100.0),
        lambda: format_pk(-1.0),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected ValueError")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Circular curve with transition spirals: T/K/Б/Д, coordinates, "
            "chainage (JSON for AutoCAD MCP railway workflow)."
        ),
    )
    parser.add_argument("--angle-deg", type=float, default=None, help="deflection angle, degrees")
    parser.add_argument("--radius", type=float, default=None, help="circular radius R, m")
    parser.add_argument("--spiral", type=float, default=0.0, help="transition spiral length L, m (0 = none)")
    parser.add_argument(
        "--station-vu", type=float, help="chainage of the angle vertex in metres, e.g. 2456.78"
    )
    parser.add_argument("--turn", choices=("left", "right"), default="left", help="turn direction")
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

    if args.angle_deg is None or args.radius is None:
        parser.error("--angle-deg and --radius are required unless --self-test is used")

    try:
        payload = compute(
            args.angle_deg,
            args.radius,
            args.spiral,
            args.station_vu,
            args.turn,
        )
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1
    print(json.dumps(payload, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
