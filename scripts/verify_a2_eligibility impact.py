#!/usr/bin/env python3
"""
A-2 Step 2c -- eligibility impact verification.

WHY THIS EXISTS
---------------
Step 2b reported eligibility_impact_report_only = [] for BOTH symbols whose
effective_first_trading_day changed (ICPUSDT, PUMPUSDT). That empty result has
TWO possible causes and the Step 2b output cannot distinguish them:

  (1) Genuine: neither symbol drops below the 60-day gate at any formation date.
  (2) Silent skip: Step 2b looked for a formation date under the keys
      'formation_date' / 'formation_day' / 'as_of'. If universe.json names that
      field something else, the loop hit `continue` for every month and produced
      an empty list that LOOKS like "no impact".

Cause (2) would be the same class of error as the Step 2 coverage gap: an absence
of output mistaken for an absence of effect. This script resolves it by printing
the actual schema and recomputing the impact from the real field.

No downloads. No thresholds touched. universe.json is read only.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from datetime import date

AGE_GATE_DAYS = 60          # FROZEN
AGE_GATE_OPERATOR = ">="    # FROZEN

EXPECTED_UNIVERSE_SHA256 = "ea79bbdeff91bdc3918c523bf248e2c2fe88aa34ec161f2633d0b1940c75c12a"

REPO = pathlib.Path(__file__).resolve().parents[1]
OUTDIR = REPO / "research" / "multicoin_expansion"
UNIVERSE = OUTDIR / "universe.json"
BACKWARD = OUTDIR / "a2_continuity_audit_backward.json"
OUTFILE = OUTDIR / "a2_eligibility_impact_verification.json"

DATE_KEY_CANDIDATES = [
    "formation_date", "formation_day", "as_of", "as_of_date", "asof",
    "formation", "date", "point_in_time", "pit_date", "selection_date",
    "effective_date", "snapshot_date", "window_end", "rank_date",
]


def parse_day(s: str) -> date:
    y, m, d = (int(x) for x in str(s)[:10].split("-"))
    return date(y, m, d)


def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def summarise(v, depth=0):
    """Compact structural description of a value."""
    if isinstance(v, dict):
        return "{" + ", ".join(sorted(v.keys())) + "}"
    if isinstance(v, list):
        return f"[{len(v)} items]" + (f" first={summarise(v[0], depth+1)}" if v else "")
    s = repr(v)
    return s if len(s) <= 60 else s[:57] + "..."


def main() -> int:
    for p in (UNIVERSE, BACKWARD):
        if not p.exists():
            print(f"FATAL: {p} not found", file=sys.stderr)
            return 2

    u_sha = sha256_file(UNIVERSE)
    if u_sha != EXPECTED_UNIVERSE_SHA256:
        print(f"FATAL: universe.json sha256 mismatch\n  expected {EXPECTED_UNIVERSE_SHA256}\n  got      {u_sha}", file=sys.stderr)
        return 2

    universe = json.loads(UNIVERSE.read_text())
    backward = json.loads(BACKWARD.read_text())

    months = universe.get("months", {})
    print(f"universe.json sha256 verified")
    print(f"top-level keys      : {sorted(universe.keys())}")
    print(f"month keys          : {sorted(months.keys())}")
    print()

    if not months:
        print("FATAL: universe.json has no 'months'", file=sys.stderr)
        return 2

    first_key = sorted(months.keys())[0]
    first = months[first_key]
    print(f"=== SCHEMA OF MONTH RECORD '{first_key}' ===")
    for k, v in first.items():
        print(f"  {k}: {summarise(v)}")
    print()
    if isinstance(first.get("members"), list) and first["members"]:
        print("=== SCHEMA OF A MEMBER RECORD ===")
        for k, v in first["members"][0].items():
            print(f"  {k}: {summarise(v)}")
        print()

    # Which candidate key actually carries the formation date?
    found_key = None
    for cand in DATE_KEY_CANDIDATES:
        if cand in first:
            try:
                parse_day(first[cand])
                found_key = cand
                break
            except Exception:
                continue

    if found_key is None:
        print("!!! NO FORMATION-DATE KEY FOUND among candidates:")
        print(f"    {DATE_KEY_CANDIDATES}")
        print("!!! This CONFIRMS cause (2): Step 2b's empty impact list was a")
        print("!!! SILENT SKIP, not a finding. The impact is UNRESOLVED.")
        print()
        print("Full first month record for schema inspection:")
        print(json.dumps(first, indent=2)[:4000])
        OUTFILE.write_text(json.dumps({
            "status": "UNRESOLVED_NO_FORMATION_DATE_KEY",
            "month_record_keys": sorted(first.keys()),
            "candidates_tried": DATE_KEY_CANDIDATES,
            "conclusion": (
                "Step 2b's eligibility_impact_report_only = [] was produced by a "
                "silent skip, not by an absence of impact. Impact remains UNRESOLVED."
            ),
        }, indent=2))
        print(f"\nwritten: {OUTFILE}")
        return 3

    print(f"FORMATION DATE KEY = '{found_key}'")
    print(f"Step 2b searched for: formation_date / formation_day / as_of")
    step2b_would_have_found = found_key in ("formation_date", "formation_day", "as_of")
    print(f"Step 2b's lookup would have SUCCEEDED: {step2b_would_have_found}")
    if not step2b_would_have_found:
        print("=> Step 2b's empty impact list was a SILENT SKIP. Recomputing below.")
    print()

    formation = {m: parse_day(rec[found_key]) for m, rec in sorted(months.items())}
    print("=== FORMATION DATES ===")
    for m, fd in formation.items():
        print(f"  {m}: {fd.isoformat()}  ({len(months[m].get('members', []))} members)")
    print()

    changes = {c["symbol"]: c for c in backward.get("effective_start_changes", [])}
    results = []
    for sym, c in sorted(changes.items()):
        orig = parse_day(c["original_first_trading_day"])
        eff = parse_day(c["effective_first_trading_day"])
        rows = []
        print(f"=== {sym}: {orig} -> {eff} ===")
        for m, fd in formation.items():
            members = months[m].get("members", [])
            is_member = any(x.get("symbol") == sym for x in members)
            rank = next((x.get("rank") for x in members if x.get("symbol") == sym), None)
            a_o = (fd - orig).days
            a_e = (fd - eff).days
            ok_o = a_o >= AGE_GATE_DAYS
            ok_e = a_e >= AGE_GATE_DAYS
            flipped = ok_o and not ok_e
            rows.append({
                "formation_month": m,
                "formation_date": fd.isoformat(),
                "age_days_under_original": a_o,
                "age_days_under_effective": a_e,
                "eligible_under_original": ok_o,
                "eligible_under_effective": ok_e,
                "eligibility_flipped_to_ineligible": flipped,
                "currently_selected_member": is_member,
                "current_rank": rank,
            })
            if flipped or is_member:
                mark = "  <<< FLIPS TO INELIGIBLE" if flipped else ""
                mem = f" member(rank {rank})" if is_member else ""
                print(f"  {m} fd={fd} age_orig={a_o} age_eff={a_e} "
                      f"elig_eff={ok_e}{mem}{mark}")
        flips = [r for r in rows if r["eligibility_flipped_to_ineligible"]]
        flips_affecting_membership = [r for r in flips if r["currently_selected_member"]]
        print(f"  -> months flipping to ineligible: {len(flips)}")
        print(f"  -> of those, currently SELECTED  : {len(flips_affecting_membership)}"
              f"  {[r['formation_month'] for r in flips_affecting_membership]}")
        print()
        results.append({
            "symbol": sym,
            "original_first_trading_day": orig.isoformat(),
            "effective_first_trading_day": eff.isoformat(),
            "months": rows,
            "months_flipping_to_ineligible": [r["formation_month"] for r in flips],
            "months_requiring_membership_rebuild":
                [r["formation_month"] for r in flips_affecting_membership],
        })

    all_rebuild = sorted({m for r in results for m in r["months_requiring_membership_rebuild"]})

    out = {
        "schema_version": 1,
        "deliverable": "A-2 Step 2c -- eligibility impact verification",
        "status": "COMPLETE",
        "purpose": (
            "Resolve whether Step 2b's empty eligibility_impact_report_only was a "
            "genuine null result or a silent skip caused by a formation-date key mismatch."
        ),
        "frozen_inputs": {
            "universe_sha256": EXPECTED_UNIVERSE_SHA256,
            "age_gate_days": AGE_GATE_DAYS,
            "age_gate_operator": AGE_GATE_OPERATOR,
        },
        "formation_date_key_in_universe": found_key,
        "step2b_lookup_would_have_succeeded": step2b_would_have_found,
        "step2b_empty_impact_was_silent_skip": not step2b_would_have_found,
        "formation_dates": {m: d.isoformat() for m, d in formation.items()},
        "symbols": results,
        "formation_months_requiring_membership_rebuild": all_rebuild,
        "prohibitions_observed": [
            "No thresholds altered (age gate 60, operator >=)",
            "universe.json read only, not modified",
            "No archives downloaded",
            "Membership NOT rebuilt -- this reports which months would need it",
            "A-2 detector not run",
            "No Stage 2 work performed",
        ],
    }
    OUTDIR.mkdir(parents=True, exist_ok=True)
    OUTFILE.write_text(json.dumps(out, indent=2))

    print("=== VERDICT ===")
    print(f"formation date key               : {found_key}")
    print(f"Step 2b empty list was a skip    : {not step2b_would_have_found}")
    print(f"months requiring membership rebuild: {all_rebuild if all_rebuild else 'NONE'}")
    print(f"\nwritten: {OUTFILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
