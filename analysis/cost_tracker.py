"""
Thread-safe API cost tracker for the current process day.
Resets at UTC midnight. Lost on process restart (acceptable for a continuous daemon).

Usage:
    from analysis.cost_tracker import record_call, get_stats
    record_call("claude")    # after each Anthropic call
    record_call("newsapi")   # after each NewsAPI call
"""
import threading
from collections import defaultdict
from datetime import datetime, timezone

# Cost estimates
CLAUDE_COST_PER_CALL = 0.003   # USD, claude-sonnet-4-6 approximate
NEWSAPI_COST_PER_CALL = 0.0    # free tier

_lock = threading.Lock()
_counts: dict[str, int] = defaultdict(int)
_current_day: str | None = None


def _reset_if_new_day() -> None:
    global _current_day
    today = datetime.now(timezone.utc).date().isoformat()
    if _current_day != today:
        _current_day = today
        _counts.clear()


def record_call(call_type: str, count: int = 1) -> None:
    """Record one (or more) API calls of the given type."""
    with _lock:
        _reset_if_new_day()
        _counts[call_type] += count


def get_stats() -> dict:
    """Return today's call counts and estimated USD cost."""
    with _lock:
        _reset_if_new_day()
        claude_calls  = _counts.get("claude", 0)
        newsapi_calls = _counts.get("newsapi", 0)
        return {
            "claude_calls":    claude_calls,
            "newsapi_calls":   newsapi_calls,
            "claude_cost_usd": round(claude_calls * CLAUDE_COST_PER_CALL, 3),
        }
