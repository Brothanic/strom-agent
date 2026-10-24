"""Tests for tools.py against frozen fixtures and small synthetic days. No network."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from strom_agent.prices import DayPrices, PriceClient, PriceServiceUnavailable, day_bounds, parse_day
from strom_agent.tools import ATTRIBUTION, ReportPublisher, Toolbox, cheapest_window, day_stats

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "data" / "fixtures"


def approve_all(tool_name: str, preview: str) -> bool:
    return True


def reject_all(tool_name: str, preview: str) -> bool:
    return False


def fixture_toolbox(tmp_path: Path, fixture_dir: Path = FIXTURE_DIR) -> Toolbox:
    price_client = PriceClient(cache_dir=tmp_path / "cache", fixture_dir=fixture_dir, use_fixtures_only=True)
    return Toolbox(price_client, ReportPublisher(tmp_path / "reports"), sleep=lambda seconds: None)


def hourly_day(day: date, prices: list[float]) -> DayPrices:
    start, _ = day_bounds(day)
    unix_seconds = [int(start.timestamp()) + index * 3600 for index in range(len(prices))]
    raw_json = json.dumps({"unix_seconds": unix_seconds, "price": prices, "unit": "EUR / MWh",
                           "license_info": "CC BY 4.0"})
    return parse_day(day, raw_json)


def call(toolbox: Toolbox, name: str, arguments: dict) -> dict:
    result = toolbox.call(name, arguments, approve_all)
    assert not result.is_error, result.content
    return json.loads(result.content)


# --- Calculations --------------------------------------------------------------


def test_day_stats_on_known_values() -> None:
    prices = [30.0, -10.0, -5.0, 80.0] + [20.0] * 20
    stats = day_stats(hourly_day(date(2025, 10, 2), prices))

    assert stats["hours"] == 24
    assert stats["min_price"] == -10.0
    assert stats["min_hour_start"] == "2025-10-02T01:00+02:00"
    assert stats["max_price"] == 80.0
    assert stats["max_hour_start"] == "2025-10-02T03:00+02:00"
    assert stats["mean_price"] == pytest.approx((30 - 10 - 5 + 80 + 20 * 20) / 24, abs=0.005)
    assert stats["negative_price_hours"] == 2


def test_cheapest_window_on_known_values() -> None:
    prices = [50.0] * 10 + [10.0, 0.0, 20.0, 5.0] + [50.0] * 10
    window = cheapest_window(hourly_day(date(2025, 10, 2), prices), 3)

    assert window["start"] == "2025-10-02T11:00+02:00"
    assert window["end"] == "2025-10-02T14:00+02:00"
    assert window["average_price"] == pytest.approx(25 / 3, abs=0.005)


def test_cheapest_window_prefers_the_earliest_tie() -> None:
    window = cheapest_window(hourly_day(date(2025, 10, 2), [7.0] * 24), 2)
    assert window["start"] == "2025-10-02T00:00+02:00"


def test_cheapest_window_can_span_the_whole_day() -> None:
    day_prices = hourly_day(date(2025, 10, 2), [float(hour) for hour in range(24)])
    window = cheapest_window(day_prices, 24)

    assert window["start"] == "2025-10-02T00:00+02:00"
    assert window["end"] == "2025-10-03T00:00+02:00"
    assert window["average_price"] == 11.5


# --- Read tools against fixtures --------------------------------------------------


def test_get_prices_aggregates_quarter_hours(tmp_path: Path) -> None:
    output = call(fixture_toolbox(tmp_path), "get_prices", {"date": "2025-10-01"})

    assert output["source_resolution_minutes"] == 15
    assert len(output["hours"]) == 24
    # Mean of 102.6, 92.24, 86.03 and 85.39 from the fixture.
    assert output["hours"][0] == {"start": "2025-10-01T00:00+02:00", "price": 91.56}


def test_get_prices_on_autumn_dst_day_shows_both_two_oclock_hours(tmp_path: Path) -> None:
    output = call(fixture_toolbox(tmp_path), "get_prices", {"date": "2025-10-26"})
    starts = [hour["start"] for hour in output["hours"]]

    assert len(starts) == 25
    assert "2025-10-26T02:00+02:00" in starts
    assert "2025-10-26T02:00+01:00" in starts


def test_cheapest_window_end_is_correct_across_spring_dst_gap(tmp_path: Path) -> None:
    output = call(fixture_toolbox(tmp_path), "cheapest_window", {"date": "2026-03-29", "hours": 23})

    assert output["start"] == "2026-03-29T00:00+01:00"
    assert output["end"] == "2026-03-30T00:00+02:00"


def test_window_longer_than_dst_day_is_an_error(tmp_path: Path) -> None:
    result = fixture_toolbox(tmp_path).call("cheapest_window", {"date": "2026-03-29", "hours": 24},
                                            approve_all)
    assert result.is_error
    assert "only 23 hours" in result.content


def test_price_stats_reports_missing_days_next_to_found_ones(tmp_path: Path) -> None:
    output = call(fixture_toolbox(tmp_path), "price_stats",
                  {"start_date": "2025-10-01", "end_date": "2025-10-03"})

    assert [day["date"] for day in output["days"]] == ["2025-10-01"]
    assert [day["date"] for day in output["missing_days"]] == ["2025-10-02", "2025-10-03"]


def test_negative_prices_are_counted_on_easter_sunday(tmp_path: Path) -> None:
    output = call(fixture_toolbox(tmp_path), "price_stats",
                  {"start_date": "2026-04-05", "end_date": "2026-04-05"})
    stats = output["days"][0]

    assert stats["negative_price_hours"] > 0
    assert stats["min_price"] < 0


# --- Structured errors ------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "arguments", "expected_text"),
    [
        ("get_prices", {"date": "yesterday"}, "date"),
        ("get_prices", {}, "date: Field required"),
        ("cheapest_window", {"date": "2025-10-01", "hours": 0}, "hours"),
        ("price_stats", {"start_date": "2025-10-08", "end_date": "2025-10-01"}, "before start_date"),
        ("price_stats", {"start_date": "2025-01-01", "end_date": "2025-03-01"}, "at most 31 days"),
        ("get_prices", {"date": "2030-01-01"}, "No price data"),
        ("delete_everything", {}, "Unknown tool"),
    ],
)
def test_bad_calls_return_error_results_instead_of_raising(
    tmp_path: Path, name: str, arguments: dict, expected_text: str
) -> None:
    result = fixture_toolbox(tmp_path).call(name, arguments, approve_all)

    assert result.is_error
    assert expected_text in result.content


class FlakyPriceClient:
    """Fails with a temporary error a given number of times, then succeeds."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def get_day(self, day: date) -> DayPrices:
        self.calls += 1
        if self.calls <= self.failures:
            raise PriceServiceUnavailable("The price API is temporarily unavailable (HTTP 503).")
        return hourly_day(day, [10.0] * 24)


