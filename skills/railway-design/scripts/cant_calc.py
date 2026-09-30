#!/usr/bin/env python3
"""Cant (superelevation) of the outer rail for gauge 1520 mm curves.

Normative core (sources kept with the constants; verify against the current
edition before design release):

* ``h = 12.5 * V**2 / R``  (h in mm, V in km/h, R in m) - cant for the
  equilibrium condition, gauge 1520 mm (PTP / OAO "RZD" methodology for
  determining outer-rail cant);
* cant is arranged in curves with ``R <= 4000 m``;
* maximum cant is 150 mm including maintenance tolerances
  (OAO "RZD" directive no. 2288r dated 14.11.2016);
* when the computed cant is below 15 mm the cant is not arranged
  (rail-track engineering practice; confirm per the current methodology);
* permissible unbalanced acceleration ``a_nep <= 0.7 m/s^2`` for passenger
  comfort (default; category-dependent - confirm per STN / design norms).

Unbalanced acceleration is derived consistently with the normative constant:
``a_nep = (h_eq - h) / 163.1`` in m/s^2, where 163.1 = 1000*S/g with the rail
centre distance S = 1.6 m; zero at the equilibrium cant.  The output JSON
flags every limit breach but never silently clips design values.

CLI example
-----------
    uv run python skills/railway-design/scripts/cant_calc.py --curves "100:800,80:1200,60:3000"

Exit codes: 0 success, 1 invalid input, 2 self-test failure.
"""

from __future__ import annotations

import argparse
import json
import math
import sys

CANT_CONST = 12.5            # h[mm] = 12.5 * V[km/h]^2 / R[m], gauge 1520 mm
MAX_CANT_MM = 150.0          # directive 2288r of 14.11.2016 (with tolerances)
MIN_ARRANGED_CANT_MM = 15.0  # below this the cant is not arranged
MAX_RADIUS_NO_CANT_M = 4000.0
UNBAL_ACC_LIMIT_MS2 = 0.7    # default comfort limit; category-dependent
MM_PER_MS2 = 163.1           # 1000 * S / g, S = 1.6 m rail centre distance

SOURCE_NOTES = {
    "formula": "h = 12.5·V²/R (мм), колея 1520 мм — методика ОАО «РЖД» / ПТЭ",
    "radius_4000": "возвышение устраивается в кривых R ≤ 4000 м",
    "max_cant": "максимум 150 мм с учётом допусков — распоряжение ОАО «РЖД» № 2288р от 14.11.2016",
    "min_cant": "при расчёте < 15 мм не устраивается — уточнить по действующей редакции методики",
    "acc_limit": "непогашенное ускорение 0,7 м/с² — типовое значение; уточнить по категории линии",
}


def equilibrium_cant_mm(speed_kmh: float, radius_m: float) -> float:
    """Equilibrium cant h = 12.5 * V^2 / R in mm."""
    if speed_kmh <= 0.0:
        raise ValueError("speed must be positive")
    if radius_m <= 0.0:
        raise ValueError("radius must be positive")
    return CANT_CONST * speed_kmh * speed_kmh / radius_m


def unbalanced_acceleration(h_eq_mm: float, applied_mm: float) -> float:
    """Unbalanced acceleration for cant deficiency (positive) or excess."""
    return (h_eq_mm - applied_mm) / MM_PER_MS2


def equilibrium_speed_kmh(cant_mm: float, radius_m: float) -> float:
    """Speed balanced by the given cant: V = sqrt(h * R / 12.5) in km/h."""
    if cant_mm < 0.0:
        raise ValueError("cant must be non-negative")
    if radius_m <= 0.0:
        raise ValueError("radius must be positive")
    return math.sqrt(cant_mm * radius_m / CANT_CONST)


