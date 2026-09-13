"""
CANONICAL BTC.D COLLECTOR — Newhedge.

Registry: Newhedge is the canonical live BTC.D source (§1). CoinGecko is retained only
as NON_CANONICAL_REFERENCE (§2). No fallback provider for canonical trading (§7).

QUOTA (§8): one scheduled retrieval per day with durable local caching. Once D-1 and D-2
are cached for the execution date, no further API call is made. Target ~31 calls/month.

FAIL-CLOSED (§7): if the required observations cannot be obtained validly before cutoff,
the day becomes NO TRADE - DATA. This module never substitutes CoinGecko, never reuses
stale data, never interpolates, never assumes 1.00.

    python newhedge_collector.py           # ensure today's D-1/D-2 are cached
    python newhedge_collector.py --check    # causal-availability test, no cache write
"""
import json, os, sys, time, hashlib, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

DATA = Path("data")
STORE = DATA / "BTCD-NEWHEDGE.json"
ENDPOINT = "https://newhedge.io/api/v2/metrics/bitcoin-dominance/btc_dominance"
CUTOFF_H, CUTOFF_M = 16, 47
MAX_ATTEMPTS = 3


def now():
    return datetime.now(timezone.utc)


def load_store():
    if not STORE.exists():
        return {"provider": "Newhedge", "metric": "btc_dominance", "resolution": "daily",
                "observations": {}, "retrievals": []}
    return json.loads(STORE.read_text())


def save_store(s):
    DATA.mkdir(exist_ok=True)
    STORE.write_text(json.dumps(s, indent=2))


def fetch():
    """One API call. Returns (observations dict keyed by ISO date, audit record)."""
    token = os.environ.get("NEWHEDGE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("NEWHEDGE_TOKEN not set - cannot retrieve canonical BTC.D")
    url = f"{ENDPOINT}?api_token={token}"
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "holo-pump-canonical/1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            pairs = json.loads(raw)
            if not isinstance(pairs, list) or not pairs:
                raise ValueError(f"unexpected payload shape: {str(pairs)[:120]}")
            obs = {}
            for ts, val in pairs:
                d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                # Registry: Newhedge day-1 observations are stamped 00:00:00 UTC.
                if (d.hour, d.minute, d.second) != (0, 0, 0):
                    raise ValueError(f"timestamp {d.isoformat()} is not 00:00:00 UTC")
                obs[d.date().isoformat()] = float(val)
            audit = {
                "retrieved_at_utc": now().isoformat(),
                "attempt": attempt,
                "observation_count": len(obs),
                "latest_observation_date": max(obs),
                "response_sha256": hashlib.sha256(raw).hexdigest(),
                "source_status": "ok",
            }
            return obs, audit
        except Exception as exc:
            last = exc
            print(f"[newhedge] attempt {attempt}/{MAX_ATTEMPTS} failed: {exc}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(attempt * 5)
    raise RuntimeError(f"canonical BTC.D retrieval failed after {MAX_ATTEMPTS} attempts: {last}")


def required(exec_date):
    return (exec_date - timedelta(days=1)).isoformat(), (exec_date - timedelta(days=2)).isoformat()


def ensure(exec_date):
    """Cache D-1 and D-2 for exec_date. Returns the canonical block, or raises."""
    store = load_store()
    d1, d2 = required(exec_date)
    have = store["observations"]

    if d1 in have and d2 in have:
        print(f"[newhedge] D-1 {d1} and D-2 {d2} already cached - no API call")
    else:
        print(f"[newhedge] cache miss for {d1} / {d2} - one API call")
        obs, audit = fetch()
        store["observations"].update(obs)
        audit["canonical_for_execution_date"] = exec_date.isoformat()
        store["retrievals"].append(audit)
        store["retrievals"] = store["retrievals"][-60:]
        save_store(store)
        have = store["observations"]

    if d1 not in have or d2 not in have:
        raise RuntimeError(
            f"FAIL-CLOSED: canonical Newhedge observations missing "
            f"(D-1 {d1}: {'ok' if d1 in have else 'MISSING'}, "
            f"D-2 {d2}: {'ok' if d2 in have else 'MISSING'}). Day is NO TRADE - DATA.")

    v1, v2 = have[d1], have[d2]
    block = {
        "btcd_provider": "NEWHEDGE",
        "btcd_convention": "CANONICAL_NEWHEDGE_DAILY",
        "btcd_d_minus_1_date": d1, "btcd_d_minus_1": v1,
        "btcd_d_minus_2_date": d2, "btcd_d_minus_2": v2,
        "btcd_direction": "DOWN" if v1 < v2 else "UP_OR_FLAT",
        "btcd_multiplier": 1.15 if v1 < v2 else 1.00,
        "resolved_at_utc": now().isoformat(),
    }
    store["canonical"] = store.get("canonical", {})
    store["canonical"][exec_date.isoformat()] = block
    save_store(store)
    print(f"[newhedge] {exec_date}: D-1 {v1:.2f} vs D-2 {v2:.2f} -> {block['btcd_multiplier']:.2f}x")
    return block


def check():
    """Registry §6: demonstrate operationally that D-1 is retrievable before 16:47 UTC."""
    t = now()
    exec_date = t.date()
    cutoff = datetime.combine(exec_date, datetime.min.time(), timezone.utc).replace(
        hour=CUTOFF_H, minute=CUTOFF_M)
    obs, audit = fetch()
    d1 = (exec_date - timedelta(days=1)).isoformat()
    available = d1 in obs
    print("=== CAUSAL AVAILABILITY TEST ===")
    print(f"  retrieval time        : {t.isoformat()}")
    print(f"  information cutoff    : {cutoff.isoformat()}")
    print(f"  margin before cutoff  : {(cutoff - t).total_seconds()/3600:+.2f} hours")
    print(f"  required D-1          : {d1}")
    print(f"  D-1 present in response: {available}")
    print(f"  latest observation     : {audit['latest_observation_date']}")
    print(f"  response sha256        : {audit['response_sha256'][:16]}...")
    print(f"  VERDICT: {'PASS' if (available and t < cutoff) else 'FAIL'}")
    return 0 if (available and t < cutoff) else 1


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(check())
    try:
        ensure(now().date())
    except Exception as exc:
        # Fail loud: BTC.D is a mandatory input under universe-level fail-closed.
        print(f"[newhedge] CANONICAL FAILURE: {exc}")
        sys.exit(1)
