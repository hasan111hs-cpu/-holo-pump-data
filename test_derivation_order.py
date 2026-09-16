"""
REGRESSION — dependency-order class (registry §9).

Asserts the general invariant, not the specific provenance case: a derived metric must
observe the authoritative state produced by mutations occurring earlier in the same
workflow. Fails if derivation is moved ahead of any required mutation.

    python test_derivation_order.py
"""
import json, os, shutil, sys, tempfile, importlib.util
from datetime import date
from pathlib import Path


def load(tmp):
    spec = importlib.util.spec_from_file_location("hb_test", tmp / "heartbeat.py")
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def canonical_log(d, pre_switch=False):
    log = {"execution_date": d, "events": [{
        "stage": "SIGNAL_LOCKED", "status": "ok",
        "detail": "artifact durably persisted to origin/main",
        "recorded_at_utc": f"{d}T16:51:00+00:00", "trigger_role": "PRIMARY_EXTERNAL"}],
        "validity_classification": "CANONICAL_AUTOMATIC_ON_TIME",
        "counts_as_prospective_observation": True,
        "canonical_automatic_prospective_observations": 0,
        "current_spec_newhedge_observations": 0}
    if pre_switch:
        log["btcd_provenance"] = {"btcd_provider_regime": "PRE_NEWHEDGE_SWITCH"}
    return log


def run():
    src = Path(__file__).resolve().parent
    tmp = Path(tempfile.mkdtemp())
    shutil.copy(src / "heartbeat.py", tmp / "heartbeat.py")
    cwd = Path.cwd(); os.chdir(tmp)
    (tmp / "operational_logs").mkdir(); (tmp / "signals").mkdir()
    hb = load(tmp)
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {name:52} {detail}")

    print("=== DERIVATION-ORDER REGRESSION ===")

    # A. authoritative input absent: three canonical days, none annotated
    for d in ("2026-09-13", "2026-09-14", "2026-09-15"):
        (tmp / "operational_logs" / f"{d}.json").write_text(json.dumps(canonical_log(d)))
        (tmp / "signals" / f"{d}.json").write_text('{"locked_state":"NO TRADE"}')
    check("A  before mutation: current_spec counts all three",
          hb.current_spec_count() == 3, f"-> {hb.current_spec_count()}")

    # B/C. the same workflow creates the authoritative input and persists it
    p = tmp / "operational_logs" / "2026-09-13.json"
    lg = json.loads(p.read_text())
    lg["btcd_provenance"] = {"btcd_provider_regime": "PRE_NEWHEDGE_SWITCH"}
    p.write_text(json.dumps(lg))
    check("B  mutation persisted", "btcd_provenance" in json.loads(p.read_text()))

    # D/E. derived metric runs afterward and observes the new authoritative state
    canon, spec = hb.derive()
    check("D  derive() after mutation sees new state",
          (canon, spec) == (3, 2), f"-> canonical {canon}, current_spec {spec}")

    stored = json.loads((tmp / "operational_logs" / "2026-09-15.json").read_text())
    check("E  derived values written to every log",
          stored["current_spec_newhedge_observations"] == 2,
          f"-> {stored['current_spec_newhedge_observations']}")

    # F. the failure this guards against: derivation BEFORE the mutation lands
    p2 = tmp / "operational_logs" / "2026-09-14.json"
    lg2 = json.loads(p2.read_text())
    stale_spec = hb.current_spec_count()          # derived first
    lg2["btcd_provenance"] = {"btcd_provider_regime": "PRE_NEWHEDGE_SWITCH"}
    p2.write_text(json.dumps(lg2))                # mutation lands after
    check("F  derive-before-mutation yields a stale value",
          stale_spec != hb.current_spec_count(),
          f"-> stale {stale_spec}, correct {hb.current_spec_count()}")

    os.chdir(cwd); shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n  {sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(run())