def test_temporary_failures_are_retried_with_backoff(tmp_path: Path) -> None:
    sleeps: list[float] = []
    toolbox = Toolbox(FlakyPriceClient(failures=2), ReportPublisher(tmp_path), sleep=sleeps.append)

    result = toolbox.call("get_prices", {"date": "2025-10-02"}, approve_all)

    assert not result.is_error
    assert sleeps == [1.0, 2.0]


def test_retries_stop_after_two(tmp_path: Path) -> None:
    flaky = FlakyPriceClient(failures=3)
    toolbox = Toolbox(flaky, ReportPublisher(tmp_path), sleep=lambda seconds: None)

    result = toolbox.call("get_prices", {"date": "2025-10-02"}, approve_all)

    assert result.is_error
    assert "temporarily unavailable" in result.content
    assert flaky.calls == 3


# --- publish_report ------------------------------------------------------------


def test_only_publish_report_requires_approval(tmp_path: Path) -> None:
    toolbox = fixture_toolbox(tmp_path)
    needing_approval = [tool.name for tool in toolbox.tools.values() if tool.requires_approval]

    assert needing_approval == ["publish_report"]
    assert [schema["name"] for schema in toolbox.schemas()] == list(toolbox.tools)


def test_rejected_report_is_not_written(tmp_path: Path) -> None:
    toolbox = fixture_toolbox(tmp_path)
    result = toolbox.call("publish_report", {"title": "Daily", "body": "Prices were low."}, reject_all)

    assert result.is_error
    assert result.approval == "rejected"
    assert "Rejected by human" in result.content
    assert not (tmp_path / "reports").exists()


def test_approved_report_is_written_exactly_as_previewed(tmp_path: Path) -> None:
    previews: list[str] = []

    def approve_and_remember(tool_name: str, preview: str) -> bool:
        previews.append(preview)
        return True

    toolbox = fixture_toolbox(tmp_path)
    result = toolbox.call("publish_report", {"title": "Daily", "body": "Prices were low."},
                          approve_and_remember)

    assert result.approval == "approved"
    written = Path(json.loads(result.content)["path"]).read_text(encoding="utf-8")
    assert ATTRIBUTION in written
    assert previews[0].endswith(written)


def test_report_file_name_keeps_the_whole_title(tmp_path: Path) -> None:
    # A 40-character cut once turned this title's date into "2026-10-2".
    title = "DE-LU Daily Power Price Report: 2026-10-23"
    result = fixture_toolbox(tmp_path).call("publish_report", {"title": title, "body": "Low."}, approve_all)

    assert Path(json.loads(result.content)["path"]).name.endswith("-de-lu-daily-power-price-report-2026-10-23.md")


def test_report_is_posted_to_slack_without_leaking_the_url(tmp_path: Path) -> None:
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, text="ok")

    webhook_url = "https://hooks.slack.example/secret-path"
    publisher = ReportPublisher(tmp_path, webhook_url, httpx.Client(transport=httpx.MockTransport(handler)))
    toolbox = Toolbox(FlakyPriceClient(failures=0), publisher)

    result = toolbox.call("publish_report", {"title": "Daily", "body": "Low."}, approve_all)

    assert json.loads(result.content) == {"published": True, "destination": "slack"}
    assert ATTRIBUTION in posted[0]["text"]
    assert "secret-path" not in result.content


def test_slack_failure_is_an_error_result(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    publisher = ReportPublisher(tmp_path, "https://hooks.slack.example/x", httpx.Client(transport=transport))
    toolbox = Toolbox(FlakyPriceClient(failures=0), publisher)

    result = toolbox.call("publish_report", {"title": "Daily", "body": "Low."}, approve_all)

    assert result.is_error
    assert "HTTP 403" in result.content
