"""The tools the model may call, with input validation and deterministic math.

Every number the agent reports comes from a function in this file. Tool
failures become structured error results so the model can react to them
instead of the program crashing.
"""

import datetime
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator

from strom_agent.prices import BERLIN, BIDDING_ZONE, DayPrices, PriceClient, PriceDataError, PriceServiceUnavailable

MAX_RETRIES = 2
FIRST_BACKOFF_SECONDS = 1.0
MAX_RANGE_DAYS = 31
ATTRIBUTION = (
    "Data: Bundesnetzagentur | SMARD.de, CC BY 4.0, via Energy-Charts (Fraunhofer ISE). "
    f"Bidding zone {BIDDING_ZONE}, day-ahead prices in EUR/MWh."
)
REJECTED_MESSAGE = "Rejected by human. Nothing was published."

# Receives the tool name and the exact content to approve; returns the decision.
Approver = Callable[[str, str], bool]


class GetPricesInput(BaseModel):
    date: datetime.date = Field(description="Day in YYYY-MM-DD format")


class PriceStatsInput(BaseModel):
    start_date: datetime.date = Field(description="First day, YYYY-MM-DD")
    end_date: datetime.date = Field(description="Last day (inclusive), YYYY-MM-DD")

    @model_validator(mode="after")
    def check_range(self) -> "PriceStatsInput":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        if (self.end_date - self.start_date).days >= MAX_RANGE_DAYS:
            raise ValueError(f"the range may cover at most {MAX_RANGE_DAYS} days")
        return self


class CheapestWindowInput(BaseModel):
    date: datetime.date = Field(description="Day in YYYY-MM-DD format")
    hours: int = Field(ge=1, le=24, description="Window length in whole hours")


class PublishReportInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=5000, description="Report text in Markdown")


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False
    approval: str | None = None  # "approved" or "rejected" for tools that need approval


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    run: Callable[[Any], dict]
    requires_approval: bool = False
    preview: Callable[[Any], str] | None = None


# --- Deterministic calculations --------------------------------------------


def format_time(moment: datetime.datetime) -> str:
    # The UTC offset tells the two 02:00 hours apart on the autumn DST day.
    return moment.isoformat(timespec="minutes")


def round_price(price: float) -> float:
    return round(price, 2)


def day_stats(day_prices: DayPrices) -> dict:
    hourly = day_prices.hourly()
    cheapest = min(hourly, key=lambda point: point.price_eur_per_mwh)
    most_expensive = max(hourly, key=lambda point: point.price_eur_per_mwh)
    prices = [point.price_eur_per_mwh for point in hourly]
    return {
        "date": day_prices.day.isoformat(),
        "hours": len(hourly),
        "min_price": round_price(cheapest.price_eur_per_mwh),
        "min_hour_start": format_time(cheapest.start),
        "max_price": round_price(most_expensive.price_eur_per_mwh),
        "max_hour_start": format_time(most_expensive.start),
        "mean_price": round_price(sum(prices) / len(prices)),
        "negative_price_hours": sum(1 for price in prices if price < 0),
    }


def cheapest_window(day_prices: DayPrices, hours: int) -> dict:
    """Find the contiguous run of `hours` hourly prices with the lowest mean.

    Windows stay within the day. Ties go to the earliest window.
    """
    hourly = day_prices.hourly()
    if hours > len(hourly):
        raise PriceDataError(f"{day_prices.day.isoformat()} has only {len(hourly)} hours.")

    best_start_index = 0
    best_total = None
    for start_index in range(len(hourly) - hours + 1):
        window = hourly[start_index : start_index + hours]
        total = sum(point.price_eur_per_mwh for point in window)
        if best_total is None or total < best_total:
            best_start_index, best_total = start_index, total

    first = hourly[best_start_index]
    last = hourly[best_start_index + hours - 1]
    # Add one hour as a Unix timestamp, because wall-clock arithmetic is wrong on DST days.
    end = datetime.datetime.fromtimestamp(last.start.timestamp() + 3600, BERLIN)
    return {
        "date": day_prices.day.isoformat(),
        "hours": hours,
        "start": format_time(first.start),
        "end": format_time(end),
        "average_price": round_price(best_total / hours),
    }


# --- Publishing ----------------------------------------------------------------


class PublishError(Exception):
    """Publishing failed; the message is safe to show (it never contains the webhook URL)."""


def render_report(title: str, body: str) -> str:
    """The exact text that is shown for approval and then published."""
    return f"# {title}\n\n{body.strip()}\n\n---\n{ATTRIBUTION}\n"


class ReportPublisher:
    def __init__(self, reports_dir: Path, slack_webhook_url: str | None = None,
                 http_client: httpx.Client | None = None) -> None:
        self.reports_dir = reports_dir
        self.slack_webhook_url = slack_webhook_url
        self.http_client = http_client or httpx.Client()

    def destination(self) -> str:
        return "Slack incoming webhook" if self.slack_webhook_url else f"a file in {self.reports_dir}/"

    def publish(self, title: str, body: str) -> dict:
        text = render_report(title, body)
        if self.slack_webhook_url:
            try:
                response = self.http_client.post(self.slack_webhook_url, json={"text": text}, timeout=10.0)
            except httpx.HTTPError as error:
                raise PublishError("Slack could not be reached.") from error
            if response.status_code != 200:
                raise PublishError(f"Slack rejected the report (HTTP {response.status_code}).")
            return {"published": True, "destination": "slack"}

        # Not shortened: the title has at most 120 characters, and a cut could split a date.
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "report"
        stamp = datetime.datetime.now(BERLIN).strftime("%Y%m%d-%H%M%S")
        path = self.reports_dir / f"{stamp}-{slug}.md"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return {"published": True, "destination": "file", "path": str(path)}


