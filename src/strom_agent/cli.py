"""Command line entry point: `strom-agent ask | evals | costs`."""

import argparse
import sys
from collections import defaultdict
from datetime import datetime

import anthropic

from strom_agent.agent import run_agent
from strom_agent.approval import ask_on_terminal
from strom_agent.budget import Budget, MissingPriceConfiguration, read_ledger
from strom_agent.config import (CACHE_DIR, DEFAULT_MODEL, FIXTURE_DIR, LEDGER_PATH, PROJECT_ROOT,
                                REPORTS_DIR, TRACES_DIR, Settings)
from strom_agent.prices import BERLIN, PriceClient
from strom_agent.tools import ReportPublisher, Toolbox
from strom_agent.trace import Trace


def build_toolbox(settings: Settings, fixtures_only: bool) -> Toolbox:
    price_client = PriceClient(CACHE_DIR, FIXTURE_DIR, use_fixtures_only=fixtures_only)
    webhook = settings.slack_webhook_url.get_secret_value() if settings.slack_webhook_url else None
    return Toolbox(price_client, ReportPublisher(REPORTS_DIR, webhook))


def run_ask(question: str, model: str, fixtures_only: bool) -> int:
    settings = Settings.from_environment()
    try:
        budget = Budget(model, settings, LEDGER_PATH)
        client = anthropic.Anthropic()
    except (MissingPriceConfiguration, anthropic.AnthropicError) as error:
        print(f"Cannot start: {error}", file=sys.stderr)
        return 2

    trace = Trace(TRACES_DIR, "ask")
    result = run_agent(question, client=client, model=model, toolbox=build_toolbox(settings, fixtures_only),
                       approve=ask_on_terminal, budget=budget, trace=trace, today=datetime.now(BERLIN).date())
    budget.write_ledger(question, kind="ask")

    usage = budget.usage
    print(f"\n{result.answer}\n")
    print(f"[{model} | {usage.api_calls} model calls, {len(result.tool_calls)} tool calls | "
          f"tokens: {usage.input_tokens} in, {usage.output_tokens} out, {usage.cache_read_tokens} cache read, "
          f"{usage.cache_write_tokens} cache write | €{budget.cost_eur():.4f} | trace: {trace.path}]")
    return 0 if result.stop_reason in ("end_turn", "stop_sequence") else 1


def run_costs() -> int:
    entries = read_ledger(LEDGER_PATH)
    if not entries:
        print("No runs recorded yet.")
        return 0
    totals: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"runs": 0, "tokens": 0, "cost": 0.0})
    for entry in entries:
        group = totals[(entry["kind"], entry["model"])]
        group["runs"] += 1
        group["tokens"] += (entry["input_tokens"] + entry["output_tokens"]
                            + entry["cache_write_tokens"] + entry["cache_read_tokens"])
        group["cost"] += entry["cost_eur"]

    print(f"{'kind':<6} {'model':<22} {'runs':>5} {'tokens':>10} {'cost':>10}")
    for (kind, model), group in sorted(totals.items()):
        print(f"{kind:<6} {model:<22} {group['runs']:>5} {group['tokens']:>10} €{group['cost']:>9.4f}")
    print(f"Total: €{sum(entry['cost_eur'] for entry in entries):.4f} over {len(entries)} runs")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="strom-agent", description="DE-LU day-ahead price agent.")
    commands = parser.add_subparsers(dest="command", required=True)

    ask = commands.add_parser("ask", help="Ask one question")
    ask.add_argument("question")
    ask.add_argument("--model", default=DEFAULT_MODEL)
    ask.add_argument("--fixtures", action="store_true", help="Use only the frozen fixture days")

    evals = commands.add_parser("evals", help="Run the eval cases against fixtures")
    evals.add_argument("--model", dest="models", action="append", help="Repeat to compare models")

    commands.add_parser("costs", help="Show cumulative costs from the ledger")

    args = parser.parse_args(argv)
    if args.command == "ask":
        return run_ask(args.question, args.model, args.fixtures)
    if args.command == "costs":
        return run_costs()

    # evals/ sits next to src/ as specified, outside the installed package.
    sys.path.insert(0, str(PROJECT_ROOT))
    from evals.run_evals import run_evals

    return run_evals(args.models or [DEFAULT_MODEL])
