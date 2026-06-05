"""
TradingAgent — main entry point.
Loads config, initialises DB, starts the APScheduler, handles shutdown.
"""
import logging
import logging.handlers
import os
import signal
import sys
import time

from config import config
from database.db_manager import DBManager
from scheduler import build_scheduler

# ── Logging setup ──────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(config.LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            config.LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5
        ),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("=" * 60)
    logger.info("TradingAgent starting up")
    logger.info("Paper trading : %s", config.PAPER_TRADING)
    logger.info("Confidence threshold : %d%%", config.CONFIDENCE_THRESHOLD)
    logger.info("Max position size : %.0f%%", config.MAX_POSITION_SIZE_PCT)
    logger.info("Daily drawdown limit : %.1f%%", config.DAILY_DRAWDOWN_LIMIT_PCT)

    # Validate config
    missing = config.validate()
    if missing:
        logger.error("Missing required config keys: %s", missing)
        sys.exit(1)

    # Initialise database
    db = DBManager()
    logger.info("Database initialised: %s", config.DB_PATH)

    # Build and start scheduler
    scheduler = build_scheduler(db)
    scheduler.start()
    logger.info("Scheduler started — analysis cycle every 4 hours")
    logger.info("=" * 60)

    # Graceful shutdown handlers
    def _shutdown(signum, frame):
        logger.info("Shutdown signal received (%s) — stopping scheduler", signum)
        scheduler.shutdown(wait=False)
        logger.info("TradingAgent stopped.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Keep alive
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
