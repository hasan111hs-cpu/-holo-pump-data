"""
OPERATIONAL HEARTBEAT / FAILURE LOGGING

Records whether the infrastructure performed each mandatory stage, separately from
what the strategy observed. Never touches signals, executions, ledgers or thresholds.

    operational_logs/<execution_date>.json

Usage (called from the workflow, no edits needed to collector/signal/execute):

    python heartbeat.py mark COLLECTOR_HEALTH_CHECK ok "3 symbols"
    python heartbeat.py mark SIGNAL_JOB_STARTED ok
    python heartbeat.py audit          # verify stages, run T+1 reconciliation, finalise
    python heartbeat.py sweep          # flag past dates with no observation at all

STAGE SEMANTICS
  *_EXPECTED stages are derived from the clock, not from a step running.
  A stage that should have occurred and did not is an OPERATIONAL FAILURE.
  A date with no signal record and no log is MISSED OBSERVATION - OPERATIONAL FAILURE.

This module NEVER reconstructs a missed prospective event and never labels one canonical.
"""
import json, os, sys, urllib.parse, urllib.request, time
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

LOGS = Path("operational_logs"); LOGS.mkdir(exist_ok=True)
SIG, EXE, DATA = Path("signals"), Path("executions"), Path("data")
BASE_URL = "https://data-api.binance.vision/api/v3/klines"
MIN = 60_000
SYMBOLS = {"HOLO": "HOLOUSDT", "PUMP": "PUMPUSDT"}

# stage -> expected UTC (hour, minute) or None if event-driven
MANDATORY = {
    "COLLECTOR_HEALTH_CHECK": None,
    "SIGNAL_JOB_EXPECTED": (16, 47),
    "SIGNAL_JOB_STARTED": None,
    "DATA_AUDIT_COMPLETE": None,
    "SIGNAL_LOCKED": None,
    "EXECUTION_JOB_EXPECTED": (18, 5),
    "EXECUTION_JOB_STARTED": None,
    "REPORT_GENERATED": None,
    "T1_BINANCE_RECONCILIATION_COMPLETE": None,
}
GRACE_MINUTES = 90          # after which a missing expected stage is a failure


def now():
    return datetime.now(timezone.utc)


def path_for(d):
    return LOGS / f"{d.isoformat()}.json"


def load(d):
    p = path_for(d)
    if p.exists():
        return json.loads(p.read_text())
    return {"execution_date": d.isoformat(), "events": [], "final_status": None}


def save(d, log):
    path_for(d).write_text(json.dumps(log, indent=2))


ORDER = ["SIGNAL_JOB_EXPECTED", "SIGNAL_JOB_STARTED", "COLLECTOR_HEALTH_CHECK",
         "DATA_AUDIT_COMPLETE", "SIGNAL_LOCKED", "EXECUTION_JOB_EXPECTED",
         "EXECUTION_JOB_STARTED", "REPORT_GENERATED",
         "T1_BINANCE_RECONCILIATION_COMPLETE"]
ON_TIME_MINUTES = 15


def ts_of(ev):
    """Old logs used timestamp_utc; new ones use recorded_at_utc. Accept either."""
    return ev.get("recorded_at_utc") or ev.get("timestamp_utc") or ""


def sort_events(events):
    return sorted(events, key=lambda e: (ORDER.index(e["stage"]) if e.get("stage") in ORDER
                                         else 99, ts_of(e)))


def expected_at(stage, d):
    """Clock deadline for an *_EXPECTED stage. Not a time any job can influence."""
    slot = MANDATORY.get(stage)
    if slot is None:
        return None
    return datetime.combine(d, datetime.min.time(), timezone.utc).replace(
        hour=slot[0], minute=slot[1]).isoformat()


def mark(stage, status="ok", detail="", d=None):
    d = d or now().date()
    log = load(d)
    ev = {"stage": stage, "status": status, "detail": detail,
          "recorded_at_utc": now().isoformat()}
    exp = expected_at(stage, d)
    if exp:
        ev["expected_at_utc"] = exp          # the deadline, distinct from when we wrote it
    log["events"].append(ev)
    log["events"] = sort_events(log["events"])
    save(d, log)
    print(f"[heartbeat] {d} {stage} {status} {detail}")


def stages_seen(log):
    return {e["stage"]: e for e in log["events"] if e["status"] == "ok"}