# --- The toolbox the agent talks to -----------------------------------------------


class Toolbox:
    def __init__(self, price_client: PriceClient, publisher: ReportPublisher,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.price_client = price_client
        self.publisher = publisher
        self.sleep = sleep
        self.tools = {tool.name: tool for tool in self._define_tools()}

    def _define_tools(self) -> list[Tool]:
        return [
            Tool("get_prices", "Hourly DE-LU day-ahead prices (EUR/MWh) for one day, in Berlin time.",
                 GetPricesInput, self._get_prices),
            Tool("price_stats", "Per-day min, max (with hour), mean and number of negative-price "
                 f"hours for a date range of at most {MAX_RANGE_DAYS} days.",
                 PriceStatsInput, self._price_stats),
            Tool("cheapest_window", "Cheapest contiguous window of whole hours within one day: "
                 "start, end and average price.", CheapestWindowInput, self._cheapest_window),
            Tool("publish_report", "Publish a short report. A human must approve it first. The "
                 "data source credit is appended automatically; do not write one into the body.",
                 PublishReportInput,
                 self._publish_report, requires_approval=True, preview=self._preview_report),
        ]

    def schemas(self) -> list[dict]:
        schemas = []
        for tool in self.tools.values():
            schemas.append({"name": tool.name, "description": tool.description,
                            "input_schema": tool.input_model.model_json_schema()})
        # Marks the end of the tool definitions as cacheable (prompt caching).
        schemas[-1]["cache_control"] = {"type": "ephemeral"}
        return schemas

    def call(self, name: str, raw_input: Any, approve: Approver) -> ToolResult:
        """Validate, ask for approval if the tool needs it, run, and never raise."""
        tool = self.tools.get(name)
        if tool is None:
            return error_result(f"Unknown tool {name!r}.")
        try:
            arguments = tool.input_model.model_validate(raw_input)
        except ValidationError as error:
            return error_result(f"Invalid input: {describe_validation_error(error)}")

        approval = None
        if tool.requires_approval:
            # This check lives in code, so no prompt or model output can skip it.
            if not approve(tool.name, tool.preview(arguments)):
                return ToolResult(REJECTED_MESSAGE, is_error=True, approval="rejected")
            approval = "approved"

        try:
            output = tool.run(arguments)
        except (PriceDataError, PublishError) as error:
            return ToolResult(json.dumps({"error": str(error)}), is_error=True, approval=approval)
        return ToolResult(json.dumps(output), approval=approval)

    def load_day(self, day: datetime.date) -> DayPrices:
        """Load a day, retrying temporary API failures with exponential backoff."""
        retries = 0
        while True:
            try:
                return self.price_client.get_day(day)
            except PriceServiceUnavailable:
                if retries == MAX_RETRIES:
                    raise
                self.sleep(FIRST_BACKOFF_SECONDS * 2**retries)
                retries += 1

    def _get_prices(self, arguments: GetPricesInput) -> dict:
        day_prices = self.load_day(arguments.date)
        hours = []
        for point in day_prices.hourly():
            hours.append({"start": format_time(point.start), "price": round_price(point.price_eur_per_mwh)})
        return {"date": arguments.date.isoformat(), "unit": "EUR/MWh",
                "source_resolution_minutes": day_prices.resolution_minutes, "hours": hours}

    def _price_stats(self, arguments: PriceStatsInput) -> dict:
        days, missing = [], []
        day = arguments.start_date
        while day <= arguments.end_date:
            try:
                days.append(day_stats(self.load_day(day)))
            except PriceDataError as error:
                missing.append({"date": day.isoformat(), "reason": str(error)})
            day += datetime.timedelta(days=1)
        if not days:
            raise PriceDataError("No price data for any day in this range.")
        return {"unit": "EUR/MWh", "days": days, "missing_days": missing}

    def _cheapest_window(self, arguments: CheapestWindowInput) -> dict:
        result = cheapest_window(self.load_day(arguments.date), arguments.hours)
        return {"unit": "EUR/MWh", **result}

    def _preview_report(self, arguments: PublishReportInput) -> str:
        text = render_report(arguments.title, arguments.body)
        return f"Destination: {self.publisher.destination()}\n\n{text}"

    def _publish_report(self, arguments: PublishReportInput) -> dict:
        return self.publisher.publish(arguments.title, arguments.body)


def error_result(message: str) -> ToolResult:
    return ToolResult(json.dumps({"error": message}), is_error=True)


def describe_validation_error(error: ValidationError) -> str:
    problems = []
    for problem in error.errors():
        field = ".".join(str(part) for part in problem["loc"]) or "input"
        problems.append(f"{field}: {problem['msg']}")
    return "; ".join(problems)