def evaluate_curve(
    speed_kmh: float,
    radius_m: float,
    max_cant_mm: float = MAX_CANT_MM,
    min_arranged_mm: float = MIN_ARRANGED_CANT_MM,
    radius_no_cant_m: float = MAX_RADIUS_NO_CANT_M,
    acc_limit_ms2: float = UNBAL_ACC_LIMIT_MS2,
) -> dict:
    """Evaluate one (V, R) pair: equilibrium cant, applied cant, status."""
    h_eq = equilibrium_cant_mm(speed_kmh, radius_m)
    warnings: list[str] = []
    if radius_m > radius_no_cant_m:
        status = "not_required"
        applied = 0.0
        warnings.append(
            f"R={radius_m:g} м > {radius_no_cant_m:g} м: возвышение, как правило, "
            "не устраивается (уточнить по действующей методике ОАО «РЖД»)"
        )
    elif h_eq > max_cant_mm:
        status = "exceeds_max"
        applied = max_cant_mm
        warnings.append(
            f"h_расч={h_eq:.1f} мм > максимума {max_cant_mm:g} мм: снизить скорость "
            "или увеличить радиус; применённое значение требует решения проектировщика"
        )
    elif h_eq < min_arranged_mm:
        status = "below_minimum"
        applied = 0.0
        warnings.append(
            f"h_расч={h_eq:.1f} мм < {min_arranged_mm:g} мм: возвышение не устраивается "
            "(минимально устраиваемое значение по действующей методике)"
        )
    else:
        status = "normal"
        applied = round(h_eq, 1)
    a_unb = unbalanced_acceleration(h_eq, applied)
    if status != "not_required" and a_unb > acc_limit_ms2:
        warnings.append(
            f"a_неп={a_unb:.3f} м/с² > допуска {acc_limit_ms2:g} м/с²: "
            "непогашенное ускорение недопустимо"
        )
    return {
        "v_kmh": speed_kmh,
        "radius_m": radius_m,
        "h_eq_mm": round(h_eq, 2),
        "h_applied_mm": applied,
        "status": status,
        "a_nep_ms2": round(a_unb, 4),
        "v_equilibrium_kmh": round(equilibrium_speed_kmh(applied, radius_m), 2)
        if applied > 0.0
        else None,
        "warnings": warnings,
    }


def evaluate_curves(pairs: list[tuple[float, float]], **limits) -> dict:
    """Evaluate a list of (V, R) pairs and build the report payload."""
    curves = [evaluate_curve(v, r, **limits) for v, r in pairs]
    flagged = [c for c in curves if c["status"] != "normal" or c["warnings"]]
    return {
        "ok": True,
        "constants": {
            "CANT_CONST": CANT_CONST,
            "MAX_CANT_MM": MAX_CANT_MM,
            "MIN_ARRANGED_CANT_MM": MIN_ARRANGED_CANT_MM,
            "MAX_RADIUS_NO_CANT_M": MAX_RADIUS_NO_CANT_M,
            "UNBAL_ACC_LIMIT_MS2": UNBAL_ACC_LIMIT_MS2,
            "sources": SOURCE_NOTES,
        },
        "limits_used": {
            "max_cant_mm": limits.get("max_cant_mm", MAX_CANT_MM),
            "min_arranged_mm": limits.get("min_arranged_mm", MIN_ARRANGED_CANT_MM),
            "radius_no_cant_m": limits.get("radius_no_cant_m", MAX_RADIUS_NO_CANT_M),
            "acc_limit_ms2": limits.get("acc_limit_ms2", UNBAL_ACC_LIMIT_MS2),
        },
        "curves": curves,
        "flagged_count": len(flagged),
        "note": "нормативы проверяет человек по действующим редакциям документов",
    }