def fetch_qv(symbol, start_ms, end_ms):
    total, cursor, minutes = 0.0, start_ms, 0
    while cursor <= end_ms:
        q = urllib.parse.urlencode({"symbol": symbol, "interval": "1m", "limit": 1000,
                                    "startTime": cursor, "endTime": end_ms})
        req = urllib.request.Request(f"{BASE_URL}?{q}",
                                     headers={"User-Agent": "HOLO-PUMP-Heartbeat/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = json.loads(r.read().decode())
        if not raw:
            break
        for k in raw:
            total += float(k[7]); minutes += 1
        nxt = raw[-1][0] + MIN
        if nxt <= cursor:
            break
        cursor = nxt
        if len(raw) < 1000:
            break
        time.sleep(0.15)
    return total, minutes


def local_qv(sym, start_ms, end_ms):
    with (DATA / f"{sym}-1m.json").open() as f:
        rows = json.load(f)
    total, minutes = 0.0, 0
    for r in rows:
        t = r.get("t", r.get("open_time_ms"))
        if start_ms <= t <= end_ms:
            total += float(r.get("qv", r.get("quote_volume", 0))); minutes += 1
    return total, minutes


def reconcile_t1(exec_date):
    """T+1 check: does the collector's prior-day window still match Binance exactly?"""
    grp = exec_date - timedelta(days=2)          # the window locked yesterday
    a = int(datetime.combine(grp, datetime.min.time(), timezone.utc)
            .replace(hour=18).timestamp() * 1000)
    z = int(datetime.combine(grp + timedelta(days=1), datetime.min.time(), timezone.utc)
            .replace(hour=16, minute=46).timestamp() * 1000)
    out = {}
    for key, sym in SYMBOLS.items():
        try:
            b_qv, b_n = fetch_qv(sym, a, z)
            l_qv, l_n = local_qv(sym, a, z)
            out[key] = {"window_group_day": grp.isoformat(),
                        "binance_quote_volume": b_qv, "collector_quote_volume": l_qv,
                        "abs_difference": b_qv - l_qv,
                        "binance_minutes": b_n, "collector_minutes": l_n,
                        "match": abs(b_qv - l_qv) < 1e-6 and b_n == l_n}
        except Exception as exc:
            out[key] = {"error": str(exc), "match": None}
    return out


def audit(d=None):
    d = d or now().date()
    log = load(d)
    seen = stages_seen(log)
    failures = []

    # 1. mandatory event-driven stages
    for stage, expect in MANDATORY.items():
        if expect is not None or stage in seen:
            continue
        failures.append({"stage": stage, "reason": "stage never reported",
                         "detected_at_utc": now().isoformat()})

    # 1b. LATENESS. An *_EXPECTED stage is a clock fact, so a job may not self-certify
    # punctuality by marking it. Measure the paired *_STARTED event against the deadline.
    timing = {}
    for expected_stage, started_stage in (("SIGNAL_JOB_EXPECTED", "SIGNAL_JOB_STARTED"),
                                          ("EXECUTION_JOB_EXPECTED", "EXECUTION_JOB_STARTED")):
        hh, mm = MANDATORY[expected_stage]
        due = datetime.combine(d, datetime.min.time(), timezone.utc).replace(hour=hh, minute=mm)
        ev = seen.get(started_stage)
        if ev is None:
            late = (now() - due).total_seconds() / 60
            if late > GRACE_MINUTES:
                failures.append({"stage": started_stage,
                                 "reason": f"not started; {late:.0f}min past {due.isoformat()}",
                                 "detected_at_utc": now().isoformat()})
            timing[started_stage] = {"due_utc": due.isoformat(), "started_utc": None,
                                     "lateness_minutes": round(late, 1), "within_grace": False}
            continue
        started = datetime.fromisoformat(ts_of(ev))
        late = (started - due).total_seconds() / 60
        timing[started_stage] = {"due_utc": due.isoformat(),
                                 "started_utc": started.isoformat(),
                                 "lateness_minutes": round(late, 1),
                                 "grace_minutes": GRACE_MINUTES,
                                 "within_grace": late <= GRACE_MINUTES}
        if late > GRACE_MINUTES:
            failures.append({"stage": started_stage,
                             "reason": f"started {late:.0f}min after {due.isoformat()}, "
                                       f"exceeding {GRACE_MINUTES}min grace "
                                       f"(scheduler did not fire on time)",
                             "detected_at_utc": now().isoformat()})

    # 2. artifacts that prove the stage really happened
    if (SIG / f"{d.isoformat()}.json").exists():
        if "SIGNAL_LOCKED" not in seen:
            mark("SIGNAL_LOCKED", "ok", "verified by artifact", d)
            log = load(d); seen = stages_seen(log)
            failures = [f for f in failures if f["stage"] != "SIGNAL_LOCKED"]
    else:
        failures.append({"stage": "SIGNAL_LOCKED",
                         "reason": "no signal record written for this execution date",
                         "detected_at_utc": now().isoformat()})

    prev = d - timedelta(days=1)
    if (SIG / f"{prev.isoformat()}.json").exists() and not (EXE / f"{prev.isoformat()}.json").exists():
        failures.append({"stage": "EXECUTION_SETTLEMENT",
                         "reason": f"signal {prev} exists but was not settled",
                         "detected_at_utc": now().isoformat()})

    # 3. T+1 Binance reconciliation
    rec = reconcile_t1(d)
    log = load(d)
    log["t1_reconciliation"] = rec
    bad = [k for k, v in rec.items() if v.get("match") is False]
    if bad:
        failures.append({"stage": "T1_BINANCE_RECONCILIATION_COMPLETE",
                         "reason": f"collector/Binance mismatch: {', '.join(bad)}",
                         "detected_at_utc": now().isoformat()})
    elif all(v.get("match") for v in rec.values()):
        log["events"].append({"stage": "T1_BINANCE_RECONCILIATION_COMPLETE",
                              "status": "ok", "detail": "exact match both symbols",
                              "recorded_at_utc": now().isoformat()})
        log["events"] = sort_events(log["events"])
        failures = [f for f in failures if f["stage"] != "T1_BINANCE_RECONCILIATION_COMPLETE"]

    # explicit delay fields measured against the two clock deadlines
    def delay_s(stage, slot):
        ev = seen.get(stage)
        if ev is None:
            return None
        due = datetime.combine(d, datetime.min.time(), timezone.utc).replace(
            hour=slot[0], minute=slot[1])
        return round((datetime.fromisoformat(ts_of(ev)) - due).total_seconds(), 1)

    log["delays"] = {
        "signal_start_delay_seconds": delay_s("SIGNAL_JOB_STARTED", (16, 47)),
        "signal_lock_delay_seconds": delay_s("SIGNAL_LOCKED", (16, 47)),
        "execution_start_delay_seconds": delay_s("EXECUTION_JOB_STARTED", (18, 5)),
    }

    # ordering invariant: the signal must be locked before execution begins
    lk, ex = seen.get("SIGNAL_LOCKED"), seen.get("EXECUTION_JOB_STARTED")
    if lk and ex and datetime.fromisoformat(ts_of(ex)) < datetime.fromisoformat(ts_of(lk)):
        failures.append({"stage": "STAGE_ORDERING",
                         "reason": "EXECUTION_JOB_STARTED recorded before SIGNAL_LOCKED",
                         "detected_at_utc": now().isoformat()})

    trigger = os.environ.get("HEARTBEAT_TRIGGER", "")
    sd = log["delays"]["signal_start_delay_seconds"]
    if trigger == "workflow_dispatch":
        classification = "MANUAL_TEST_RUN"
    elif failures or sd is None:
        classification = "MISSED_OBSERVATION_OPERATIONAL_FAILURE"
    elif sd <= ON_TIME_MINUTES * 60:
        classification = "CANONICAL_ON_TIME"
    elif sd <= GRACE_MINUTES * 60:
        classification = "CANONICAL_LATE_WITHIN_GRACE"
    else:
        classification = "MISSED_OBSERVATION_OPERATIONAL_FAILURE"

    log["trigger"] = trigger or "unknown"
    log["validity_classification"] = classification
    log["counts_as_prospective_observation"] = classification.startswith("CANONICAL")
    log["timing"] = timing
    log["failures"] = failures
    log["final_status"] = "OPERATIONAL FAILURE" if failures else "OK"
    log["audited_at_utc"] = now().isoformat()
    save(d, log)

    print(f"\n[heartbeat audit] {d}: {log['final_status']}  "
          f"[{log['validity_classification']}]  "
          f"prospective={log['counts_as_prospective_observation']}")
    for k, v in log["delays"].items():
        print(f"  DELAY    {k}: {v}")
    for k, v in timing.items():
        print(f"  TIMING   {k}: due {v['due_utc'][11:16]}Z, "
              f"started {(v['started_utc'] or 'never')[11:16]}, "
              f"late {v['lateness_minutes']}min, within_grace={v['within_grace']}")
    for f in failures:
        print(f"  FAILURE  {f['stage']}: {f['reason']}")
    for k, v in rec.items():
        print(f"  T+1 {k}: match={v.get('match')} diff={v.get('abs_difference')}")
    return log


def sweep():
    """Flag any execution date with no signal record - a missed prospective lock."""
    dates = sorted(p.stem for p in SIG.glob("*.json"))
    if not dates:
        print("[sweep] no signal records yet"); return
    start, end = date.fromisoformat(dates[0]), now().date()
    missed = []
    d = start
    while d <= end:
        if not (SIG / f"{d.isoformat()}.json").exists():
            log = load(d)
            log["final_status"] = "OPERATIONAL FAILURE"
            log["validity_classification"] = "MISSED_OBSERVATION_OPERATIONAL_FAILURE"
            log["counts_as_prospective_observation"] = False
            log["failures"] = [{"stage": "SIGNAL_LOCKED",
                                "reason": "no signal record; prospective lock never occurred",
                                "detected_at_utc": now().isoformat()}]
            log["note"] = ("NOT reconstructed. A missed causal lock is excluded from the "
                           "prospective sample and is never labelled canonical.")
            save(d, log)
            missed.append(d.isoformat())
        d += timedelta(days=1)
    print(f"[sweep] {start} -> {end}: {len(missed)} missed observation(s)")
    for m in missed:
        print(f"  MISSED  {m}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "audit"
    if cmd == "mark":
        try:
            mark(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "ok",
                 " ".join(sys.argv[4:]) if len(sys.argv) > 4 else "")
        except Exception as exc:
            # Observability must never be able to break the thing it observes.
            print(f"[heartbeat] WARNING: mark failed: {exc}")
    elif cmd == "audit":
        audit()
    elif cmd == "sweep":
        sweep()
    else:
        print(f"unknown command: {cmd}")
