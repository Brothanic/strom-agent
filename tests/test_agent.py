"""Tests for the agent loop, budget and approval gate with a mocked Anthropic client.

No test makes a real API call or costs money.
"""

import copy
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from anthropic.types import Message

from strom_agent import config
from strom_agent.agent import MAX_ITERATIONS, SYSTEM_PROMPT, run_agent
from strom_agent.approval import ask_on_terminal, reject_without_prompt
from strom_agent.budget import Budget, BudgetExceeded, MissingPriceConfiguration, call_cost_usd, read_ledger
from strom_agent.config import ModelPrice, Settings
from strom_agent.prices import PriceClient
from strom_agent.tools import ReportPublisher, Toolbox
from strom_agent.trace import Trace

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "data" / "fixtures"
MODEL = "test-model"
# Round numbers make expected costs easy to check by hand.
TEST_PRICE = ModelPrice(input=1.0, output=5.0, cache_write=1.25, cache_read=0.1)


@pytest.fixture(autouse=True)
def test_model_price(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(config.MODEL_PRICES_USD, MODEL, TEST_PRICE)


def message(content: list[dict], stop_reason: str, input_tokens: int = 1000, output_tokens: int = 100) -> Message:
    return Message.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant", "model": MODEL,
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })


def tool_use(name: str, arguments: dict, block_id: str = "toolu_1") -> Message:
    return message([{"type": "tool_use", "id": block_id, "name": name, "input": arguments}], "tool_use")


def final_answer(text: str) -> Message:
    return message([{"type": "text", "text": text}], "end_turn")


class FakeMessages:
    def __init__(self, responses: list[Any], input_tokens: int) -> None:
        self.responses = list(responses)
        self.input_tokens = input_tokens
        self.requests: list[dict] = []

    def count_tokens(self, **request: Any) -> SimpleNamespace:
        return SimpleNamespace(input_tokens=self.input_tokens)

    def create(self, **request: Any) -> Message:
        self.requests.append(copy.deepcopy(request))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    def __init__(self, responses: list[Any], input_tokens: int = 1000) -> None:
        self.messages = FakeMessages(responses, input_tokens)


def run(tmp_path: Path, client: FakeClient, settings: Settings | None = None,
        approve=reject_without_prompt, question: str = "Question?"):
    settings = settings or Settings(usd_to_eur=1.0)
    budget = Budget(MODEL, settings, tmp_path / "ledger.jsonl")
    price_client = PriceClient(tmp_path / "cache", FIXTURE_DIR, use_fixtures_only=True)
    toolbox = Toolbox(price_client, ReportPublisher(tmp_path / "reports"))
    trace = Trace(tmp_path / "traces")
    result = run_agent(question, client=client, model=MODEL, toolbox=toolbox, approve=approve,
                       budget=budget, trace=trace, today=date(2026, 10, 15))
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    return result, budget, events


def tool_results_sent(request: dict) -> list[dict]:
    return request["messages"][-1]["content"]


# --- Agent loop ----------------------------------------------------------------


def test_tool_result_is_sent_back_and_final_text_is_the_answer(tmp_path: Path) -> None:
    client = FakeClient([tool_use("price_stats", {"start_date": "2025-10-01", "end_date": "2025-10-01"}),
                         final_answer("The mean was 100 EUR/MWh.")])

    result, budget, events = run(tmp_path, client)

    assert result.answer == "The mean was 100 EUR/MWh."
    assert result.stop_reason == "end_turn"
    assert [call.name for call in result.tool_calls] == ["price_stats"]
    sent = tool_results_sent(client.messages.requests[1])
    assert sent[0]["tool_use_id"] == "toolu_1"
    assert sent[0]["is_error"] is False
    assert json.loads(sent[0]["content"])["days"][0]["date"] == "2025-10-01"
    assert budget.usage.api_calls == 2
    assert [event["event"] for event in events] == [
        "run_start", "model_response", "tool_call", "model_response", "run_end"]


