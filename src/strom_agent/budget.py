"""Token and cost accounting, spending limits and the cost ledger.

Before every model call the agent asks `check_before_call` whether the call
could push spending over a limit. The estimate is an upper bound: the input
tokens of the request are counted exactly beforehand and priced at the most
expensive input rate, and the output cannot exceed `max_tokens`.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from strom_agent.config import MODEL_PRICES_USD, ModelPrice, Settings
from strom_agent.prices import BERLIN

PER_MILLION = 1_000_000


class BudgetExceeded(Exception):
    """A limit would be exceeded; the message is shown to the user."""


class MissingPriceConfiguration(Exception):
    """No price or exchange rate is configured, so no paid call may start."""


@dataclass
class RunUsage:
    api_calls: int = 0
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_write_tokens + self.cache_read_tokens


def call_cost_usd(price: ModelPrice, input_tokens: int, output_tokens: int,
                  cache_write_tokens: int, cache_read_tokens: int) -> float:
    total = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_write_tokens * price.cache_write
        + cache_read_tokens * price.cache_read
    )
    return total / PER_MILLION


def read_ledger(ledger_path: Path) -> list[dict]:
    if not ledger_path.exists():
        return []
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class Budget:
    def __init__(self, model: str, settings: Settings, ledger_path: Path) -> None:
        if model not in MODEL_PRICES_USD:
            raise MissingPriceConfiguration(
                f"No price for {model!r} in MODEL_PRICES_USD (config.py). Fill it in from the "
                "Anthropic pricing page before making paid calls."
            )
        if settings.usd_to_eur is None:
            raise MissingPriceConfiguration("Set STROM_USD_TO_EUR to convert USD prices to euros.")
        self.model = model
        self.price = MODEL_PRICES_USD[model]
        self.usd_to_eur = settings.usd_to_eur
        self.settings = settings
        self.ledger_path = ledger_path
        self.spent_before_run_eur = sum(entry["cost_eur"] for entry in read_ledger(ledger_path))
        self.usage = RunUsage()

    def cost_eur(self) -> float:
        return self.usage.cost_usd * self.usd_to_eur

    def check_before_call(self, input_tokens: int, max_tokens: int) -> None:
        # Any input token could turn out to be a cache write, the dearest input rate.
        worst_input_rate = max(self.price.input, self.price.cache_write)
        worst_usd = (input_tokens * worst_input_rate + max_tokens * self.price.output) / PER_MILLION
        worst_eur = worst_usd * self.usd_to_eur

        run_after_call = self.cost_eur() + worst_eur
        if run_after_call > self.settings.max_cost_per_run_eur:
            raise BudgetExceeded(
                f"Stopped: the next model call could cost up to €{worst_eur:.4f}, which could exceed "
                f"the per-run limit of €{self.settings.max_cost_per_run_eur:.2f} "
                f"(this run has spent €{self.cost_eur():.4f})."
            )
        if self.spent_before_run_eur + run_after_call > self.settings.max_cost_total_eur:
            raise BudgetExceeded(
                f"Stopped: the total limit of €{self.settings.max_cost_total_eur:.2f} could be exceeded "
                f"(ledger total €{self.spent_before_run_eur:.4f})."
            )

    def record(self, usage: Any) -> float:
        """Add one API response's usage and return that call's cost in euros."""
        cache_write = usage.cache_creation_input_tokens or 0
        cache_read = usage.cache_read_input_tokens or 0
        cost_usd = call_cost_usd(self.price, usage.input_tokens, usage.output_tokens, cache_write, cache_read)

        self.usage.api_calls += 1
        self.usage.input_tokens += usage.input_tokens
        self.usage.output_tokens += usage.output_tokens
        self.usage.cache_write_tokens += cache_write
        self.usage.cache_read_tokens += cache_read
        self.usage.cost_usd += cost_usd
        return cost_usd * self.usd_to_eur

    def write_ledger(self, question: str, kind: str) -> None:
        entry = {
            "time": datetime.now(BERLIN).isoformat(),
            "kind": kind,
            "model": self.model,
            # A hash identifies repeated questions without storing their text.
            "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest()[:16],
            **asdict(self.usage),
            "usd_to_eur": self.usd_to_eur,
            "cost_eur": self.cost_eur(),
        }
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry) + "\n")
