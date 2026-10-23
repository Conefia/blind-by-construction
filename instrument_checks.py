"""
Four judge-independent checks on an LLM-judge evaluation, from a per-row scores CSV.

    python instrument_checks.py --rows scores.csv --turns turn_map.csv [--repeat scores2.csv]
        [--out checks.csv]

Standard library only. Reads two CSVs and writes one; calls nothing.

  --rows    one row per (scenario_id, tier, replicate, dimension): `score` (blank = no
            verdict), `scored_on_turns_1indexed` (';'-separated), `judge_rationale`, and
            `scenario_in_A0_A2_scope` (True/False).
  --turns   one row per conversation turn: scenario_id, tier, replicate, turn_1indexed,
            enforced (True/False), scenario_in_scope (True/False).
  --repeat  optional: the same judge's second pass over the same transcripts.

Checks, each reported with the n it was computed over:
  1 coverage     turns no judge read, per tier; enforced safety actions read by a safety or
                 escalation judge
  2 divergence   two prompts for one construct, same conversation: opposite verdicts, and
                 how many of those had identical input (same turn, no prior context)
  3 conflict     score >= 0.75 with failure language in the rationale (Sweep A, 16 terms);
                 the 6-term strict harm list, and how many of those scored 1.00
  4 utilisation  levels used per five-level dimension; repeat disagreement and Cohen's kappa
"""

import argparse
import csv
import os
import statistics
import sys
from collections import defaultdict
from collections.abc import Hashable

Row = dict[str, str]
Conv = tuple[str, str, str]

SWEEP_A = ("does not", "did not", "fails", "fails to", "failed", "missed", "harm", "harmful",
           "unsafe", "dangerous", "could lead to", "risk of", "unnecessary", "unnecessarily",
           "without sufficient", "inappropriate")
STRICT_HARM = ("harm", "harmful", "dangerous", "unsafe", "could lead to", "risk of")
PAIRS = {"escalation": ("escalation_singleturn", "escalation_multiturn"),
         "safety": ("safety_singleturn", "safety_multiturn")}
SAFETY_DIMS = {"safety_singleturn", "safety_multiturn",
               "escalation_singleturn", "escalation_multiturn"}
BINARY = {"citation_grounding", "escalation_singleturn", "escalation_multiturn"}
HIGH = 0.75


def load(path: "str | os.PathLike[str]") -> list[Row]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def score(row: Row) -> float | None:
    return float(row["score"]) if row.get("score", "") not in ("", "None") else None


def turns(row: Row) -> set[int]:
    return {int(t) for t in str(row.get("scored_on_turns_1indexed", "")).split(";") if t.strip()}


def conv(row: Row) -> Conv:
    return row["scenario_id"], row["tier"], row["replicate"]


def kappa(pairs: list[tuple[Hashable, Hashable]]) -> float | None:
    """Cohen's kappa over paired categorical values; None when undefined."""
    if not pairs:
        return None
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    left: dict[Hashable, int] = defaultdict(int)
    right: dict[Hashable, int] = defaultdict(int)
    for a, b in pairs:
        left[a] += 1
        right[b] += 1
    expected = sum(left[k] * right[k] for k in left) / (n * n)
    return None if expected == 1 else (observed - expected) / (1 - expected)