def test_todays_date_is_in_the_user_message_not_the_system_prompt(tmp_path: Path) -> None:
    client = FakeClient([final_answer("Hi.")])
    run(tmp_path, client)
    request = client.messages.requests[0]

    assert request["messages"][0]["content"].startswith("Today is 2026-10-15.")
    assert "2026" not in SYSTEM_PROMPT
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_system_prompt_is_under_300_words() -> None:
    assert len(SYSTEM_PROMPT.split()) < 300


def test_invalid_tool_input_goes_back_to_the_model_as_an_error(tmp_path: Path) -> None:
    client = FakeClient([tool_use("get_prices", {"date": "not a date"}), final_answer("Sorry.")])

    result, _, _ = run(tmp_path, client)

    sent = tool_results_sent(client.messages.requests[1])
    assert sent[0]["is_error"] is True
    assert "Invalid input" in sent[0]["content"]
    assert result.answer == "Sorry."


def test_parallel_tool_calls_return_together_in_one_message(tmp_path: Path) -> None:
    both = message([
        {"type": "tool_use", "id": "toolu_a", "name": "get_prices", "input": {"date": "2025-10-01"}},
        {"type": "tool_use", "id": "toolu_b", "name": "get_prices", "input": {"date": "2025-10-08"}},
    ], "tool_use")
    client = FakeClient([both, final_answer("Done.")])

    run(tmp_path, client)

    sent = tool_results_sent(client.messages.requests[1])
    assert [result["tool_use_id"] for result in sent] == ["toolu_a", "toolu_b"]


def test_loop_stops_after_max_iterations(tmp_path: Path) -> None:
    endless = [tool_use("get_prices", {"date": "2025-10-01"}) for _ in range(MAX_ITERATIONS + 2)]
    client = FakeClient(endless)

    result, _, _ = run(tmp_path, client)

    assert result.stop_reason == "max_iterations"
    assert len(client.messages.requests) == MAX_ITERATIONS
    assert "Stopped after 6 model calls" in result.answer


def test_api_error_stops_cleanly(tmp_path: Path) -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    client = FakeClient([anthropic.APIConnectionError(request=request)])

    result, _, events = run(tmp_path, client)

    assert result.stop_reason == "api_error"
    assert "APIConnectionError" in result.answer
    assert events[-2]["event"] == "api_error"


# --- Approval -----------------------------------------------------------------


def test_rejected_report_is_reported_to_the_model_and_not_written(tmp_path: Path) -> None:
    client = FakeClient([tool_use("publish_report", {"title": "Daily", "body": "Low prices."}),
                         final_answer("The report was rejected and not published.")])

    result, _, events = run(tmp_path, client, approve=reject_without_prompt)

    sent = tool_results_sent(client.messages.requests[1])
    assert sent[0]["is_error"] is True
    assert "Rejected by human" in sent[0]["content"]
    assert result.tool_calls[0].approval == "rejected"
    assert events[2]["approval"] == "rejected"
    assert not (tmp_path / "reports").exists()


def test_approved_report_is_written(tmp_path: Path) -> None:
    client = FakeClient([tool_use("publish_report", {"title": "Daily", "body": "Low prices."}),
                         final_answer("Published.")])

    run(tmp_path, client, approve=lambda tool_name, preview: True)

    assert len(list((tmp_path / "reports").iterdir())) == 1


@pytest.mark.parametrize(("typed", "approved"),
                         [("y", True), ("Y", True), (" y ", True), ("yes", False), ("", False), ("n", False)])
def test_only_an_explicit_y_approves(typed: str, approved: bool) -> None:
    shown: list[str] = []
    decision = ask_on_terminal("publish_report", "THE CONTENT", read_line=lambda prompt: typed, write=shown.append)

    assert decision is approved
    assert "THE CONTENT" in shown


