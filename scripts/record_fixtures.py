"""Record the frozen fixture days once from the Energy-Charts API.

Usage: uv run python scripts/record_fixtures.py

Existing fixtures are never overwritten, so evals keep fixed answers.
Each response is validated, stored verbatim and listed in provenance.json.
"""

import json
import time
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from strom_agent.prices import (
    API_URL,
    BIDDING_ZONE,
    PriceServiceUnavailable,
    fetch_day_json,
    file_name_for,
    parse_day,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "data" / "fixtures"
PROVENANCE_PATH = FIXTURE_DIR / "provenance.json"

# Why each day was chosen is documented in data/fixtures/README.md.
FIXTURE_DAYS = [
    date(2025, 9, 30),
    date(2025, 10, 1),
    date(2025, 10, 8),
    date(2025, 10, 26),
    date(2026, 3, 29),
    date(2026, 4, 5),
    date(2026, 10, 14),
]

# The API answers HTTP 429 to quick successive requests.
PAUSE_BETWEEN_REQUESTS_SECONDS = 10
RETRIES_WHEN_UNAVAILABLE = 3


def fetch_with_patience(http_client: httpx.Client, day: date) -> str:
    for attempt in range(RETRIES_WHEN_UNAVAILABLE + 1):
        try:
            return fetch_day_json(http_client, day)
        except PriceServiceUnavailable:
            if attempt == RETRIES_WHEN_UNAVAILABLE:
                raise
            time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS * (attempt + 1))
    raise AssertionError("unreachable")


def load_provenance() -> dict:
    if PROVENANCE_PATH.exists():
        return json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
    return {}


def save_provenance(provenance: dict) -> None:
    PROVENANCE_PATH.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    provenance = load_provenance()

    with httpx.Client() as http_client:
        for day in FIXTURE_DAYS:
            fixture_path = FIXTURE_DIR / file_name_for(day)
            if fixture_path.exists():
                print(f"{day}: fixture exists, skipped")
                continue

            raw_json = fetch_with_patience(http_client, day)
            day_prices = parse_day(day, raw_json)
            fixture_path.write_text(raw_json, encoding="utf-8")

            provenance[fixture_path.name] = {
                "url": f"{API_URL}?bzn={BIDDING_ZONE}&start={day}&end={day}",
                "fetched_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                "resolution_minutes": day_prices.resolution_minutes,
                "value_count": len(day_prices.points),
                "license_info": day_prices.license_info,
            }
            # Saved after every day so an interrupted run leaves no fixture undocumented.
            save_provenance(provenance)
            print(f"{day}: recorded {len(day_prices.points)} values "
                  f"at {day_prices.resolution_minutes}-minute resolution")
            time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)


if __name__ == "__main__":
    main()
