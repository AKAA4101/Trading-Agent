import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))


class Config:
    # Alpaca
    ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
    ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
    ALPACA_BASE_URL: str = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

    # OANDA
    OANDA_API_TOKEN: str = os.getenv("OANDA_API_TOKEN", "")
    OANDA_ACCOUNT_ID: str = os.getenv("OANDA_ACCOUNT_ID", "")
    OANDA_BASE_URL: str = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com")

    # Market Data
    MASSIVE_API_KEY: str = os.getenv("MASSIVE_API_KEY", "")

    # News
    NEWS_API_KEY: str = os.getenv("NEWS_API_KEY", "")

    # AI
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    # Notifications
    SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_EMAIL: str = os.getenv("SMTP_EMAIL", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_TO: str = os.getenv("SMTP_TO", "")

    # Agent
    CONFIDENCE_THRESHOLD: int = int(os.getenv("CONFIDENCE_THRESHOLD", "70"))
    MAX_POSITION_SIZE_PCT: float = float(os.getenv("MAX_POSITION_SIZE_PCT", "20"))
    DAILY_DRAWDOWN_LIMIT_PCT: float = float(os.getenv("DAILY_DRAWDOWN_LIMIT_PCT", "5"))
    PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"

    # Database
    DB_PATH: str = os.path.join(os.path.dirname(__file__), "trading_agent.db")

    # Logging
    LOG_PATH: str = os.path.join(os.path.dirname(__file__), "logs", "agent.log")

    # Timezone
    BRISBANE_TZ: str = "Australia/Brisbane"

    def validate(self) -> list[str]:
        missing = []
        required = [
            ("ALPACA_API_KEY", self.ALPACA_API_KEY),
            ("ALPACA_SECRET_KEY", self.ALPACA_SECRET_KEY),
            ("OANDA_API_TOKEN", self.OANDA_API_TOKEN),
            ("OANDA_ACCOUNT_ID", self.OANDA_ACCOUNT_ID),
            ("NEWS_API_KEY", self.NEWS_API_KEY),
            ("ANTHROPIC_API_KEY", self.ANTHROPIC_API_KEY),
            ("SMTP_EMAIL", self.SMTP_EMAIL),
            ("SMTP_PASSWORD", self.SMTP_PASSWORD),
        ]
        for name, val in required:
            if not val:
                missing.append(name)
        return missing


config = Config()
