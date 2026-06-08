"""
One-shot script: run liquidity filter across the entire watchlist.
Prints pass/fail counts, markets most affected, and top 10 failures.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import time
import yfinance as yf
from collections import defaultdict, Counter
from data.watchlist import get_active

MIN_AVG_VOLUME = 500_000
MIN_MARKET_CAP = 500_000_000
CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "liquidity_cache.json")
CACHE_TTL  = 7 * 24 * 3600

def load_cache():
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_cache(cache):
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)

def check(symbol, market_type, cache, now):
    if market_type in ("forex", "crypto"):
        return True, 0, 0, "EXEMPT"

    entry = cache.get(symbol)
    if entry and (now - entry.get("timestamp", 0)) < CACHE_TTL:
        passes = entry["passes"]
        avg_vol = entry.get("avg_vol", 0)
        mkt_cap = entry.get("mkt_cap", 0)
        return passes, avg_vol, mkt_cap, entry["reason"]

    try:
        info = yf.Ticker(symbol).fast_info
        avg_vol = getattr(info, "three_month_average_volume", None) or 0
        mkt_cap = getattr(info, "market_cap", None) or 0
    except Exception:
        avg_vol, mkt_cap = 0, 0

    if avg_vol >= MIN_AVG_VOLUME:
        passes = True
        reason = f"PASS: avgVol {avg_vol:,.0f}"
    elif avg_vol == 0 and mkt_cap >= MIN_MARKET_CAP:
        passes = True
        reason = f"PASS: marketCap ${mkt_cap/1e9:.1f}B (vol unavailable)"
    elif avg_vol > 0:
        passes = False
        reason = f"FAIL: avgVol {avg_vol:,.0f} < {MIN_AVG_VOLUME:,}"
    else:
        passes = False
        reason = f"FAIL: no vol data, mktCap ${mkt_cap/1e6:.0f}M < $500M"

    cache[symbol] = {"passes": passes, "reason": reason, "avg_vol": avg_vol, "mkt_cap": mkt_cap, "timestamp": now}
    return passes, avg_vol, mkt_cap, reason


def main():
    instruments = get_active()
    cache = load_cache()
    now = time.time()

    passed = []
    failed = []
    exempt = []
    market_fails = defaultdict(list)

    print(f"\nScanning {len(instruments)} instruments for liquidity...\n")

    for inst in instruments:
        ok, avg_vol, mkt_cap, reason = check(inst.symbol, inst.market_type, cache, now)
        exchange = inst.exchange or inst.market_type

        if inst.market_type in ("forex", "crypto"):
            exempt.append(inst.symbol)
        elif ok:
            passed.append((inst.symbol, avg_vol, exchange))
        else:
            failed.append((inst.symbol, avg_vol, mkt_cap, exchange, reason))
            market_fails[exchange].append(inst.symbol)

        print(f"  {'✓' if ok else '✗'} {inst.symbol:<20s} {reason}")

    save_cache(cache)

    print(f"\n{'='*60}")
    print(f"LIQUIDITY FILTER RESULTS")
    print(f"{'='*60}")
    print(f"Total instruments  : {len(instruments)}")
    print(f"  Exempt (fx/crypto): {len(exempt)}")
    print(f"  PASS              : {len(passed)}")
    print(f"  FAIL              : {len(failed)}")
    print(f"\nPass rate (equity only): {len(passed)}/{len(passed)+len(failed)} = "
          f"{len(passed)/(len(passed)+len(failed))*100:.1f}%" if (passed or failed) else "")

    print(f"\n── MARKETS MOST AFFECTED (most failures) ─────────────")
    for mkt, syms in sorted(market_fails.items(), key=lambda x: -len(x[1])):
        print(f"  {mkt:<20s}: {len(syms):3d} filtered  {', '.join(syms[:5])}{'...' if len(syms)>5 else ''}")

    print(f"\n── TOP 10 FAILURES (by instrument) ───────────────────")
    top10 = sorted(failed, key=lambda x: x[1])[:10]  # lowest volume first
    for sym, vol, cap, exch, reason in top10:
        vol_str = f"{vol:>12,.0f}" if vol else "   unavailable"
        print(f"  {sym:<20s} avgVol: {vol_str}  [{exch}]")

    # Emit JSON for the weekly email report
    stats = {
        "pass": len(passed),
        "fail": len(failed),
        "exempt": len(exempt),
        "top_fail_markets": {k: len(v) for k, v in market_fails.items()},
        "top10_failed": [(s, v) for s, v, *_ in top10],
    }
    print(f"\nJSON stats for weekly report:")
    print(json.dumps(stats, indent=2))

if __name__ == "__main__":
    main()
