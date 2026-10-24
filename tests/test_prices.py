"""Tests for prices.py. Real fixtures are used where possible; small synthetic
responses cover error cases. No test touches the network."""

import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from strom_agent.prices import (
    BERLIN,
    PriceClient,
    PriceDataError,
    PriceServiceUnavailable,
    day_bounds,
    parse_day,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "data" / "fixtures"


def fixture_client(tmp_path: Path) -> PriceClient:
    return PriceClient(cache_dir=tmp_path, fixture_dir=FIXTURE_DIR, use_fixtures_only=True)


def synthetic_response(day: date, step_seconds: int, prices: list[float | None]) -> str:
    start, _ = day_bounds(day)
    first = int(start.timestamp())
    unix_seconds = [first + index * step_seconds for index in range(len(prices))]
    return json.dumps({
        "unix_seconds": unix_seconds,
        "price": prices,
        "unit": "EUR / MWh",
        "license_info": "CC BY 4.0 from Bundesnetzagentur | SMARD.de",
    })


def mock_http_client(status_code: int, body: str = "") -> tuple[httpx.Client, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, text=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


# --- Resolution and aggregation -------------------------------------------


def test_hourly_fixture_day_is_detected_and_left_unchanged(tmp_path: Path) -> None:
    day_prices = fixture_client(tmp_path).get_day(date(2025, 9, 30))

    assert day_prices.resolution_minutes == 60
    assert len(day_prices.points) == 24
    assert day_prices.hourly() == day_prices.points


def test_quarter_hour_fixture_day_is_aggregated_to_hourly_means(tmp_path: Path) -> None:
    day_prices = fixture_client(tmp_path).get_day(date(2025, 10, 1))
    hourly = day_prices.hourly()

    assert day_prices.resolution_minutes == 15
    assert len(day_prices.points) == 96
    assert len(hourly) == 24
    first_four = [point.price_eur_per_mwh for point in day_prices.points[:4]]
    assert hourly[0].price_eur_per_mwh == pytest.approx(sum(first_four) / 4)
    assert hourly[0].start == datetime(2025, 10, 1, 0, 0, tzinfo=BERLIN)
    assert hourly[23].start == datetime(2025, 10, 1, 23, 0, tzinfo=BERLIN)


def test_aggregation_uses_known_values() -> None:
    day = date(2025, 10, 2)
    prices = [10.0, 20.0, 30.0, 40.0] + [-4.0, 0.0, 4.0, 8.0] + [50.0] * 88
    day_prices = parse_day(day, synthetic_response(day, 900, prices))
    hourly = day_prices.hourly()

    assert hourly[0].price_eur_per_mwh == 25.0
    assert hourly[1].price_eur_per_mwh == 2.0
    assert hourly[2].price_eur_per_mwh == 50.0


def test_all_points_use_berlin_time(tmp_path: Path) -> None:
    day_prices = fixture_client(tmp_path).get_day(date(2026, 4, 5))

    for point in day_prices.points + day_prices.hourly():
        assert point.start.tzinfo == BERLIN
        assert point.start.date() == date(2026, 4, 5)


# --- Daylight saving time -------------------------------------------------


def test_autumn_dst_day_has_25_hours_with_02_00_twice(tmp_path: Path) -> None:
    day_prices = fixture_client(tmp_path).get_day(date(2025, 10, 26))
    hourly = day_prices.hourly()

    assert len(day_prices.points) == 100
    assert len(hourly) == 25
    two_oclock_hours = [point for point in hourly if point.start.hour == 2]
    assert len(two_oclock_hours) == 2
    offsets = [point.start.utcoffset().total_seconds() / 3600 for point in two_oclock_hours]
    assert offsets == [2.0, 1.0]  # first CEST, then CET


def test_spring_dst_day_has_23_hours_without_02_00(tmp_path: Path) -> None:
    day_prices = fixture_client(tmp_path).get_day(date(2026, 3, 29))
    hourly = day_prices.hourly()

    assert len(day_prices.points) == 92
    assert len(hourly) == 23
    local_hours = [point.start.hour for point in hourly]
    assert 2 not in local_hours
    assert local_hours[:3] == [0, 1, 3]


def test_dst_day_missing_its_extra_hour_is_rejected() -> None:
    day = date(2025, 10, 26)
    only_24_hours = [50.0] * 96

    with pytest.raises(PriceDataError, match="Incomplete"):
        parse_day(day, synthetic_response(day, 900, only_24_hours))


# --- Validation -----------------------------------------------------------


def test_incomplete_day_is_rejected() -> None:
    day = date(2025, 10, 2)
    with pytest.raises(PriceDataError, match="Incomplete"):
        parse_day(day, synthetic_response(day, 900, [50.0] * 95))


def test_missing_price_value_is_rejected() -> None:
    day = date(2025, 10, 2)
    prices: list[float | None] = [50.0] * 96
    prices[10] = None
    with pytest.raises(PriceDataError, match="missing"):
        parse_day(day, synthetic_response(day, 900, prices))


def test_irregular_intervals_are_rejected() -> None:
    day = date(2025, 10, 2)
    response = json.loads(synthetic_response(day, 900, [50.0] * 96))
    response["unix_seconds"][5] += 60
    with pytest.raises(PriceDataError, match="Irregular"):
        parse_day(day, json.dumps(response))


def test_malformed_json_is_rejected() -> None:
    with pytest.raises(PriceDataError, match="Malformed"):
        parse_day(date(2025, 10, 2), '{"unix_seconds": "oops"}')


# --- Fixture mode, cache and HTTP errors -----------------------------------


def test_fixture_mode_reports_missing_day_without_network(tmp_path: Path) -> None:
    http_client, requests = mock_http_client(200)
    client = PriceClient(tmp_path, FIXTURE_DIR, use_fixtures_only=True, http_client=http_client)

    with pytest.raises(PriceDataError, match="No price data"):
        client.get_day(date(2024, 1, 1))
    assert requests == []


def test_fetched_day_is_cached_and_read_back_without_network(tmp_path: Path) -> None:
    day = date(2025, 10, 2)
    body = synthetic_response(day, 3600, [42.0] * 24)
    http_client, requests = mock_http_client(200, body)
    client = PriceClient(tmp_path, FIXTURE_DIR, http_client=http_client)

    first = client.get_day(day)
    second = client.get_day(day)

    assert first == second
    assert len(requests) == 1
    assert requests[0].url.params["bzn"] == "DE-LU"
    assert (tmp_path / "DE-LU_2025-10-02.json").read_text() == body


def test_invalid_response_is_not_cached(tmp_path: Path) -> None:
    day = date(2025, 10, 2)
    http_client, _ = mock_http_client(200, synthetic_response(day, 900, [42.0] * 50))
    client = PriceClient(tmp_path, FIXTURE_DIR, http_client=http_client)

    with pytest.raises(PriceDataError):
        client.get_day(day)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("status_code", "error_type", "message"),
    [
        (404, PriceDataError, "No price data"),
        (422, PriceDataError, "rejected"),
        (429, PriceServiceUnavailable, "temporarily unavailable"),
        (503, PriceServiceUnavailable, "temporarily unavailable"),
        (500, PriceDataError, "Unexpected"),
    ],
)
def test_http_errors_become_price_errors(
    tmp_path: Path, status_code: int, error_type: type, message: str
) -> None:
    http_client, _ = mock_http_client(status_code)
    client = PriceClient(tmp_path, FIXTURE_DIR, http_client=http_client)

    with pytest.raises(error_type, match=message):
        client.get_day(date(2025, 10, 2))


def test_timeout_is_temporary_unavailability(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = PriceClient(tmp_path, FIXTURE_DIR, http_client=http_client)

    with pytest.raises(PriceServiceUnavailable, match="in time"):
        client.get_day(date(2025, 10, 2))
