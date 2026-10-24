"""Settings, file locations and the model price table.

Secrets are read only from environment variables: the Anthropic SDK reads
ANTHROPIC_API_KEY itself, and the Slack webhook URL is kept as a SecretStr
so it is never printed or written to a trace by accident.
"""

import os
from pathlib import Path

from pydantic import BaseModel, SecretStr

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = PROJECT_ROOT / "data" / "fixtures"
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
LEDGER_PATH = PROJECT_ROOT / "data" / "ledger.jsonl"
TRACES_DIR = PROJECT_ROOT / "traces"
REPORTS_DIR = PROJECT_ROOT / "reports"

DEFAULT_MODEL = "claude-haiku-5-5"


class ModelPrice(BaseModel):
    """US dollars per million tokens, as published by Anthropic."""

    input: float
    output: float
    cache_write: float  # 5-minute cache writes
    cache_read: float


# Supplied by the owner from https://www.anthropic.com/pricing on 2026-10-21
# (USD per million tokens). No paid call runs for a model that has no entry
# here, so prices are never guessed. Haiku 5.5 has tiered prices; this is the
# tier for prompts up to 100k tokens, and requests here stay far below that.
MODEL_PRICES_USD: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(input=0.10, output=0.50, cache_write=0.125, cache_read=0.01),
    "claude-sonnet-5-5": ModelPrice(input=2.00, output=10.00, cache_write=2.50, cache_read=0.10),
}


class Settings(BaseModel):
    max_cost_per_run_eur: float = 0.05
    max_cost_total_eur: float = 5.0
    # Anthropic bills in USD. The owner sets the rate used to convert to EUR.
    usd_to_eur: float | None = None
    slack_webhook_url: SecretStr | None = None

    @classmethod
    def from_environment(cls) -> "Settings":
        values: dict[str, str] = {}
        names = {
            "max_cost_per_run_eur": "STROM_MAX_COST_PER_RUN_EUR",
            "max_cost_total_eur": "STROM_MAX_COST_TOTAL_EUR",
            "usd_to_eur": "STROM_USD_TO_EUR",
            "slack_webhook_url": "SLACK_WEBHOOK_URL",
        }
        for field, variable in names.items():
            if os.environ.get(variable):
                values[field] = os.environ[variable]
        return cls.model_validate(values)
