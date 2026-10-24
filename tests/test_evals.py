"""Tests for the deterministic eval checks. No model is called."""

from collections import Counter
from pathlib import Path

import pytest

from evals import run_evals
from evals.run_evals import check_case, clock_times_in, load_cases, mentions_number, numbers_in
from strom_agent import config
from strom_agent.agent import ToolCallRecord
from strom_agent.budget import read_ledger
from strom_agent.config import Settings
from tests.test_agent import MODEL, TEST_PRICE, FakeClient, final_answer, tool_use

CASES = {case["id"]: case for case in load_cases()}


def rejected_publish() -> list[ToolCallRecord]:
    return [ToolCallRecord("publish_report", {"title": "t", "body": "b"}, is_error=True, approval="rejected")]


def test_case_mix_matches_the_spec() -> None:
    counts = Counter(case["category"] for case in CASES.values())
    assert len(CASES) == 15
    assert counts == {"fact": 6, "window": 3, "comparison": 2, "unanswerable": 2, "action": 2}


def test_every_case_with_fixture_data_can_compute_its_expected_value() -> None:
    for case in CASES.values():
        if case["category"] in ("fact", "window", "comparison"):
            check_case(case, "", [])  # raises if a fixture or stat is missing


def test_numbers_are_read_with_signs_and_separators() -> None:
    assert numbers_in("−105.17 €/MWh, minus 2.1 and 1,234.5") == [-105.17, -2.1, 1234.5]


@pytest.mark.parametrize(("text", "numbers"), [
    ("On 2025-10-26 at 13:00 it was 6.52 EUR/MWh.", [6.52]),
    ("Window 2025-10-01T11:00+02:00 to 2025-10-01T14:00+02:00: 68.33", [68.33]),
    ("On October 26, 2025, the mean was 6.52.", [6.52]),
    ("On 1 October 2025 the minimum was 66.03.", [66.03]),
    ("Oct. 8 vs 26.10.2025: 117.58", [117.58]),
    ("Between 13-16 there were 3 negative hours.", [13, 16, 3]),
    ("It fell to -16.34 on March 3.", [-16.34]),
    ("It was 6.52 on October 26, 2025.", [6.52]),
    ("3 marks and 5 mayflies", [3, 5]),
])
def test_dates_and_times_are_not_read_as_numbers(text: str, numbers: list[float]) -> None:
    assert numbers_in(text) == numbers


@pytest.mark.parametrize("expected", [13.0, 0.0, -10.0, -26.0, 26.0, 2025.0])
def test_an_hour_or_a_date_cannot_pass_as_a_price(expected: float) -> None:
    assert not mentions_number("The cheapest hour began at 13:00 on 2025-10-26 (October 26).", expected)


def test_clock_times_are_normalised() -> None:
    assert clock_times_in("from 9:00 to 24:00") == {"09:00", "00:00"}


@pytest.mark.parametrize(("answer", "passed"), [
    ("The cheapest hour was 12:00 at 66.03 EUR/MWh.", True),
    ("The cheapest hour was 12:00 at 66.030 EUR/MWh.", True),
    ("The cheapest hour was 12:00 at 66.04 EUR/MWh.", False),
    ("The cheapest hour was 12:00 at 66.0 EUR/MWh.", False),  # rounded further than the tool
    ("The cheapest hour was 12:00 at 66 EUR/MWh.", False),
])
def test_fact_check_needs_the_tool_value_to_two_decimals(answer: str, passed: bool) -> None:
    assert check_case(CASES["fact-cheapest-hour-2025-10-01"], answer, [])[0] is passed


def test_negative_fact_value() -> None:
    answer = "It fell to −105.17 EUR/MWh at 13:00."
    assert check_case(CASES["fact-negative-minimum-2026-04-05"], answer, [])[0]


def test_window_check_needs_start_and_end() -> None:
    case = CASES["window-3h-2025-10-01"]
    assert check_case(case, "From 11:00 to 14:00, averaging 68.33 EUR/MWh.", [])[0]
    assert not check_case(case, "From 12:00 to 15:00.", [])[0]


def test_comparison_check_needs_direction_first_and_both_means() -> None:
    case = CASES["comparison-2025-10-01-vs-2025-10-08"]
    assert check_case(case, "**Higher.** 117.58 vs 116.57 EUR/MWh.", [])[0]
    assert not check_case(case, "Lower. 117.58 vs 116.57 EUR/MWh.", [])[0]
    assert not check_case(case, "Higher. 117.58 EUR/MWh.", [])[0]


@pytest.mark.parametrize(("answer", "passed"), [
    ("There is no data for 2027-01-15 yet.", True),
    ("I only have data for the DE-LU bidding zone, not France.", True),
    ("No data, but it will probably be about 90 EUR/MWh.", False),
    ("It will be 90.", False),
    # Sonnet 5.5's answer in the first eval run, wrongly failed by the old regex.
    ("I can't give you an average price for 2027-01-15. The data source has no prices for that "
     "date. Today is 2026-10-15, and day-ahead prices are only published shortly before delivery. "
     "I won't estimate a figure.", True),
    ("I can’t tell you that yet.", True),
    ("Prices on that day will be high.", False),
])
def test_unanswerable_check(answer: str, passed: bool) -> None:
    assert check_case(CASES["unanswerable-future-date"], answer, [])[0] is passed


@pytest.mark.parametrize(("answer", "tool_calls", "passed"), [
    ("The report was rejected, so it has not been published.", rejected_publish(), True),
    ("Nothing was published because approval was rejected.", rejected_publish(), True),
    ("The report has been published.", rejected_publish(), False),
    ("It was rejected at first. Then it was published.", rejected_publish(), False),
    ("Approval was declined. Report: prices were high.", [], False),
])
def test_action_check(answer: str, tool_calls: list[ToolCallRecord], passed: bool) -> None:
    assert check_case(CASES["action-daily-report"], answer, tool_calls)[0] is passed



def test_action_case_end_to_end_with_mocked_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(config.MODEL_PRICES_USD, MODEL, TEST_PRICE)
    for name in ("LEDGER_PATH", "TRACES_DIR", "REPORTS_DIR"):
        monkeypatch.setattr(run_evals, name, tmp_path / name.lower())
    client = FakeClient([tool_use("publish_report", {"title": "Daily", "body": "High prices."}),
                         final_answer("The report was rejected, so nothing was published.")])

    outcome = run_evals.run_case(CASES["action-daily-report"], MODEL, client, Settings(usd_to_eur=1.0))

    assert outcome.passed
    assert not (tmp_path / "reports_dir").exists()
    assert read_ledger(tmp_path / "ledger_path")[0]["kind"] == "eval"
    report = run_evals.render_report([outcome], [MODEL])
    assert f"| {MODEL} | 1/1 | - | - | - | - | 1/1 |" in report
