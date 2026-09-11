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
import json, sys, urllib.parse, urllib.request, time
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


def mark(stage, status="ok", detail="", d=None):
    d = d or now().date()
    log = load(d)
    log["events"].append({"stage": stage, "status": status, "detail": detail,
                          "timestamp_utc": now().isoformat()})
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

    # 1. mandatory stages
    for stage, expect in MANDATORY.items():
        if stage in seen:
            continue
        if expect is None:
            failures.append({"stage": stage, "reason": "stage never reported",
                             "detected_at_utc": now().isoformat()})
        else:
            due = datetime.combine(d, datetime.min.time(), timezone.utc).replace(
                hour=expect[0], minute=expect[1])
            if now() > due + timedelta(minutes=GRACE_MINUTES):
                failures.append({"stage": stage,
                                 "reason": f"expected by {due.isoformat()} plus "
                                           f"{GRACE_MINUTES}min grace, not observed",
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
                              "timestamp_utc": now().isoformat()})
        failures = [f for f in failures if f["stage"] != "T1_BINANCE_RECONCILIATION_COMPLETE"]

    log["failures"] = failures
    log["final_status"] = "OPERATIONAL FAILURE" if failures else "OK"
    log["audited_at_utc"] = now().isoformat()
    save(d, log)

    print(f"\n[heartbeat audit] {d}: {log['final_status']}")
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
            log["final_status"] = "MISSED OBSERVATION - OPERATIONAL FAILURE"
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
        mark(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "ok",
             " ".join(sys.argv[4:]) if len(sys.argv) > 4 else "")
    elif cmd == "audit":
        audit()
    elif cmd == "sweep":
        sweep()
    else:
        print(f"unknown command: {cmd}")
