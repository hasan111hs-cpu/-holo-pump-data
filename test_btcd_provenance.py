"""
REGRESSION SUITE — canonical BTC.D provenance.

Registry §2: the CoinGecko poison test must be retained permanently. These tests assert
the provenance guarantees only; they touch no strategy mathematics and are safe to run
at any time. Uses a temporary directory, never the live data/ tree.

    python test_btcd_provenance.py
"""
import json, sys, shutil, tempfile, importlib.util
from datetime import date, timedelta
from pathlib import Path

ED = date(2026, 9, 13)
D1, D2 = (ED - timedelta(days=1)).isoformat(), (ED - timedelta(days=2)).isoformat()


def load_signal(tmp):
    spec = importlib.util.spec_from_file_location("sg_test", tmp / "signal.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


def write_store(tmp, obs, provider="Newhedge"):
    (tmp / "data").mkdir(exist_ok=True)
    (tmp / "data" / "BTCD-NEWHEDGE.json").write_text(json.dumps({
        "provider": provider, "metric": "btc_dominance",
        "resolution": "daily", "observations": obs}))


def multiplier(sg):
    dom = sg.btcd_daily()
    a, b = dom[ED - timedelta(days=1)], dom[ED - timedelta(days=2)]
    return 1.15 if a < b else 1.00


def run():
    src = Path(__file__).resolve().parent
    tmp = Path(tempfile.mkdtemp())
    shutil.copy(src / "signal.py", tmp / "signal.py")
    cwd = Path.cwd()
    import os
    os.chdir(tmp)
    sg = load_signal(tmp)
    results = []

    def check(name, fn, expect_raise=False, expect=None):
        try:
            got = fn()
            ok = (not expect_raise) and (expect is None or abs(got - expect) < 1e-9)
            detail = f"-> {got}"
        except Exception as exc:
            ok = expect_raise
            detail = f"-> fail-closed ({type(exc).__name__})"
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {name:44} {detail}")

    print("=== CANONICAL BTC.D PROVENANCE REGRESSION ===")
    write_store(tmp, {D1: 58.76, D2: 59.30})
    check("A  D-1 < D-2 yields 1.15x", lambda: multiplier(sg), expect=1.15)

    write_store(tmp, {D1: 59.30, D2: 58.76})
    check("B  D-1 >= D-2 yields 1.00x", lambda: multiplier(sg), expect=1.00)

    write_store(tmp, {D2: 59.30})
    check("C  missing D-1 fails closed", lambda: multiplier(sg), expect_raise=True)

    write_store(tmp, {D1: 58.76})
    check("D  missing D-2 fails closed", lambda: multiplier(sg), expect_raise=True)

    write_store(tmp, {D1: 58.76, D2: 59.30}, provider="CoinGecko")
    check("E  wrong provider fails closed", lambda: multiplier(sg), expect_raise=True)

    write_store(tmp, {})
    check("E2 empty store fails closed", lambda: multiplier(sg), expect_raise=True)

    # §2 POISON TEST - permanent. CoinGecko values that would invert the multiplier.
    write_store(tmp, {D1: 58.76, D2: 59.30})
    (tmp / "data" / "BTC-D.json").write_text(json.dumps([
        {"source_updated_at_unix": 1789084800, "btc_dominance_pct": 99.0},
        {"source_updated_at_unix": 1789171200, "btc_dominance_pct": 1.0}]))
    check("F  CoinGecko poisoned, canonical unchanged", lambda: multiplier(sg), expect=1.15)

    os.chdir(cwd)
    shutil.rmtree(tmp, ignore_errors=True)
    passed, total = sum(results), len(results)
    print(f"\n  {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
