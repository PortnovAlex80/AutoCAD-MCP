#!/usr/bin/env python3
"""Turnout (switch) geometry for gauge 1520 mm: frog angle and layout data.

The frog (crossing) angle of a turnout with mark M (марка перевода) is defined
exactly by the tangent ratio:

    alpha = arctan(1 / M),   cot(alpha) = M

Marks 1/9, 1/11, 1/18 and 1/22 are the common Russian-practice marks.  The
full turnout length Lp, front lead ``a`` (distance from the switch point to
the turnout centre) and rear lead ``b`` (turnout centre to the frog end) are
album values that depend on rail type (R65, R50) and the standard-design
album (typovoy albom, e.g. projects of the 2720 series).  They CANNOT be
derived from the mark alone; the built-in reference values below are marked
``"verified": false`` and must be confirmed against the applicable album
before dimensioning on a drawing.

For a normal single crossover (съезд) between parallel track axes spaced E,
the distance between turnout centres along the main track is approximately

    L_center = E * M      (approximation; assumes identical marks and the
                           classical straight-connection layout - confirm
                           against the album / design norms)

Pure stdlib; no network.  Exit codes: 0 success, 1 invalid input,
2 self-test failure.
"""

from __future__ import annotations

import argparse
import json
import math
import sys

ALBUM_WARNING = (
    "album values are reference only; confirm against the applicable "
    "standard-design album (e.g. 2720 series) before dimensioning"
)

# Reference album lengths (full turnout length Lp, metres).  marked
# "verified": false on purpose - see ALBUM_WARNING.
REFERENCE_TURNOUTS: dict[str, dict] = {
    "1/9": {"rail": "R65", "length_m": 31.05},
    "1/11": {"rail": "R65", "length_m": 33.35},
    "1/18": {"rail": "R65", "length_m": 57.75},
    "1/22": {"rail": "R65", "length_m": 79.86},
}


def frog_angle_rad(mark: str) -> float:
    """Exact frog angle alpha = arctan(1/M) in radians for mark '1/M'."""
    ratio = parse_mark(mark)
    return math.atan(1.0 / ratio)


def parse_mark(mark: str) -> float:
    """Parse '1/11' (or '11') into the ratio M = cot(alpha)."""
    text = str(mark).strip()
    if "/" in text:
        num_str, _, den_str = text.partition("/")
        if num_str.strip() != "1":
            raise ValueError(f"mark '{mark}' must be in the form 1/M")
        ratio = float(den_str)
    else:
        ratio = float(text)
    if ratio <= 1.0:
        raise ValueError(f"mark denominator must be > 1, got {ratio}")
    return ratio


def format_dms(angle_rad: float) -> str:
    """Format radians as degrees-minutes-seconds, e.g. 6 deg 20' 25\"."""
    total_deg = math.degrees(angle_rad)
    degrees = int(total_deg)
    minutes_full = (total_deg - degrees) * 60.0
    minutes = int(minutes_full)
    seconds = (minutes_full - minutes) * 60.0
    return f"{degrees}deg {minutes}' {seconds:.1f}\""


def turnout_payload(
    mark: str,
    length_m: float | None = None,
    a_m: float | None = None,
    rail: str | None = None,
) -> dict:
    """Build the JSON payload for one turnout mark."""
    ratio = parse_mark(mark)
    alpha = frog_angle_rad(mark)
    reference = REFERENCE_TURNOUTS.get(_canonical_mark(mark), {})
    length = length_m if length_m is not None else reference.get("length_m")
    b_m = None
    if length is not None and a_m is not None:
        if a_m <= 0.0 or a_m >= length:
            raise ValueError("a_m must satisfy 0 < a_m < length")
        b_m = length - a_m
    warnings = [ALBUM_WARNING]
    if length is None:
        warnings.append("full length unknown: provide --length from the album")
    if a_m is None:
        warnings.append("front lead a unknown: provide --a from the album")
    return {
        "ok": True,
        "mark": f"1/{ratio:g}",
        "frog_angle": {
            "rad": round(alpha, 9),
            "deg": round(math.degrees(alpha), 6),
            "dms": format_dms(alpha),
            "tan": round(1.0 / ratio, 9),
            "cot": ratio,
        },
        "album": {
            "rail": rail or reference.get("rail"),
            "length_m": length,
            "a_m": a_m,
            "b_m": round(b_m, 3) if b_m is not None else None,
            "verified": False,
        },
        "warnings": warnings,
    }


