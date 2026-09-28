#!/usr/bin/env python3
"""
A-2 Step 2e -- POINT-IN-TIME eligibility recomputation and membership rebuild.

WHY THIS EXISTS
---------------
Step 2d found TWO defects in Step 2:

  PUMPUSDT  Step 2 reported first_in_window_observation = 2025-07-01.
            MEASURED value is 2025-07-10. No bars exist 07-01..07-09.
            Step 2's field was a clamp (max(window_start, birth)), not a
            measurement, so it was wrong for the one symbol with a gap
            straddling the window start.

  AIAUSDT   Step 2 reported 0 in-window gaps. MEASURED: a 39-day gap
            2025-12-12 .. 2026-01-19. Step 2's forward path was also
            defective.

Both change effective_first_trading_day, so eligibility must be recomputed.

THE POINT-IN-TIME QUESTION THIS SCRIPT MAKES EXPLICIT
----------------------------------------------------
The frozen rule reads "first trading day of the latest continuous trading run
reaching the study period". Applied as a SINGLE GLOBAL value per symbol, AIAUSDT
gets 2026-01-20 -- which would retroactively make it ineligible at the 2025-10,
2025-11 and 2025-12 formations, even though at those dates it was trading
normally in a run that began 2025-09-18.

That is not what a point-in-time universe means. This script therefore computes
effective_first_trading_day AS OF EACH FORMATION DATE: the start of the latest
continuous run reaching THAT date. It reports BOTH readings side by side so the
difference is visible and Hasan can rule. It does not choose for him.

It also flags any month where a symbol was NOT TRADING at the formation date
(inside a gap, or after its final bar), which is a separate condition the frozen
rule does not address.

NOTHING IS TUNED. Age gate 60, operator >=, ranking and selection count read as
frozen. universe.json is read-only. No archives are downloaded. Membership is
REPORTED, never written.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

AGE_GATE_DAYS = 60          # FROZEN
AGE_GATE_OPERATOR = ">="    # FROZEN
WINDOW_START = date(2025, 7, 1)

EXPECTED_UNIVERSE_SHA256 = "ea79bbdeff91bdc3918c523bf248e2c2fe88aa34ec161f2633d0b1940c75c12a"

REPO = pathlib.Path(__file__).resolve().parents[1]
OUTDIR = REPO / "research" / "multicoin_expansion"
UNIVERSE = OUTDIR / "universe.json"
BACKWARD = OUTDIR / "a2_continuity_audit_backward.json"
FORWARD = OUTDIR / "a2_continuity_audit_forward.json"
OUTFILE = OUTDIR / "a2_eligibility_pit.json"


def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def to_day(v) -> date:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        n = float(v)
        if n > 1e17: n /= 1e9
        elif n > 1e14: n /= 1e6
        elif n > 1e11: n /= 1e3
        return datetime.fromtimestamp(n, tz=timezone.utc).date()
    s = str(v).strip()
    if s.isdigit():
        return to_day(int(s))
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return (dt.astimezone(timezone.utc) if dt.tzinfo else dt).date()
    except ValueError:
        y, m, d = (int(x) for x in s[:10].split("-"))
        return date(y, m, d)


def sym_of(x):
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        for k in ("symbol", "sym", "ticker", "name"):
            if k in x:
                return x[k]
    return None


def main() -> int:
    for p in (UNIVERSE, BACKWARD, FORWARD):
        if not p.exists():
            print(f"FATAL: {p} not found", file=sys.stderr)
            return 2

    u_sha = sha256_file(UNIVERSE)
    if u_sha != EXPECTED_UNIVERSE_SHA256:
        print(f"FATAL: universe.json sha256 mismatch: {u_sha}", file=sys.stderr)
        return 2

    universe = json.loads(UNIVERSE.read_text())
    back = json.loads(BACKWARD.read_text())
    fwd = json.loads(FORWARD.read_text())

    months = universe.get("months", {})
    if not months:
        print("FATAL: universe.json has no 'months'", file=sys.stderr)
        return 2

    first_rec = months[sorted(months)[0]]
    date_key = next((k for k in ("formation_timestamp", "formation_date", "as_of")
                     if k in first_rec), None)
    if date_key is None:
        print(f"FATAL: no formation date key in {sorted(first_rec)}", file=sys.stderr)
        return 3
    formation = {m: to_day(rec[date_key]) for m, rec in sorted(months.items())}

    # ---- assemble the authoritative per-symbol timeline -------------------
    fwd_by_sym = {r["symbol"]: r for r in fwd["symbols"]}
    back_by_sym = {r["symbol"]: r for r in back["symbols"]}

    timeline = {}
    for sym, f in fwd_by_sym.items():
        birth = to_day(f["original_first_trading_day"])
        measured_first = to_day(f["measured_first_in_window_observation"]) \
            if f["measured_first_in_window_observation"] else None
        last_obs = to_day(f["measured_last_in_window_observation"]) \
            if f["measured_last_in_window_observation"] else None

        gaps = []  # (start, end, source)

        # Pre-window break from Step 2b, corrected by Step 2d's MEASURED first
        # in-window bar. Step 2b anchored on an assumed 2025-07-01 bar; where
        # that assumption was wrong the gap actually runs on to measured_first-1.
        b = back_by_sym.get(sym)
        if b and b.get("latest_break"):
            gs = to_day(b["latest_break"]["gap_start"])
            ge = to_day(b["latest_break"]["gap_end"])
            if (ge + timedelta(days=1)) == WINDOW_START and measured_first and \
                    measured_first > WINDOW_START:
                ge = measured_first - timedelta(days=1)   # extend across the boundary
                src = "step2b_corrected_by_step2d"
            else:
                src = "step2b"
            gaps.append((gs, ge, src))

        # Boundary gap that Step 2b never saw, revealed only by Step 2d.
        elif measured_first and birth < WINDOW_START and measured_first > WINDOW_START:
            gaps.append((WINDOW_START, measured_first - timedelta(days=1),
                         "step2d_boundary"))

        for g in f.get("gaps") or []:
            gaps.append((to_day(g["gap_start"]), to_day(g["gap_end"]), "step2d"))

        gaps.sort()
        timeline[sym] = {"birth": birth, "gaps": gaps, "last_obs": last_obs,
                         "measured_first": measured_first}

    def effective_as_of(sym: str, f: date):
        """(effective start at f, trading_at_f, active_gap_or_None)"""
        t = timeline.get(sym)
        if t is None:
            return None, None, None
        for gs, ge, _ in t["gaps"]:
            if gs <= f <= ge:
                return None, False, (gs, ge)
        if t["last_obs"] and f > t["last_obs"]:
            return None, False, ("after_final_bar", t["last_obs"])
        eff = t["birth"]
        for gs, ge, _ in t["gaps"]:
            if ge < f:
                eff = ge + timedelta(days=1)
        return eff, True, None

    def effective_global(sym: str):
        t = timeline.get(sym)
        if t is None:
            return None
        return (t["gaps"][-1][1] + timedelta(days=1)) if t["gaps"] else t["birth"]

    # symbols whose continuity picture changed at all
    changed = sorted({r["symbol"] for r in back.get("effective_start_changes", [])}
                     | {r["symbol"] for r in fwd.get("symbols_with_in_window_gaps", [])}
                     | {d["symbol"] for d in
                        fwd["agreement_with_step2"]["disagreements_first_observation"]})

    print(f"universe.json sha256 verified; formation date key = '{date_key}'")
    print(f"symbols with any continuity event: {changed}")
    print()

    results, rebuild_months = [], set()
    for sym in changed:
        t = timeline[sym]
        print(f"=== {sym}  birth={t['birth']}  last_obs={t['last_obs']} ===")
        for gs, ge, src in t["gaps"]:
            print(f"    gap {gs} .. {ge}  ({(ge-gs).days+1}d)  [{src}]")
        g_eff = effective_global(sym)
        print(f"    global effective start (single-value reading): {g_eff}")
        rows = []
        for m, f in formation.items():
            members = months[m].get("members", [])
            is_member = any(sym_of(x) == sym for x in members)
            rank = next((x.get("rank") for x in members
                         if isinstance(x, dict) and sym_of(x) == sym), None)
            eff_pit, trading, why = effective_as_of(sym, f)
            age_pit = (f - eff_pit).days if eff_pit else None
            ok_pit = (age_pit is not None and age_pit >= AGE_GATE_DAYS)
            age_glob = (f - g_eff).days if g_eff else None
            ok_glob = (age_glob is not None and age_glob >= AGE_GATE_DAYS)
            age_orig = (f - t["birth"]).days
            ok_orig = age_orig >= AGE_GATE_DAYS
            row = {
                "formation_month": m, "formation_date": f.isoformat(),
                "trading_at_formation": trading,
                "not_trading_reason": [str(x) for x in why] if why else None,
                "effective_start_point_in_time": eff_pit.isoformat() if eff_pit else None,
                "age_point_in_time": age_pit,
                "eligible_point_in_time": ok_pit,
                "effective_start_global": g_eff.isoformat() if g_eff else None,
                "age_global": age_glob,
                "eligible_global": ok_glob,
                "age_under_original_birth": age_orig,
                "eligible_under_original_birth": ok_orig,
                "readings_disagree": ok_pit != ok_glob,
                "currently_selected_member": is_member,
                "current_rank": rank,
                "flips_to_ineligible_point_in_time": ok_orig and not ok_pit,
            }
            rows.append(row)
            if is_member or row["flips_to_ineligible_point_in_time"] \
                    or trading is False or row["readings_disagree"]:
                tags = []
                if is_member: tags.append(f"SELECTED rank={rank}")
                if row["flips_to_ineligible_point_in_time"]: tags.append("FLIPS INELIGIBLE (PIT)")
                if trading is False: tags.append(f"NOT TRADING AT FORMATION {why}")
                if row["readings_disagree"]: tags.append("PIT vs GLOBAL DISAGREE")
                print(f"    {m} f={f} age_pit={age_pit} elig_pit={ok_pit} "
                      f"age_glob={age_glob} elig_glob={ok_glob}   " + " | ".join(tags))
            if is_member and (not ok_pit or trading is False):
                rebuild_months.add(m)
        results.append({"symbol": sym,
                        "birth": t["birth"].isoformat(),
                        "gaps": [{"start": a.isoformat(), "end": b.isoformat(),
                                  "days": (b-a).days+1, "source": s}
                                 for a, b, s in t["gaps"]],
                        "global_effective_start": g_eff.isoformat() if g_eff else None,
                        "months": rows})
        print()

    # ---- membership rebuild, point-in-time reading ------------------------
    rebuilds = []
    for m in sorted(rebuild_months):
        rec = months[m]
        f = formation[m]
        top_n = rec.get("requested_top_n") or rec.get("selected_count") or 15
        old = [sym_of(x) for x in rec.get("members", [])]
        ranking = rec.get("full_eligible_ranking") or []

        def ok(s):
            if s not in timeline:
                return True
            eff, trading, _ = effective_as_of(s, f)
            if trading is False:
                return False
            return eff is not None and (f - eff).days >= AGE_GATE_DAYS

        new, dropped = [], []
        for x in ranking:
            s = sym_of(x)
            if s is None:
                continue
            if ok(s):
                if len(new) < top_n:
                    new.append(s)
            else:
                dropped.append(s)
        removed = [s for s in old if s not in new]
        added = [s for s in new if s not in old]
        print(f"=== REBUILD {m} (formation {f}, top_n={top_n}) ===")
        print(f"    removed : {removed}")
        print(f"    added   : {added}")
        print(f"    dropped anywhere in ranking: {dropped}")
        if len(new) < top_n:
            print(f"    WARNING: only {len(new)} eligible for {top_n} slots")
        rebuilds.append({"formation_month": m, "formation_date": f.isoformat(),
                         "top_n": top_n, "old_members": old, "new_members": new,
                         "removed": removed, "added": added,
                         "dropped_anywhere_in_ranking": dropped,
                         "short_of_top_n": len(new) < top_n})
    if rebuilds:
        print()

    # ---- symbols not trading at a formation they were selected in ---------
    not_trading_selected = []
    for m, f in formation.items():
        for x in months[m].get("members", []):
            s = sym_of(x)
            _, trading, why = effective_as_of(s, f)
            if trading is False:
                not_trading_selected.append(
                    {"formation_month": m, "formation_date": f.isoformat(),
                     "symbol": s, "rank": x.get("rank") if isinstance(x, dict) else None,
                     "reason": [str(y) for y in why] if why else None})

    out = {
        "schema_version": 1,
        "deliverable": "A-2 Step 2e -- point-in-time eligibility and membership rebuild",
        "status": "COMPLETE",
        "inputs": {
            "universe_sha256": EXPECTED_UNIVERSE_SHA256,
            "backward_audit": "a2_continuity_audit_backward.json",
            "forward_audit": "a2_continuity_audit_forward.json",
            "age_gate_days": AGE_GATE_DAYS,
            "age_gate_operator": AGE_GATE_OPERATOR,
        },
        "step2_defects_corrected": [
            "PUMPUSDT first_in_window_observation 2025-07-01 -> MEASURED 2025-07-10",
            "AIAUSDT in-window gap 2025-12-12..2026-01-19 (39d) missed entirely by Step 2",
        ],
        "open_rule_application_question": (
            "effective_first_trading_day applied as a SINGLE GLOBAL value versus "
            "evaluated POINT-IN-TIME at each formation date. Both readings are "
            "reported per month; rows where they disagree are marked "
            "readings_disagree. This script does not choose between them."
        ),
        "formation_dates": {m: d.isoformat() for m, d in formation.items()},
        "symbols": results,
        "formation_months_requiring_membership_rebuild": sorted(rebuild_months),
        "membership_rebuilds_point_in_time": rebuilds,
        "selected_members_not_trading_at_formation": not_trading_selected,
        "prohibitions_observed": [
            "No thresholds altered (age gate 60, operator >=)",
            "Ranking rule and selection count read as frozen",
            "universe.json read only -- digest asserted unchanged",
            "No archives downloaded",
            "Membership REPORTED, not written",
            "Rule-application question reported, not resolved",
            "A-2 detector not run; no Stage 2 work performed",
        ],
    }
    OUTDIR.mkdir(parents=True, exist_ok=True)
    OUTFILE.write_text(json.dumps(out, indent=2))

    print("=== VERDICT ===")
    for r in results:
        flips = [x["formation_month"] for x in r["months"]
                 if x["flips_to_ineligible_point_in_time"]]
        dis = [x["formation_month"] for x in r["months"] if x["readings_disagree"]]
        print(f"{r['symbol']}: global_eff={r['global_effective_start']}  "
              f"flips_PIT={flips or 'none'}  PIT_vs_GLOBAL_disagree={dis or 'none'}")
    print(f"months requiring membership rebuild: {sorted(rebuild_months) or 'NONE'}")
    print(f"selected members not trading at their formation date: "
          f"{len(not_trading_selected)}")
    for r in not_trading_selected:
        print(f"   {r['formation_month']} {r['symbol']} rank={r['rank']} {r['reason']}")
    print(f"\nwritten: {OUTFILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