def test_missing_terminal_input_rejects() -> None:
    def no_input(prompt: str) -> str:
        raise EOFError

    assert ask_on_terminal("publish_report", "text", read_line=no_input, write=lambda text: None) is False


# --- Budget ---------------------------------------------------------------------


def test_call_cost_uses_each_token_rate() -> None:
    # 1M input at $1, 1M output at $5, 1M cache write at $1.25, 1M cache read at $0.10
    cost = call_cost_usd(TEST_PRICE, 1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert cost == pytest.approx(7.35)


def test_usage_is_summed_and_converted_to_euros(tmp_path: Path) -> None:
    budget = Budget(MODEL, Settings(usd_to_eur=0.5), tmp_path / "ledger.jsonl")
    usage = SimpleNamespace(input_tokens=1000, output_tokens=200,
                            cache_creation_input_tokens=400, cache_read_input_tokens=None)

    call_eur = budget.record(usage)

    expected_usd = (1000 * 1.0 + 200 * 5.0 + 400 * 1.25) / 1_000_000
    assert call_eur == pytest.approx(expected_usd * 0.5)
    assert budget.usage.cache_write_tokens == 400
    assert budget.usage.cache_read_tokens == 0


def test_run_without_price_or_exchange_rate_cannot_start(tmp_path: Path) -> None:
    with pytest.raises(MissingPriceConfiguration):
        Budget("unpriced-model", Settings(usd_to_eur=1.0), tmp_path / "ledger.jsonl")
    with pytest.raises(MissingPriceConfiguration):
        Budget(MODEL, Settings(), tmp_path / "ledger.jsonl")


def test_budget_stops_before_a_call_that_could_exceed_the_run_limit(tmp_path: Path) -> None:
    # Worst case per call: 1000 input * $1.25 + 1024 output * $5 = $0.00637.
    # The first call actually costs 1000 * $1 + 100 * $5 = $0.0015, so the second
    # check sees $0.0015 + $0.00637 = $0.00787, above the limit.
    client = FakeClient([tool_use("get_prices", {"date": "2025-10-01"}), final_answer("Never sent.")])
    settings = Settings(usd_to_eur=1.0, max_cost_per_run_eur=0.007)

    result, budget, _ = run(tmp_path, client, settings)

    assert result.stop_reason == "budget"
    assert "per-run limit" in result.answer
    assert len(client.messages.requests) == 1
    assert budget.cost_eur() == pytest.approx(0.0015)


def test_total_limit_counts_earlier_runs_from_the_ledger(tmp_path: Path) -> None:
    (tmp_path / "ledger.jsonl").write_text(json.dumps({"cost_eur": 4.999}) + "\n")
    client = FakeClient([final_answer("Never sent.")])

    result, _, _ = run(tmp_path, client)

    assert result.stop_reason == "budget"
    assert "total limit" in result.answer
    assert client.messages.requests == []


def test_check_raises_only_above_the_limit(tmp_path: Path) -> None:
    budget = Budget(MODEL, Settings(usd_to_eur=1.0, max_cost_per_run_eur=0.006), tmp_path / "l.jsonl")
    budget.check_before_call(input_tokens=0, max_tokens=1000)  # 1000 * $5 = $0.005
    with pytest.raises(BudgetExceeded):
        budget.check_before_call(input_tokens=1000, max_tokens=1000)  # + 1000 * $1.25 = $0.00625


def test_ledger_stores_a_hash_instead_of_the_question(tmp_path: Path) -> None:
    client = FakeClient([final_answer("Hi.")])
    _, budget, _ = run(tmp_path, client, question="secret question")

    budget.write_ledger("secret question", kind="ask")

    entry = read_ledger(tmp_path / "ledger.jsonl")[0]
    assert "secret question" not in json.dumps(entry)
    assert entry["kind"] == "ask"
    assert entry["api_calls"] == 1
    assert entry["cost_eur"] == pytest.approx((1000 * 1.0 + 100 * 5.0) / 1_000_000)