def crossover_payload(mark: str, track_spacing_m: float) -> dict:
    """Approximate geometry of a normal single crossover (съезд)."""
    ratio = parse_mark(mark)
    if track_spacing_m <= 0.0:
        raise ValueError("track spacing must be positive")
    payload = turnout_payload(mark)
    payload["crossover"] = {
        "track_axes_spacing_m": track_spacing_m,
        "center_distance_approx_m": round(track_spacing_m * ratio, 3),
        "approximation": True,
        "note": (
            "L_center ~ E * cot(alpha); assumes identical marks and the "
            "classical layout - confirm against the album"
        ),
    }
    return payload


def _canonical_mark(mark: str) -> str:
    return f"1/{parse_mark(mark):g}"


def _self_test() -> bool:
    """Deterministic asserts; return True when every check passes."""
    # 1. Exact frog angles.
    expected_deg = {"1/9": 6.3401917, "1/11": 5.1944289, "1/18": 3.1798301, "1/22": 2.6025622}
    for mark, deg in expected_deg.items():
        assert abs(math.degrees(frog_angle_rad(mark)) - deg) < 1e-6, mark
    # tan(alpha) == 1/M exactly.
    assert abs(math.tan(frog_angle_rad("1/11")) - 1.0 / 11.0) < 1e-12

    # 2. Mark parsing accepts both forms and rejects bad input.
    assert parse_mark("1/9") == 9.0
    assert parse_mark("18") == 18.0
    for bad in ("2/9", "1/0.5", ""):
        try:
            parse_mark(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")

    # 3. Album override arithmetic: b = L - a.
    payload = turnout_payload("1/11", length_m=33.35, a_m=14.44)
    assert abs(payload["album"]["b_m"] - 18.91) < 1e-6
    try:
        turnout_payload("1/11", length_m=33.35, a_m=40.0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for a >= L")

    # 4. Built-in table keeps verified:false and warns.
    payload = turnout_payload("1/18")
    assert payload["album"]["verified"] is False
    assert any("album" in w for w in payload["warnings"])

    # 5. Crossover approximation L_center = E * M.
    cross = crossover_payload("1/9", 5.3)
    assert abs(cross["crossover"]["center_distance_approx_m"] - 47.7) < 1e-6
    assert cross["crossover"]["approximation"] is True

    # 6. DMS formatting sanity: 6.3401917 deg = 6 deg 20' 24.7\".
    text = format_dms(frog_angle_rad("1/9"))
    assert text.startswith("6deg 20'"), text
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Turnout geometry for gauge 1520: frog angle from the mark, "
            "album-reference lengths (verify!), crossover approximation."
        ),
    )
    parser.add_argument("--mark", type=str, help="turnout mark, e.g. 1/11")
    parser.add_argument(
        "--length", type=float, help="override full turnout length Lp, m (from the album)"
    )
    parser.add_argument("--a", type=float, help="override front lead a, m (from the album)")
    parser.add_argument("--rail", type=str, help="rail type label, e.g. R65")
    parser.add_argument(
        "--crossover",
        type=float,
        help="also compute a single crossover for parallel tracks spaced E metres",
    )
    parser.add_argument(
        "--table", action="store_true", help="print the built-in reference table and exit"
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

    if args.table:
        print(
            json.dumps(
                {
                    "ok": True,
                    "reference_turnouts": {
                        mark: {**data, "verified": False, "warning": ALBUM_WARNING}
                        for mark, data in REFERENCE_TURNOUTS.items()
                    },
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 0

    if not args.mark:
        parser.error("--mark is required unless --table or --self-test is used")

    try:
        payload = turnout_payload(args.mark, args.length, args.a, args.rail)
        if args.crossover is not None:
            cross = crossover_payload(args.mark, args.crossover)
            payload["crossover"] = cross["crossover"]
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1
    print(json.dumps(payload, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
