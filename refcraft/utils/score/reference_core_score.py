"""Adopted candidate arithmetic, independent of model identity and old grades.

Unknown observations retain their denominator. Rational arithmetic provides a
certificate when the minimum is known despite unknown nonbinding components.
This module does not change the published CURRENT release.
"""
from fractions import Fraction

CAP = {"existence": 2, "correctness": 2, "precision": 3}
WEIGHT = {"existence": Fraction(1, 2), "correctness": Fraction(1),
          "precision": Fraction(3, 2)}
POLICY = {
    "id": "checklist-core-reference-min-v1",
    "formula": "mean_tasks(min(checklist_base, core_min_goal, reference_aspect_mean))",
    "importance_weights": dict.fromkeys(("critical", "high", "medium", "low"), 1),
    "depth_weights": {k: float(v) for k, v in WEIGHT.items()},
    "depth_caps": CAP,
    "old_critical_threshold": None,
    "saturation_filter": False,
    "full_credit": 100,
    "unknowns": "fixed denominator, exact bounds, no imputation",
    "rounding": "round only model-level display, never per-cell intermediates",
    "no_reference_aspect": "omit reference cap, retain task",
}


def item_bounds(spec, row):
    cap = CAP[spec["depth"]]
    if row is None or row["status"] == "unresolved":
        if row is not None and row["score"] is not None:
            raise ValueError("Unknown item has a numeric score")
        return Fraction(0), Fraction(100)
    if row["status"] != "assessed" or type(row["score"]) is not int or not 0 <= row["score"] <= cap:
        raise ValueError("Invalid assessed item")
    result = Fraction(100 * row["score"], cap)
    return result, result


def mean_bounds(rows, weights=None):
    if not rows:
        raise ValueError("Empty denominator")
    weights = weights or [Fraction(1)] * len(rows)
    if len(weights) != len(rows) or any(w <= 0 for w in weights):
        raise ValueError("Invalid weights")
    return tuple(sum(row[i] * w for row, w in zip(rows, weights)) / sum(weights) for i in (0, 1))


def checklist_bounds(specs, observations):
    specs = [s for s in specs if s["importance"] != "zero"]
    if len({s["id"] for s in specs}) != len(specs):
        raise ValueError("Duplicate checklist IDs")
    return mean_bounds([item_bounds(s, observations.get(s["id"])) for s in specs],
                       [WEIGHT[s["depth"]] for s in specs])


def core_bounds(scope, cases):
    expected = {(g["key"], i) for g in scope["outcomes"] for i in range(len(g["test_cases"]))}
    by_key = {(c["outcome_key"], c["case_index"]): c for c in cases}
    if len(by_key) != len(cases) or set(by_key) != expected:
        raise ValueError("Core cases must cover exactly the fixed scope")
    groups = []
    for goal in scope["outcomes"]:
        statuses = [by_key[goal["key"], i]["status"] for i in range(len(goal["test_cases"]))]
        if not statuses or not set(statuses) <= {"pass", "fail", "unresolved"}:
            raise ValueError("Invalid core statuses")
        p, u, n = statuses.count("pass"), statuses.count("unresolved"), len(statuses)
        groups.append((Fraction(100 * p, n), Fraction(100 * (p + u), n)))
    if not groups:
        raise ValueError("Missing core goals")
    return tuple(min(g[i] for g in groups) for i in (0, 1))


def reference_bounds(groups, observations):
    if not groups:
        return None
    return mean_bounds([mean_bounds([item_bounds(s, observations.get(s["id"])) for s in g["items"]]) for g in groups])


def minimum_bounds(*components):
    rows = [r for r in components if r is not None]
    if not rows or any(not 0 <= lo <= hi <= 100 for lo, hi in rows):
        raise ValueError("Invalid score bounds")
    return min(r[0] for r in rows), min(r[1] for r in rows)


def serialized(bounds):
    if bounds is None:
        return None
    lo, hi = bounds
    return {"lower": float(lo), "upper": float(hi), "exact": lo == hi,
            "score": float(lo) if lo == hi else None,
            "rational": [str(lo), str(hi)]}
