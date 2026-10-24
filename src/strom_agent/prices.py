"""Day-ahead prices for the DE-LU bidding zone from the Energy-Charts API.

Only DE-LU is supported because other bidding zones come with different
license terms. Responses are stored verbatim as JSON so a cached or recorded
day can always be traced back to exactly what the API returned.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ValidationError

API_URL = "https://api.energy-charts.info/price"
BIDDING_ZONE = "DE-LU"
BERLIN = ZoneInfo("Europe/Berlin")
EXPECTED_UNIT = "EUR / MWh"
REQUEST_TIMEOUT_SECONDS = 10.0


class PriceDataError(Exception):
    """Prices for a day cannot be provided; the message is safe to show a user."""


class PriceServiceUnavailable(PriceDataError):
    """A temporary failure (rate limit, 503, timeout) that may succeed on retry."""


class ApiPriceResponse(BaseModel):
    """The part of the Energy-Charts response we rely on. Extra fields are ignored."""

    unix_seconds: list[int]
    price: list[float | None]
    unit: str
    license_info: str


@dataclass(frozen=True)
class PricePoint:
    start: datetime  # timezone-aware, Europe/Berlin
    price_eur_per_mwh: float


@dataclass(frozen=True)
class DayPrices:
    day: date
    resolution_minutes: int
    points: list[PricePoint]
    license_info: str

    def hourly(self) -> list[PricePoint]:
        """Return one point per hour, averaging sub-hourly values.

        A normal day has 24 hours, the spring DST day 23 and the autumn DST
        day 25. Grouping by UTC hour keeps the repeated 02:00 hour in October
        as two separate hours, because the Berlin offset is a whole number of
        hours and Berlin hours therefore line up with UTC hours.
        """
        if self.resolution_minutes == 60:
            return list(self.points)

        prices_by_hour: dict[int, list[float]] = {}
        for point in self.points:
            unix_hour = int(point.start.timestamp()) // 3600 * 3600
            prices_by_hour.setdefault(unix_hour, []).append(point.price_eur_per_mwh)

        hourly_points = []
        for unix_hour, prices in prices_by_hour.items():
            start = datetime.fromtimestamp(unix_hour, BERLIN)
            mean_price = sum(prices) / len(prices)
            hourly_points.append(PricePoint(start, mean_price))
        return hourly_points


def file_name_for(day: date) -> str:
    return f"{BIDDING_ZONE}_{day.isoformat()}.json"


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Return local midnight at the start and end of a Berlin calendar day."""
    start = datetime(day.year, day.month, day.day, tzinfo=BERLIN)
    following_day = day + timedelta(days=1)
    end = datetime(following_day.year, following_day.month, following_day.day, tzinfo=BERLIN)
    return start, end


