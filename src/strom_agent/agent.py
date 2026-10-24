"""The tool-use loop, written directly against the Anthropic Messages API.

One iteration: check the budget, send the conversation, record usage. If the
model asked for tools, run them and send their results back; otherwise its
text is the answer. The loop stops after MAX_ITERATIONS model calls.
"""

import time
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import anthropic

from strom_agent.budget import Budget, BudgetExceeded
from strom_agent.tools import Approver, Toolbox
from strom_agent.trace import Trace

MAX_ITERATIONS = 6
MAX_TOKENS = 1024

SYSTEM_PROMPT = """\
You answer questions about day-ahead electricity prices in the German-Luxembourg \
bidding zone (DE-LU). The data comes from Energy-Charts (Fraunhofer ISE); the source \
is Bundesnetzagentur | SMARD.de under CC BY 4.0.

Rules:
- Never do math yourself. Every number in your answer must be copied exactly from a \
tool result, without rounding it further: no own sums, averages, differences, \
percentages or unit conversions. If no tool gives a number, say so instead of estimating.
- Prices are in EUR/MWh. Keep that unit.
- Times are Europe/Berlin. Write them in 24-hour HH:MM format. Tool results include \
the UTC offset, which matters on the day the clocks go back, when 02:00 occurs twice. \
Name an hour once by its start time, for example "at 23:00".
- Only DE-LU is supported. For any other bidding zone or country, say that you have \
no data for it.
- If a tool reports missing data or an error, say honestly that the data is not \
available. Never guess or invent numbers.
- Your only way to act outside this conversation is the publish_report tool. A human \
must approve every report. Do not write a data source or credit line into a report; \
publish_report appends it automatically. If publishing was rejected or failed, say \
clearly that nothing was published.
- Resolve relative dates such as "yesterday" from today's date in the user's message.
- Answer briefly in English."""

# Static text first and marked cacheable; today's date goes into the user message
# instead, so the cached prefix stays identical from day to day.
SYSTEM_BLOCKS = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]


@dataclass
class ToolCallRecord:
    name: str
    input: Any
    is_error: bool
    approval: str | None


@dataclass
class AgentResult:
    answer: str
    stop_reason: str  # the model's stop_reason, or "max_iterations", "budget", "api_error"
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    latency_seconds: float = 0.0


def run_agent(question: str, *, client: anthropic.Anthropic, model: str, toolbox: Toolbox,
              approve: Approver, budget: Budget, trace: Trace, today: date) -> AgentResult:
    started = time.monotonic()
    messages: list[dict] = [{"role": "user", "content": f"Today is {today.isoformat()}.\n\n{question}"}]
    tools = toolbox.schemas()
    tool_calls: list[ToolCallRecord] = []
    trace.log("run_start", model=model, question=question, today=today)

    def finish(answer: str, stop_reason: str) -> AgentResult:
        trace.log("run_end", answer=answer, stop_reason=stop_reason, usage=asdict(budget.usage),
                  cost_eur=budget.cost_eur())
        return AgentResult(answer, stop_reason, tool_calls, time.monotonic() - started)

    for iteration in range(1, MAX_ITERATIONS + 1):
        try:
            # Counting tokens is free; it makes the budget check exact for the input.
            counted = client.messages.count_tokens(model=model, system=SYSTEM_BLOCKS, tools=tools,
                                                   messages=messages)
            budget.check_before_call(counted.input_tokens, MAX_TOKENS)
            call_started = time.monotonic()
            response = client.messages.create(model=model, max_tokens=MAX_TOKENS, system=SYSTEM_BLOCKS,
                                              tools=tools, messages=messages)
        except BudgetExceeded as error:
            trace.log("budget_stop", iteration=iteration, message=str(error))
            return finish(str(error), "budget")
        except anthropic.APIError as error:
            # The SDK has already retried connection errors, 429 and 5xx twice.
            trace.log("api_error", iteration=iteration, error=repr(error))
            return finish(f"The model API failed: {error.__class__.__name__}. Please try again later.",
                          "api_error")

        call_cost_eur = budget.record(response.usage)
        trace.log("model_response", iteration=iteration, duration_seconds=time.monotonic() - call_started,
                  cost_eur=call_cost_eur, response=response.model_dump(mode="json"))
        # Send the full content back, including any thinking blocks, as the API expects.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return finish(answer_text(response), response.stop_reason or "unknown")

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_started = time.monotonic()
            result = toolbox.call(block.name, block.input, approve)
            trace.log("tool_call", name=block.name, input=block.input, output=result.content,
                      is_error=result.is_error, approval=result.approval,
                      duration_seconds=time.monotonic() - tool_started)
            tool_calls.append(ToolCallRecord(block.name, block.input, result.is_error, result.approval))
            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                 "content": result.content, "is_error": result.is_error})
        # All results of one turn go back together in a single user message.
        messages.append({"role": "user", "content": tool_results})

    return finish(f"Stopped after {MAX_ITERATIONS} model calls without a final answer. "
                  "Try a narrower question.", "max_iterations")


def answer_text(response: Any) -> str:
    texts = [block.text for block in response.content if block.type == "text"]
    answer = "\n".join(texts).strip()
    if response.stop_reason == "max_tokens":
        answer += "\n\n[The answer was cut off at the output limit.]"
    if response.stop_reason == "refusal":
        answer = answer or "The model declined to answer."
    return answer or "(The model returned no text.)"
