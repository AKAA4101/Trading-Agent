"""
Connectivity test — checks all external dependencies.
Run with: python3 connectivity_test.py
"""
import os
import sys

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

import sqlite3
from config import config

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        ok, detail = fn()
        results.append((name, ok, detail))
    except Exception as exc:
        results.append((name, False, str(exc)))


# ── 1. .env file ──────────────────────────────────────────────────────────

def test_env():
    missing = config.validate()
    if missing:
        return False, f"Missing keys: {missing}"
    return True, f"All required keys present (paper={config.PAPER_TRADING})"

check(".env config", test_env)


# ── 2. Alpaca paper trading ───────────────────────────────────────────────

def test_alpaca():
    from alpaca.trading.client import TradingClient
    client = TradingClient(
        api_key=config.ALPACA_API_KEY,
        secret_key=config.ALPACA_SECRET_KEY,
        paper=True,
    )
    acct = client.get_account()
    return True, f"Account status={acct.status}  equity={acct.equity}"

check("Alpaca paper trading API", test_alpaca)


# ── 3. OANDA practice ────────────────────────────────────────────────────

def test_oanda():
    import oandapyV20
    import oandapyV20.endpoints.accounts as accounts_ep
    client = oandapyV20.API(access_token=config.OANDA_API_TOKEN, environment="practice")
    req = accounts_ep.AccountSummary(accountID=config.OANDA_ACCOUNT_ID)
    client.request(req)
    acct = req.response.get("account", {})
    bal = acct.get("balance", "?")
    return True, f"Account balance={bal}  currency={acct.get('currency', '?')}"

check("OANDA practice API", test_oanda)


# ── 4. NewsAPI ────────────────────────────────────────────────────────────

def test_newsapi():
    import requests
    url = "https://newsapi.org/v2/everything"
    params = {"q": "forex", "pageSize": 1, "apiKey": config.NEWS_API_KEY}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    total = data.get("totalResults", 0)
    return True, f"totalResults={total}"

check("NewsAPI", test_newsapi)


# ── 5. Anthropic API ─────────────────────────────────────────────────────

def test_anthropic():
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        messages=[{"role": "user", "content": "Reply with OK"}],
    )
    reply = msg.content[0].text.strip()
    return True, f"Response: {reply!r}"

check("Anthropic API", test_anthropic)


# ── 6. Massive (global equities) API ─────────────────────────────────────

def test_massive():
    from data.collectors.massive_collector import check_connectivity
    ok = check_connectivity()
    return ok, "API key valid, AAPL data returned" if ok else "API returned non-200"

check("Massive (global equities) API", test_massive)


# ── 7. yfinance — international equity tickers ────────────────────────────

def test_yfinance():
    from data.collectors.yfinance_collector import check_connectivity
    symbols = ["BHP.AX", "SHEL.L", "SAP.DE", "7203.T", "0700.HK"]
    results = check_connectivity(symbols)
    passed  = [s for s, ok in results.items() if ok]
    failed  = [s for s, ok in results.items() if not ok]
    ok = len(failed) == 0
    detail = f"OK: {passed}" + (f" | FAIL: {failed}" if failed else "")
    return ok, detail

check("yfinance international equities", test_yfinance)


# ── 8. Alpaca — US-listed ADR tickers ────────────────────────────────────

def test_alpaca_adrs():
    from data.collectors.alpaca_collector import get_equity_bars
    symbols = ["SHEL", "BHP", "TM", "BABA"]
    passed, failed = [], []
    for sym in symbols:
        try:
            df = get_equity_bars(sym, lookback_days=5)
            (passed if not df.empty else failed).append(sym)
        except Exception:
            failed.append(sym)
    ok = len(failed) == 0
    detail = f"OK: {passed}" + (f" | FAIL: {failed}" if failed else "")
    return ok, detail

check("Alpaca ADR tickers (SHEL, BHP, TM, BABA)", test_alpaca_adrs)


# ── 9. SQLite database schema ─────────────────────────────────────────────

def test_database():
    from database.db_manager import DBManager
    db = DBManager()
    conn = sqlite3.connect(config.DB_PATH)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    conn.close()
    expected = {"signals", "trades", "portfolio_snapshots"}
    missing = expected - tables
    if missing:
        return False, f"Missing tables: {missing}"
    return True, f"Tables present: {tables}"

check("SQLite database schema", test_database)


# ── Print results ─────────────────────────────────────────────────────────

print()
print("=" * 60)
print("  TradingAgent — Connectivity Test")
print("=" * 60)
for name, ok, detail in results:
    status = PASS if ok else FAIL
    print(f"  [{status}] {name}")
    print(f"         {detail}")
print("=" * 60)

failed = [n for n, ok, _ in results if not ok]
if failed:
    print(f"\n  ⚠  {len(failed)} check(s) failed: {failed}")
    sys.exit(1)
else:
    print(f"\n  ✅  All {len(results)} checks passed.")
    sys.exit(0)
