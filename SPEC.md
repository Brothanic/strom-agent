# SPEC: strom-agent

A small, transparent LLM agent that answers questions about the German day-ahead electricity market. It uses tools, asks for human approval before acting externally, enforces a cost budget, and is measured with evals.

## 0. Context for the coding agent (read first)

This project is a **work sample for job applications** in AI integration / agent engineering. The owner must be able to **explain every line** in an interview. That leads to hard rules:

- **Simplicity over elegance.** No agent framework (no LangChain, no LangGraph). The tool-use loop is written directly against the official `anthropic` Python SDK so it is visible how it works.
- **Size:** target ≤ 1000 lines of Python excluding tests. If it grows beyond that, stop and ask.
- **Readable code:** descriptive names, short functions, type hints. No nested comprehensions or clever one-liners. Comments explain *why*, not *what*.
- **Few dependencies:** `anthropic`, `httpx`, `pydantic`, `pyyaml`, `pytest`. Justify any additional dependency.
- **Build incrementally** (see section 9). After each step, stop and give the owner a short code walkthrough: which files, what they do, which decisions were made and what the alternatives were.
- Python 3.12, package management with `uv`.
- Everything in the repo is in English: code, comments, commit messages, README.

## 1. Goal and usage

```bash
strom-agent ask "When was the cheapest 3-hour window yesterday for charging?"
strom-agent ask "Compare the average prices of October 1 and October 8" --model claude-sonnet-5-5
strom-agent ask "Write a short daily report for yesterday and post it"   # → approval prompt
strom-agent evals --model claude-haiku-5-5 --model claude-sonnet-5-5
strom-agent costs            # cumulative costs from the ledger
```

Default model: `claude-haiku-5-5`.

## 2. Data source

- Energy-Charts API (Fraunhofer ISE): `GET https://api.energy-charts.info/price?bzn=DE-LU&start=YYYY-MM-DD&end=YYYY-MM-DD`
- The response contains `unix_seconds[]`, `price[]` (EUR/MWh), `unit` and `license_info`. Errors: 404 when no data exists, 422 for invalid parameters. The API is occasionally unavailable (503).
- License: DE-LU data is CC BY 4.0 (Bundesnetzagentur | SMARD.de). Credit the source in the README and in every report. **Support only the DE-LU bidding zone**, because other zones come with different license terms.
- With 15-minute resolution, values are aggregated to hourly means for hourly analyses. Detect the resolution, don't assume it.
- **Cache and fixtures:** responses are stored as JSON under `data/cache/`. For tests and evals there are **frozen fixture days** under `data/fixtures/` (5–7 days, recorded once). Evals run exclusively against these fixtures so they are reproducible and have fixed correct answers.
- Time zone: all output in `Europe/Berlin`. Handle daylight saving transitions correctly: such a day has 23 or 25 hours, and it needs a test.

## 3. Architecture

```
src/strom_agent/
  prices.py     # API client, cache, fixture mode, aggregation
  tools.py      # tool schemas + deterministic implementations
  agent.py      # tool-use loop (anthropic SDK), max iterations
  approval.py   # human-in-the-loop gate for external actions
  budget.py     # token/cost accounting, limits, ledger
  trace.py      # JSONL trace per run
  config.py     # settings (pydantic), price table
  cli.py        # entry point
evals/
  cases.yaml
  run_evals.py
tests/
```

**Core principle: the LLM never does the math.** Every number in an answer comes from a tool. The model picks tools, combines their results and writes the answer. This rule is also stated in the system prompt.

## 4. Tools

| Tool | Kind | Description |
|---|---|---|
| `get_prices(date)` | read | Hourly prices for a day (list of time and price) |
| `price_stats(start_date, end_date)` | read | Min, max, mean and number of negative-price hours per day |
| `cheapest_window(date, hours)` | read | Cheapest contiguous window: start, end, average price |
| `publish_report(title, body)` | **write** | Writes a report to `reports/`, or optionally to a Slack incoming webhook (if `SLACK_WEBHOOK_URL` is set) |

- Tool inputs are validated with pydantic. Invalid input returns a structured error as the tool result (`is_error: true`) instead of raising. The model should be able to react to it.
- API errors (404, 503, timeout) also produce structured errors with a clear message, e.g. "no data for this date". For 503 and timeouts, at most 2 retries with backoff.

## 5. Agent loop

- Manual loop: send message → if `stop_reason == "tool_use"`, run the tools → return `tool_result` → repeat.
- **At most 6 iterations** per question. After that, stop cleanly with a message to the user.
- Short system prompt (under 300 words): role, "never do the math yourself", "say honestly when data is missing", "external actions only via `publish_report`".
- Enable prompt caching for the system prompt and tool definitions if the SDK supports it. Report cached tokens separately in the budget.