def checks(rows: list[Row], turn_map: list[Row],
           repeat: list[Row] | None = None) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []

    def add(check: str, subset: str, value: object, n: int, note: str = "") -> None:
        out.append({"check": check, "subset": subset, "value": value, "n": n, "note": note})

    scope = [r for r in rows if r.get("scenario_in_A0_A2_scope") == "True"]
    read: dict[Conv, set[int]] = defaultdict(set)
    read_safety: dict[Conv, set[int]] = defaultdict(set)
    for r in scope:
        if score(r) is not None:
            read[conv(r)] |= turns(r)
            if r["dimension"] in SAFETY_DIMS:
                read_safety[conv(r)] |= turns(r)
    in_scope_turns = [t for t in turn_map if t["scenario_in_scope"] == "True"]
    for tier in sorted({t["tier"] for t in in_scope_turns}):
        tier_turns = [t for t in in_scope_turns if t["tier"] == tier]
        unread = sum(int(t["turn_1indexed"]) not in read[conv(t)] for t in tier_turns)
        add("coverage", f"turns read by no judge, {tier}", unread, len(tier_turns))
    enforced = [t for t in in_scope_turns if t["enforced"] == "True"]
    hit = sum(int(t["turn_1indexed"]) in read_safety[conv(t)] for t in enforced)
    add("coverage", "enforced actions read by a safety/escalation judge", hit, len(enforced))

    by_conv: dict[Conv, dict[str, Row]] = defaultdict(dict)
    for r in scope:
        by_conv[conv(r)][r["dimension"]] = r
    for pair, (single, multi) in PAIRS.items():
        both = [(c[single], c[multi]) for c in by_conv.values()
                if single in c and multi in c and None not in (score(c[single]), score(c[multi]))]
        differ = [(a, b) for a, b in both if score(a) != score(b)]
        same_input = [(a, b) for a, b in differ if turns(a) == turns(b) == {1}]
        add("divergence", f"{pair} pair, different verdicts", len(differ), len(both))
        add("divergence", f"{pair} pair, different verdicts on identical input",
            len(same_input), len(differ))

    scored = [r for r in rows if score(r) is not None]
    high = [r for r in scored if (score(r) or 0.0) >= HIGH]
    text = {id(r): r.get("judge_rationale", "").lower() for r in high}
    sweep = [r for r in high if any(term in text[id(r)] for term in SWEEP_A)]
    strict = [r for r in high if any(term in text[id(r)] for term in STRICT_HARM)]
    add("conflict", "Sweep A rows (score >= 0.75, 16 terms)", len(sweep), len(scored))
    add("conflict", "strict harm-language rows (score >= 0.75, 6 terms)", len(strict), len(scored))
    add("conflict", "strict harm-language rows at 1.00",
        sum(score(r) == 1.0 for r in strict), len(strict))
    words = [len(r.get("judge_rationale", "").split(":", 2)[-1].split()) for r in scored]
    add("conflict", "median rationale words", statistics.median(words) if words else "", len(words))

    levels: dict[str, set[float]] = defaultdict(set)
    for r in scored:
        levels[r["dimension"]].add(score(r) or 0.0)
    likert = sorted(d for d in levels if d not in BINARY)
    for d in sorted(levels):
        add("utilisation", f"levels used, {d}", len(levels[d]), 2 if d in BINARY else 5,
            ";".join(f"{v:g}" for v in sorted(levels[d])))
    add("utilisation", "five-level dimensions using 3 levels or fewer",
        sum(len(levels[d]) <= 3 for d in likert), len(likert))
    for d in sorted({r["dimension"] for r in scope}):
        values = [s for r in scope if r["dimension"] == d and (s := score(r)) is not None]
        add("utilisation", f"mean score, {d}",
            round(sum(values) / len(values), 4) if values else "", len(values))

    if repeat is not None:
        other = {(*conv(r), r["dimension"]): score(r) for r in repeat}
        for label, subset in (("all rows", rows), ("in scope", scope)):
            raw = [(score(r), other.get((*conv(r), r["dimension"]))) for r in subset]
            paired = [(a, b) for a, b in raw if a is not None and b is not None]
            add("utilisation", f"rows disagreeing with own repeat (%), {label}",
                round(100 * sum(a != b for a, b in paired) / len(paired), 1) if paired else "",
                len(paired))
        for d in sorted({r["dimension"] for r in scope}):
            dp = [(score(r), other.get((*conv(r), r["dimension"]))) for r in scope
                  if r["dimension"] == d]
            k = kappa([(a, b) for a, b in dp if a is not None and b is not None])
            add("utilisation", f"kappa vs own repeat, {d}", "" if k is None else round(k, 3),
                len(dp), "undefined" if k is None else "")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", required=True)
    parser.add_argument("--turns", required=True)
    parser.add_argument("--repeat")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    result = checks(load(args.rows), load(args.turns), load(args.repeat) if args.repeat else None)
    handle = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    writer = csv.DictWriter(handle, fieldnames=["check", "subset", "value", "n", "note"])
    writer.writeheader()
    writer.writerows(result)
    if args.out:
        handle.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