def fetch_day_json(http_client: httpx.Client, day: date) -> str:
    """Download one Berlin calendar day and return the response body unchanged."""
    params = {"bzn": BIDDING_ZONE, "start": day.isoformat(), "end": day.isoformat()}
    try:
        response = http_client.get(API_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    except httpx.TimeoutException as error:
        raise PriceServiceUnavailable("The price API did not respond in time.") from error
    except httpx.TransportError as error:
        raise PriceServiceUnavailable("The price API could not be reached.") from error

    if response.status_code == 200:
        return response.text
    if response.status_code == 404:
        raise PriceDataError(f"No price data for {BIDDING_ZONE} on {day.isoformat()}.")
    if response.status_code == 422:
        raise PriceDataError(f"The price API rejected the request for {day.isoformat()}.")
    if response.status_code in (429, 503):
        raise PriceServiceUnavailable(
            f"The price API is temporarily unavailable (HTTP {response.status_code})."
        )
    raise PriceDataError(f"Unexpected response from the price API (HTTP {response.status_code}).")


def parse_day(day: date, raw_json: str) -> DayPrices:
    """Validate a raw API response for one day and convert it to Berlin time.

    The resolution is detected from the timestamps instead of being assumed,
    because the market switched from hourly to 15-minute products.
    """
    try:
        response = ApiPriceResponse.model_validate_json(raw_json)
    except ValidationError as error:
        raise PriceDataError(f"Malformed price data for {day.isoformat()}.") from error

    if response.unit != EXPECTED_UNIT:
        raise PriceDataError(f"Unexpected price unit {response.unit!r}.")
    if len(response.unix_seconds) != len(response.price):
        raise PriceDataError(f"Timestamps and prices do not match for {day.isoformat()}.")
    if len(response.unix_seconds) < 2:
        raise PriceDataError(f"Too few price values for {day.isoformat()}.")

    resolution_seconds = detect_resolution_seconds(day, response.unix_seconds)
    check_complete_day(day, response.unix_seconds, resolution_seconds)

    points = []
    for unix_second, price in zip(response.unix_seconds, response.price):
        if price is None:
            raise PriceDataError(f"Some prices are missing for {day.isoformat()}.")
        start = datetime.fromtimestamp(unix_second, BERLIN)
        points.append(PricePoint(start, price))

    return DayPrices(
        day=day,
        resolution_minutes=resolution_seconds // 60,
        points=points,
        license_info=response.license_info,
    )


def detect_resolution_seconds(day: date, unix_seconds: list[int]) -> int:
    """Return the common step between timestamps; it must divide one hour."""
    resolution = unix_seconds[1] - unix_seconds[0]
    for previous, current in zip(unix_seconds, unix_seconds[1:]):
        if current - previous != resolution:
            raise PriceDataError(f"Irregular price intervals for {day.isoformat()}.")
    if resolution <= 0 or 3600 % resolution != 0:
        raise PriceDataError(f"Unsupported price resolution for {day.isoformat()}.")
    return resolution


def check_complete_day(day: date, unix_seconds: list[int], resolution_seconds: int) -> None:
    """Require exactly one value per interval from local midnight to midnight.

    The day length comes from the Berlin calendar, so DST days are 23 or 25
    hours long here without any special case.
    """
    start, end = day_bounds(day)
    # Not `end - start`: Python subtracts datetimes sharing one tzinfo as wall
    # clock time and would report 24 hours for both DST days.
    day_length_seconds = int(end.timestamp()) - int(start.timestamp())
    expected_count = day_length_seconds // resolution_seconds
    if unix_seconds[0] != int(start.timestamp()) or len(unix_seconds) != expected_count:
        raise PriceDataError(f"Incomplete price data for {day.isoformat()}.")


class PriceClient:
    """Load DE-LU prices for a day from fixtures only, or from cache then API.

    Fixture mode never touches the network, which keeps tests and evals
    reproducible and free of rate limits.
    """

    def __init__(
        self,
        cache_dir: Path,
        fixture_dir: Path,
        use_fixtures_only: bool = False,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.fixture_dir = fixture_dir
        self.use_fixtures_only = use_fixtures_only
        self.http_client = http_client or httpx.Client()

    def get_day(self, day: date) -> DayPrices:
        if self.use_fixtures_only:
            return self._load_fixture(day)
        return self._load_cached_or_fetch(day)

    def _load_fixture(self, day: date) -> DayPrices:
        fixture_path = self.fixture_dir / file_name_for(day)
        if not fixture_path.exists():
            raise PriceDataError(f"No price data for {BIDDING_ZONE} on {day.isoformat()}.")
        return parse_day(day, fixture_path.read_text(encoding="utf-8"))

    def _load_cached_or_fetch(self, day: date) -> DayPrices:
        cache_path = self.cache_dir / file_name_for(day)
        if cache_path.exists():
            return parse_day(day, cache_path.read_text(encoding="utf-8"))

        raw_json = fetch_day_json(self.http_client, day)
        # Parse before writing so that only complete, valid days are cached.
        day_prices = parse_day(day, raw_json)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(raw_json, encoding="utf-8")
        return day_prices