## 6. Human-in-the-loop (`approval.py`)

- Every tool carries a `requires_approval` flag in code. `publish_report` is the only write tool and has `True`.
- Before execution, the CLI shows the exact content and asks `Approve? [y/N]`. Anything other than an explicit `y` is a rejection, and the model receives "rejected by human" as the tool result.
- **The model cannot approve itself.** The approval logic lives only in code, not in the prompt.
- In eval mode there is no interactive prompt. Approval is automatically rejected and logged.

## 7. Budget and costs (`budget.py`)

- Price table per model in `config.py` (input, output, cache read, cache write per million tokens). **The owner fills these in from the current Anthropic pricing page. Do not guess.**
- Per run, sum the token counts from `response.usage` of every API response and compute the cost.
- Limits: `max_cost_per_run` (default €0.05) and `max_cost_total` (default €5, from the ledger). Check before every API call. If a limit is exceeded, stop with a clear message.
- Ledger: `data/ledger.jsonl`, one line per run (time, model, tokens, cost, question hash).
- In addition, the owner sets a spend limit in the Anthropic Console as a second safeguard. The README mentions it.

## 8. Observability and evals

**Trace:** each run writes `traces/<timestamp>.jsonl` with all model responses, tool calls (name, input, output, duration), errors, token counts and costs. No API key and no webhook URL in the trace.

**Evals (`evals/cases.yaml`, 15 cases, fixture days only):**

- 6× fact questions (cheapest or most expensive hour, mean). Check: the expected number appears in the answer, within a small tolerance.
- 3× window questions (cheapest n-hour window). Check: start and end time are correct.
- 2× comparisons between two days. Check: direction and numbers are correct.
- 2× unanswerable questions (date without data, other bidding zone). Check: the answer says honestly that data is missing and contains no made-up number.
- 2× action questions ("post a report"). Check: `publish_report` was called, approval was rejected, and the answer does not claim it was posted.

Expected values are **computed in code from the fixtures**, not entered by hand. Checks are deterministic (regex and number comparison), no LLM-as-judge.

**Report:** `evals/report.md` with a table per model: overall and per-category pass rate, avg. tool calls, avg. tokens, total cost, avg. latency. Plus the failed cases with a link to their trace. Passing several `--model` flags produces a comparison, e.g. Haiku vs. Sonnet.

**Tests (`pytest`, free):** tools against fixtures (including a DST day and 15-minute aggregation), budget calculation and abort, the approval gate, and the agent loop with a **mocked Anthropic client** (no real API calls in tests).

## 9. Build steps (stop after each step and give a walkthrough)

1. Project scaffold, `prices.py` with cache, record fixtures, tests for aggregation and DST.
2. `tools.py` with all read tools, plus tests.
3. `agent.py`, `budget.py`, `trace.py`, CLI `ask`. First real run with Haiku.
4. `approval.py` and `publish_report`.
5. Evals and report. One run with Haiku and Sonnet.
6. Dockerfile (`docker run --env-file .env strom-agent ask "…"`), README.

## 10. Security

- `ANTHROPIC_API_KEY` and `SLACK_WEBHOOK_URL` only from environment variables. `.env`, `data/cache`, `traces/` and `data/ledger.jsonl` are in `.gitignore`. Fixtures are committed.
- Before the first push, scan the entire Git history for secrets.

## 11. README (in English)

1. What the agent does, with an example run as a terminal snippet
2. Architecture sketch and the core principle "the LLM doesn't do the math"
3. Design decisions with rationale: no framework, approval in code instead of the prompt, deterministic evals, fixtures, budget
4. Eval results (table from `report.md`)
5. Costs: what a run and a full eval pass actually cost (from the ledger)
6. Limitations and next steps
7. **How this was built:** specified by the owner, implemented with Claude Code, every file reviewed by the owner. Honest and brief.
8. Data source and license notice

## 12. Out of scope

Web UI, other bidding zones, forecasting, RAG, multi-agent, database. These may appear in the README under "Next steps".

## 13. Owner's interview prep (after completion)

The owner should be able to answer without looking anything up:

- How does the tool-use loop work step by step? What happens when a tool fails?
- Why is approval in code and not in the prompt? How could a model get around a prompt rule?
- How are costs calculated? How much do cached tokens make up?
- Why fixtures and deterministic checks instead of LLM-as-judge? Where are the limits of the evals?
- What was the most surprising difference between Haiku and Sonnet in the evals?
- How would you connect the agent to Slack or GitHub in production (OAuth, permissions, GDPR)?
