#!/usr/bin/env python3
"""
A-2 Step 2c (v2) -- eligibility impact verification and membership rebuild.

STEP 2c v1 RESULT: status UNRESOLVED_NO_FORMATION_DATE_KEY. The month record's
date field is named 'formation_timestamp', which was not among v1's candidate
keys. That CONFIRMED the diagnosis: Step 2b's eligibility_impact_report_only = []
was a SILENT SKIP, not a finding. The impact was never computed.

v1 also revealed the real month-record schema:

  eligible_count, exclusions, formation_timestamp, full_eligible_ranking,
  members, requested_top_n, selected_count, volume_window_end,
  volume_window_start

'full_eligible_ranking' means the corrected top-15 can be computed EXACTLY --
the replacement for any removed member is read from the frozen ranking rather
than inferred.

WHAT THIS SCRIPT DOES
---------------------
1. Resolves the formation date from 'formation_timestamp', accepting ISO strings,
   epoch seconds and epoch milliseconds. If that fails it scans every key for a
   parseable date and reports which it used. It does not silently skip.
2. For ICPUSDT and PUMPUSDT (the only symbols whose effective_first_trading_day
   changed), computes eligibility at each formation date under BOTH the original
   and the effective start.
3. Where a currently-selected member flips to ineligible, rebuilds that month's
   top-15 from full_eligible_ranking and reports the exact diff.

NOTHING IS TUNED. AGE_GATE_DAYS = 60, operator >=, ranking rule, selection count
and formation dates are all read as frozen. universe.json is read-only and its
digest is asserted unchanged. No archives are downloaded. Membership in
universe.json is NOT written -- the rebuild is reported for Hasan's ruling.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from datetime import date, datetime, timezone

AGE_GATE_DAYS = 60          # FROZEN
AGE_GATE_OPERATOR = ">="    # FROZEN

EXPECTED_UNIVERSE_SHA256 = "ea79bbdeff91bdc3918c523bf248e2c2fe88aa34ec161f2633d0b1940c75c12a"

REPO = pathlib.Path(__file__).resolve().parents[1]
OUTDIR = REPO / "research" / "multicoin_expansion"
UNIVERSE = OUTDIR / "universe.json"
BACKWARD = OUTDIR / "a2_continuity_audit_backward.json"
OUTFILE = OUTDIR / "a2_eligibility_impact_verification.json"

DATE_KEY_CANDIDATES = [
    "formation_timestamp", "formation_date", "formation_day", "formation",
    "as_of", "as_of_date", "asof", "selection_date", "point_in_time",
    "pit_date", "effective_date", "snapshot_date", "rank_date", "date",
]


def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def to_day(v) -> date:
    """Parse ISO string, epoch seconds, or epoch milliseconds into a UTC date."""
    if isinstance(v, bool):
        raise ValueError("bool is not a date")
    if isinstance(v, (int, float)):
        n = float(v)
        if n > 1e17:        # nanoseconds
            n /= 1e9
        elif n > 1e14:      # microseconds
            n /= 1e6
        elif n > 1e11:      # milliseconds
            n /= 1e3
        return datetime.fromtimestamp(n, tz=timezone.utc).date()
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return to_day(int(s))
        s = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
            return (dt.astimezone(timezone.utc) if dt.tzinfo else dt).date()
        except ValueError:
            y, m, d = (int(x) for x in s[:10].split("-"))
            return date(y, m, d)
    raise ValueError(f"unparseable date: {v!r}")


def summarise(v):
    if isinstance(v, dict):
        return "{" + ", ".join(sorted(v.keys())) + "}"
    if isinstance(v, list):
        return f"[{len(v)} items]" + (f" first={summarise(v[0])}" if v else "")
    s = repr(v)
    return s if len(s) <= 70 else s[:67] + "..."


def sym_of(x):
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        for k in ("symbol", "sym", "ticker", "name"):
            if k in x:
                return x[k]
    return None


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
    if not months:
        print("FATAL: universe.json has no 'months'", file=sys.stderr)
        return 2

    first_key = sorted(months)[0]
    first = months[first_key]
    print("universe.json sha256 verified (unchanged)")
    print(f"=== MONTH RECORD SCHEMA ('{first_key}') ===")
    for k, v in first.items():
        print(f"  {k}: {summarise(v)}")
    print()

    # ---- resolve the formation date field, loudly -------------------------
    found_key, how = None, None
    for cand in DATE_KEY_CANDIDATES:
        if cand in first:
            try:
                to_day(first[cand])
                found_key, how = cand, "named candidate"
                break
            except Exception:
                continue
    if found_key is None:
        for k, v in first.items():
            try:
                d = to_day(v)
                if date(2020, 1, 1) <= d <= date(2030, 1, 1):
                    found_key, how = k, "scanned fallback"
                    break
            except Exception:
                continue
    if found_key is None:
        print("FATAL: no parseable formation date on the month record.", file=sys.stderr)
        print(json.dumps(first, indent=2)[:4000], file=sys.stderr)
        return 3

    print(f"FORMATION DATE FIELD = '{found_key}'  ({how})")
    print(f"  raw value example  : {first[found_key]!r}  -> {to_day(first[found_key])}")
    print()

    formation = {m: to_day(rec[found_key]) for m, rec in sorted(months.items())}
    print("=== FORMATION DATES AND VOLUME WINDOWS ===")
    for m, fd in formation.items():
        rec = months[m]
        ws = rec.get("volume_window_start")
        we = rec.get("volume_window_end")
        try:
            ws = to_day(ws).isoformat() if ws is not None else "?"
        except Exception:
            ws = str(ws)[:10]
        try:
            we = to_day(we).isoformat() if we is not None else "?"
        except Exception:
            we = str(we)[:10]
        print(f"  {m}: formation={fd}  window {ws} .. {we}  "
              f"eligible={rec.get('eligible_count')} selected={rec.get('selected_count')}")
    print()

    if isinstance(first.get("full_eligible_ranking"), list) and first["full_eligible_ranking"]:
        print("=== full_eligible_ranking ELEMENT SCHEMA ===")
        print(f"  length: {len(first['full_eligible_ranking'])}")
        print(f"  first : {summarise(first['full_eligible_ranking'][0])}")
        print(f"  raw   : {json.dumps(first['full_eligible_ranking'][0])[:300]}")
        print()

    # ---- impact -----------------------------------------------------------
    changes = {c["symbol"]: c for c in backward.get("effective_start_changes", [])}
    if not changes:
        print("No effective-start changes to assess.")
    effective = {s: to_day(c["effective_first_trading_day"]) for s, c in changes.items()}
    original = {s: to_day(c["original_first_trading_day"]) for s, c in changes.items()}

    results, rebuilds = [], []
    for sym in sorted(changes):
        o, e = original[sym], effective[sym]
        print(f"=== {sym}:  original {o}  ->  effective {e} ===")
        rows = []
        for m, fd in formation.items():
            rec = months[m]
            members = rec.get("members", [])
            is_member = any(sym_of(x) == sym for x in members)
            rank = next((x.get("rank") for x in members
                         if isinstance(x, dict) and sym_of(x) == sym), None)
            a_o, a_e = (fd - o).days, (fd - e).days
            ok_o, ok_e = a_o >= AGE_GATE_DAYS, a_e >= AGE_GATE_DAYS
            flipped = ok_o and not ok_e
            rows.append({
                "formation_month": m, "formation_date": fd.isoformat(),
                "age_days_under_original": a_o, "age_days_under_effective": a_e,
                "eligible_under_original": ok_o, "eligible_under_effective": ok_e,
                "eligibility_flipped_to_ineligible": flipped,
                "currently_selected_member": is_member, "current_rank": rank,
            })
            if flipped or is_member:
                tag = "   <<< FLIPS TO INELIGIBLE" if flipped else ""
                mem = f"  SELECTED rank={rank}" if is_member else "  (not selected)"
                print(f"  {m}  fd={fd}  age_orig={a_o:>5}  age_eff={a_e:>5}  "
                      f"elig_eff={str(ok_e):<5}{mem}{tag}")
        flips = [r for r in rows if r["eligibility_flipped_to_ineligible"]]
        hits = [r for r in flips if r["currently_selected_member"]]
        print(f"  months flipping to ineligible : {len(flips)} {[r['formation_month'] for r in flips]}")
        print(f"  of those, SELECTED members    : {len(hits)} {[r['formation_month'] for r in hits]}")
        print()
        results.append({
            "symbol": sym,
            "original_first_trading_day": o.isoformat(),
            "effective_first_trading_day": e.isoformat(),
            "months": rows,
            "months_flipping_to_ineligible": [r["formation_month"] for r in flips],
            "months_requiring_membership_rebuild": [r["formation_month"] for r in hits],
        })

    # ---- rebuild affected months from the frozen ranking ------------------
    affected = sorted({m for r in results for m in r["months_requiring_membership_rebuild"]})
    if affected:
        print("=== MEMBERSHIP REBUILD (reported only, universe.json NOT modified) ===")
    for m in affected:
        rec = months[m]
        fd = formation[m]
        top_n = rec.get("requested_top_n") or rec.get("selected_count") or 15
        old = [sym_of(x) for x in rec.get("members", [])]
        ranking = rec.get("full_eligible_ranking") or []

        def still_eligible(s):
            if s in effective:
                return (fd - effective[s]).days >= AGE_GATE_DAYS
            return True   # unchanged effective start -> gate already applied

        new, dropped = [], []
        for x in ranking:
            s = sym_of(x)
            if s is None:
                continue
            if still_eligible(s):
                if len(new) < top_n:
                    new.append(s)
            else:
                dropped.append(s)
        removed = [s for s in old if s not in new]
        added = [s for s in new if s not in old]
        print(f"  {m}  formation={fd}  top_n={top_n}")
        print(f"    removed : {removed}")
        print(f"    added   : {added}")
        print(f"    dropped by age gate anywhere in ranking: {dropped}")
        if len(new) < top_n:
            print(f"    WARNING: only {len(new)} eligible symbols available for {top_n} slots")
        rebuilds.append({
            "formation_month": m, "formation_date": fd.isoformat(), "top_n": top_n,
            "old_members": old, "new_members": new,
            "removed": removed, "added": added,
            "symbols_dropped_by_age_gate": dropped,
            "short_of_top_n": len(new) < top_n,
        })
    if affected:
        print()

    out = {
        "schema_version": 2,
        "deliverable": "A-2 Step 2c -- eligibility impact verification and membership rebuild",
        "status": "COMPLETE",
        "supersedes": "Step 2c v1 (UNRESOLVED_NO_FORMATION_DATE_KEY)",
        "step2b_empty_impact_was_silent_skip": True,
        "step2b_silent_skip_reason": (
            "Step 2b looked for formation_date / formation_day / as_of. The field is "
            "named 'formation_timestamp', so every month hit `continue` and the empty "
            "impact list carried no information."
        ),
        "frozen_inputs": {
            "universe_sha256": EXPECTED_UNIVERSE_SHA256,
            "age_gate_days": AGE_GATE_DAYS,
            "age_gate_operator": AGE_GATE_OPERATOR,
        },
        "formation_date_field": found_key,
        "formation_date_resolution": how,
        "formation_dates": {m: d.isoformat() for m, d in formation.items()},
        "volume_windows": {
            m: {"start": str(months[m].get("volume_window_start")),
                "end": str(months[m].get("volume_window_end"))}
            for m in sorted(months)
        },
        "symbols": results,
        "formation_months_requiring_membership_rebuild": affected,
        "membership_rebuilds": rebuilds,
        "prohibitions_observed": [
            "No thresholds altered (age gate 60, operator >=)",
            "Ranking rule and selection count read as frozen, not recomputed",
            "universe.json read only -- digest asserted unchanged",
            "No archives downloaded",
            "Membership rebuild REPORTED, not written",
            "A-2 detector not run",
            "No Stage 2 work performed",
        ],
    }
    OUTDIR.mkdir(parents=True, exist_ok=True)
    OUTFILE.write_text(json.dumps(out, indent=2))

    print("=== VERDICT ===")
    print(f"formation date field                 : {found_key} ({how})")
    print(f"Step 2b empty impact was silent skip : True")
    for r in results:
        print(f"{r['symbol']}: eff={r['effective_first_trading_day']}  "
              f"flips={r['months_flipping_to_ineligible'] or 'none'}  "
              f"rebuild={r['months_requiring_membership_rebuild'] or 'none'}")
    print(f"formation months requiring rebuild   : {affected if affected else 'NONE'}")
    print(f"\nwritten: {OUTFILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