def parse_curves(spec: str) -> list[tuple[float, float]]:
    """Parse a 'V:R,V:R' spec into (speed, radius) pairs."""
    pairs: list[tuple[float, float]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        v_str, _, r_str = chunk.partition(":")
        if not r_str:
            raise ValueError(f"curve '{chunk}' must be V:R, e.g. 100:800")
        pairs.append((float(v_str), float(r_str)))
    if not pairs:
        raise ValueError("at least one V:R pair is required")
    return pairs


def _self_test() -> bool:
    """Deterministic asserts; return True when every check passes."""
    # 1. Equilibrium cant golden values.
    assert abs(equilibrium_cant_mm(100.0, 1000.0) - 125.0) < 1e-9
    assert abs(equilibrium_cant_mm(80.0, 800.0) - 100.0) < 1e-9
    assert abs(equilibrium_cant_mm(60.0, 3000.0) - 15.0) < 1e-9

    # 2. Back-solved equilibrium speed.
    assert abs(equilibrium_speed_kmh(150.0, 1000.0) - 109.544512) < 1e-5

    # 3. Unbalanced acceleration: zero at equilibrium, positive deficiency.
    assert abs(unbalanced_acceleration(125.0, 125.0)) < 1e-12
    assert abs(unbalanced_acceleration(125.0, 45.0) - (80.0 / 163.1)) < 1e-9

    # 4. Statuses.
    normal = evaluate_curve(100.0, 1000.0)
    assert normal["status"] == "normal" and normal["h_applied_mm"] == 125.0
    below = evaluate_curve(50.0, 3000.0)  # h_eq = 10.42 mm < 15 mm
    assert below["status"] == "below_minimum" and below["h_applied_mm"] == 0.0
    boundary = evaluate_curve(60.0, 3000.0)  # h_eq == 15.0 mm -> still arranged
    assert boundary["status"] == "normal" and boundary["h_applied_mm"] == 15.0
    high = evaluate_curve(140.0, 800.0)  # h_eq = 306.25 -> clipped to 150
    assert high["status"] == "exceeds_max" and high["h_applied_mm"] == 150.0
    flat = evaluate_curve(100.0, 5000.0)
    assert flat["status"] == "not_required" and flat["h_applied_mm"] == 0.0

    # 5. Acceleration limit produces a warning when the max cant cannot
    # balance the speed: V=140, R=800 -> h_eq=306.25, applied 150.
    warned = evaluate_curve(140.0, 800.0)
    assert any("a_неп" in w for w in warned["warnings"]), warned
    assert warned["a_nep_ms2"] > UNBAL_ACC_LIMIT_MS2

    # 6. Parser.
    assert parse_curves("100:800, 80:1200") == [(100.0, 800.0), (80.0, 1200.0)]
    try:
        parse_curves("100")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for malformed pair")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Outer-rail cant (gauge 1520) for (V, R) pairs; normative limits flagged.",
    )
    parser.add_argument(
        "--curves",
        type=str,
        default=None,
        help="comma-separated V:R pairs in km/h:m, e.g. \"100:800,80:1200\"",
    )
    parser.add_argument("--max-cant", type=float, default=MAX_CANT_MM, help="max cant, mm")
    parser.add_argument(
        "--min-cant", type=float, default=MIN_ARRANGED_CANT_MM, help="min arranged cant, mm"
    )
    parser.add_argument(
        "--radius-no-cant", type=float, default=MAX_RADIUS_NO_CANT_M,
        help="radius above which the cant is not arranged, m",
    )
    parser.add_argument(
        "--acc-limit", type=float, default=UNBAL_ACC_LIMIT_MS2,
        help="permissible unbalanced acceleration, m/s^2",
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

    if args.curves is None:
        parser.error("--curves is required unless --self-test is used")

    try:
        pairs = parse_curves(args.curves)
        payload = evaluate_curves(
            pairs,
            max_cant_mm=args.max_cant,
            min_arranged_mm=args.min_cant,
            radius_no_cant_m=args.radius_no_cant,
            acc_limit_ms2=args.acc_limit,
        )
    except (ValueError, ZeroDivisionError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1
    print(json.dumps(payload, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
