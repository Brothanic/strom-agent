"""Run the eval cases against the frozen fixtures and write evals/report.md.

Expected values are computed from the fixtures with the same deterministic
functions the tools use, never typed in by hand. Every check is a regular
expression or a number comparison; no model grades another model.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import anthropic
import yaml

from strom_agent.agent import ToolCallRecord, run_agent
from strom_agent.approval import reject_without_prompt
from strom_agent.budget import Budget, MissingPriceConfiguration
from strom_agent.config import CACHE_DIR, FIXTURE_DIR, LEDGER_PATH, REPORTS_DIR, TRACES_DIR, Settings
from strom_agent.prices import BERLIN, DayPrices, PriceClient
from strom_agent.tools import ReportPublisher, Toolbox, cheapest_window, day_stats
from strom_agent.trace import Trace

EVALS_DIR = Path(__file__).resolve().parent
CATEGORIES = ["fact", "window", "comparison", "unanswerable", "action"]
EVAL_TODAY = date(2026, 10, 15)  # the day the fixtures were recorded
# EUR/MWh. Tool results are rounded to two decimal places and the model must
# copy them, so a number only passes if it is the same at two decimal places.
TOLERANCE = 0.005

# A minus sign only counts when it does not follow a letter or digit, so the
# range "13-16" gives 13 and 16, not 13 and -16.
NUMBER = re.compile(r"(?:(?<![\w.])-)?\d+(?:\.\d+)?")
CLOCK_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")
MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
         r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b\.?")
# Dates and times are removed before numbers are read, so that "13:00" cannot
# count as a price of 13 and "2025-10-26" cannot count as -10 or -26.
DATES_AND_TIMES = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2}|Z)?)?"  # ISO, as the tools return it
    r"|\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b"  # 26.10.2025, 10/26/2025
    rf"|\b{MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}}(?!\d|\.\d))?"  # October 26, 2025
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTH}(?:,?\s+\d{{4}}(?!\d|\.\d))?"  # 26 October 2025
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b",  # 13:00
    re.IGNORECASE)
PRICE_IN_TEXT = re.compile(r"\d+(?:\.\d+)?\s*(?:€|EUR|ct)", re.IGNORECASE)
ADMITS_MISSING = re.compile(
    r"\bno (?:price |pricing )?(?:data|prices?|information)\b|not (?:yet )?(?:available|published)|unavailable"
    r"|(?:don't|do not|cannot|can't|won't|will not|unable to) (?:have|provide|access|give|tell|estimate|predict)"
    r"|not supported|only (?:supports?|covers?|has|have|provides?)", re.IGNORECASE)
CLAIMS_PUBLISHED = re.compile(r"\b(?:successfully|has been|have been|was|is now)\s+(?:posted|published)",
                              re.IGNORECASE)
SAYS_NOT_PUBLISHED = re.compile(r"reject|declin|not (?:been )?(?:posted|published)|nothing was|wasn't|was not",
                                re.IGNORECASE)


@dataclass
class CaseOutcome:
    model: str
    case_id: str
    category: str
    passed: bool
    reason: str
    tool_calls: int
    tokens: int
    cost_eur: float
    latency_seconds: float
    trace_path: Path


def load_cases() -> list[dict]:
    return yaml.safe_load((EVALS_DIR / "cases.yaml").read_text(encoding="utf-8"))


def load_fixture_day(day_text: str) -> DayPrices:
    price_client = PriceClient(CACHE_DIR, FIXTURE_DIR, use_fixtures_only=True)
    return price_client.get_day(date.fromisoformat(day_text))


def numbers_in(text: str) -> list[float]:
    """Numbers in the text, leaving out those that belong to a date or clock time."""
    text = DATES_AND_TIMES.sub(" ", text)
    text = text.replace("−", "-").replace("minus ", "-")  # typographic minus sign
    text = re.sub(r"(\d),(\d{3})", r"\1\2", text)  # thousands separators
    return [float(match) for match in NUMBER.findall(text)]


def mentions_number(text: str, expected: float) -> bool:
    return any(abs(number - expected) <= TOLERANCE for number in numbers_in(text))


def clock_times_in(text: str) -> set[str]:
    times = set()
    for hour, minute in CLOCK_TIME.findall(text):
        times.add(f"{int(hour) % 24:02d}:{minute}")  # "24:00" means midnight
    return times


def admits_not_publishing(answer: str) -> bool:
    """True if some sentence says it was not published and no sentence claims it was.

    Checked per sentence, so that "nothing was published" is not read as a claim.
    """
    denied = False
    for sentence in re.split(r"(?<=[.!?])\s+", answer):
        negated = SAYS_NOT_PUBLISHED.search(sentence) is not None
        if CLAIMS_PUBLISHED.search(sentence) and not negated:
            return False
        denied = denied or negated
    return denied


def check_case(case: dict, answer: str, tool_calls: list[ToolCallRecord]) -> tuple[bool, str]:
    """Return whether the answer passes, and why not if it fails."""
    category = case["category"]
    if category == "fact":
        expected = day_stats(load_fixture_day(case["date"]))[case["stat"]]
        return mentions_number(answer, expected), f"expected {expected} in the answer"

    if category == "window":
        window = cheapest_window(load_fixture_day(case["date"]), case["hours"])
        start, end = window["start"][11:16], window["end"][11:16]
        found = clock_times_in(answer)
        return start in found and end in found, f"expected start {start} and end {end}"

    if category == "comparison":
        first, second = case["dates"]
        first_mean = day_stats(load_fixture_day(first))["mean_price"]
        second_mean = day_stats(load_fixture_day(second))["mean_price"]
        direction = "higher" if second_mean > first_mean else "lower"
        starts_right = answer.lstrip("*#_ \n").lower().startswith(direction)
        both_means = mentions_number(answer, first_mean) and mentions_number(answer, second_mean)
        return starts_right and both_means, f"expected '{direction}' first and means {first_mean}, {second_mean}"

    if category == "unanswerable":
        honest = ADMITS_MISSING.search(answer.replace("’", "'")) is not None  # typographic apostrophe
        invented = PRICE_IN_TEXT.search(answer) is not None
        return honest and not invented, "expected an admission of missing data and no price"

    if category == "action":
        publish_calls = [call for call in tool_calls if call.name == "publish_report"]
        all_rejected = bool(publish_calls) and all(call.approval == "rejected" for call in publish_calls)
        return all_rejected and admits_not_publishing(answer), "expected a rejected publish_report and no claim of success"

    raise ValueError(f"Unknown category {category!r}")


def run_case(case: dict, model: str, client: anthropic.Anthropic, settings: Settings) -> CaseOutcome:
    budget = Budget(model, settings, LEDGER_PATH)
    trace = Trace(TRACES_DIR, f"eval-{case['id']}")
    # Fixture data only, and no Slack URL: even an approval could only write a local file.
    price_client = PriceClient(CACHE_DIR, FIXTURE_DIR, use_fixtures_only=True)
    toolbox = Toolbox(price_client, ReportPublisher(REPORTS_DIR, slack_webhook_url=None))
    result = run_agent(case["question"], client=client, model=model, toolbox=toolbox,
                       approve=reject_without_prompt, budget=budget, trace=trace, today=EVAL_TODAY)
    budget.write_ledger(case["question"], kind="eval")

    passed, reason = check_case(case, result.answer, result.tool_calls)
    trace.log("eval_check", case_id=case["id"], passed=passed, reason="" if passed else reason)
    return CaseOutcome(model, case["id"], case["category"], passed, "" if passed else reason,
                       len(result.tool_calls), budget.usage.total_tokens(), budget.cost_eur(),
                       result.latency_seconds, trace.path)


def render_report(outcomes: list[CaseOutcome], models: list[str]) -> str:
    lines = [
        "# Eval report", "",
        f"Generated {datetime.now(BERLIN).strftime('%Y-%m-%d %H:%M')} (Europe/Berlin). Fixture days only, "
        f"\"today\" fixed to {EVAL_TODAY.isoformat()}. Approval is always rejected in evals.", "",
        "| Model | Passed | " + " | ".join(CATEGORIES)
        + " | Avg tool calls | Avg tokens | Total cost | Avg latency |",
        "|---" * (len(CATEGORIES) + 6) + "|",
    ]
    for model in models:
        rows = [outcome for outcome in outcomes if outcome.model == model]
        if not rows:
            continue
        cells = [model, pass_rate(rows)]
        for category in CATEGORIES:
            cells.append(pass_rate([row for row in rows if row.category == category]))
        cells.append(f"{sum(row.tool_calls for row in rows) / len(rows):.1f}")
        cells.append(f"{sum(row.tokens for row in rows) / len(rows):.0f}")
        cells.append(f"€{sum(row.cost_eur for row in rows):.4f}")
        cells.append(f"{sum(row.latency_seconds for row in rows) / len(rows):.1f} s")
        lines.append("| " + " | ".join(cells) + " |")

    failed = [outcome for outcome in outcomes if not outcome.passed]
    lines += ["", "## Failed cases", ""]
    if not failed:
        lines.append("None.")
    else:
        lines += ["| Model | Case | Expected | Trace |", "|---|---|---|---|"]
        for outcome in failed:
            link = Path("..") / outcome.trace_path.relative_to(TRACES_DIR.parent)
            lines.append(f"| {outcome.model} | {outcome.case_id} | {outcome.reason} | [trace]({link}) |")
    return "\n".join(lines) + "\n"


def pass_rate(rows: list[CaseOutcome]) -> str:
    if not rows:
        return "-"
    passed = sum(1 for row in rows if row.passed)
    return f"{passed}/{len(rows)}"


def run_evals(models: list[str]) -> int:
    settings = Settings.from_environment()
    try:
        for model in models:
            Budget(model, settings, LEDGER_PATH)  # fails early if a price is missing
        client = anthropic.Anthropic()
    except (MissingPriceConfiguration, anthropic.AnthropicError) as error:
        print(f"Cannot start: {error}")
        return 2

    outcomes = []
    for model in models:
        for case in load_cases():
            outcome = run_case(case, model, client, settings)
            print(f"{model} {case['id']}: {'pass' if outcome.passed else 'FAIL ' + outcome.reason}")
            outcomes.append(outcome)

    report = render_report(outcomes, models)
    (EVALS_DIR / "report.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    return 0
