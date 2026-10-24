# strom-agent

A small, transparent LLM agent for questions about the German-Luxembourg
(DE-LU) day-ahead electricity market. It uses tools for every number, asks a
human before it acts externally, enforces a cost budget, and is measured with
deterministic evals.

**Status:** all six build steps from [SPEC.md](SPEC.md) are implemented and
covered by free tests (mocked model, frozen fixtures). The first real runs
with `claude-haiku-5-5` are done; the eval run is still open. See
[HANDOFF.md](HANDOFF.md) for the current state.

## What it does

```bash
strom-agent ask "When was the cheapest 3-hour window yesterday for charging?"
strom-agent ask "Compare the average prices of October 1 and October 8, 2025" --model claude-sonnet-5-5
strom-agent ask "Write a short daily report for yesterday and post it"   # → approval prompt
strom-agent ask "What was the cheapest hour on 2025-10-01?" --fixtures     # frozen data only
strom-agent evals --model claude-haiku-5-5 --model claude-sonnet-5-5
strom-agent costs
```

Example (first real run, 2026-10-24):

```text
$ strom-agent ask "What was the cheapest hour on 2025-10-01?" --fixtures

The cheapest hour on 2025-10-01 was **12:00–13:00 (Berlin time), at 66.03 EUR/MWh**.

[claude-haiku-5-5 | 2 model calls, 1 tool calls | tokens: 1011 in, 200 out, 1359 cache read, 1359 cache write | €0.0003 | trace: traces/20261024T170118647212-ask.jsonl]
```

## Architecture

```
question ─► agent.py ── loop (max 6 model calls) ───────────────────────────┐
              │  budget.py: count input tokens, check worst-case cost       │
              │  Anthropic Messages API (system prompt + tool definitions)  │
              │  stop_reason == "tool_use"?                                 │
              │     yes ─► tools.py: validate input (pydantic)              │
              │             ├─ get_prices / price_stats / cheapest_window   │
              │             │     └─ prices.py: fixtures | cache | API      │
              │             └─ publish_report ─► approval.py (y/N) ─► file  │
              │                                                  or Slack   │
              │            results go back to the model ────────────────────┘
              │     no  ─► the text is the answer
              └─ trace.py: one JSONL line per step   budget.py: ledger line per run
```

| File | Role |
|---|---|
| `prices.py` | Energy-Charts client: DE-LU only, cache, fixture mode, resolution detection, DST-aware validation, hourly means |
| `tools.py` | Tool schemas, input validation, deterministic calculations, retries, report publishing |
| `agent.py` | The manual tool-use loop and the system prompt |
| `approval.py` | Terminal approval (`Approve? [y/N]`) and the always-reject approver for evals |
| `budget.py` | Token and cost accounting, limits, `data/ledger.jsonl` |
| `trace.py` | `traces/<timestamp>-<label>.jsonl` per run |
| `config.py` | Paths, settings from environment variables, the model price table |
| `cli.py` | `ask`, `evals`, `costs` |
| `evals/` | 15 cases and the deterministic runner that writes `evals/report.md` |

**Core principle: the LLM doesn't do the math.** Minimums, maximums, means,
negative-hour counts and cheapest windows are computed in `tools.py`. The
model chooses tools, combines their results and writes the answer. The system
prompt forbids its own arithmetic, and the evals check that numbers match the
tool values.

## Design decisions

- **No agent framework.** The loop is about 60 lines in `agent.py`, written
  against the official `anthropic` SDK, so every step can be read and
  explained. The cost is writing message handling by hand.
- **Approval in code, not in the prompt.** `Toolbox.call` checks the tool's
  `requires_approval` flag and asks a human before `publish_report` runs. The
  model only sees the result ("Rejected by human"). A prompt rule could be
  overridden by injected text or ignored by the model; a code path cannot. In
  evals, every approval is rejected without a prompt.
- **Exact content for approval.** The human approves the exact rendered
  report, including the data-source credit that code appends, and its
  destination.
- **Structured tool errors.** Invalid input, missing days and API failures
  come back as `is_error` tool results, so the model can react. Temporary API
  failures (429, 503, timeouts) are retried twice with 1 s and 2 s backoff.
- **Fixtures and deterministic evals.** Seven frozen days (including both DST
  transitions, 15-minute data and negative prices) give fixed correct
  answers. Expected values are computed from the fixtures by the same
  functions the tools use. Checks are regular expressions and number
  comparisons, not LLM-as-judge, so a result can be reproduced and explained.
- **Budget as a real upper bound.** Before each call the request's input tokens
  are counted with the free `count_tokens` endpoint and priced at the most
  expensive input rate (cache write). The output is priced at `max_tokens`
  (1024). The call is skipped if this worst case could exceed the per-run
  limit (default €0.05) or the ledger total (default €5). Remaining gaps:
  `count_tokens` is documented as an estimate, and the euro amount depends on
  the exchange rate the owner configures.
- **Prompt caching.** The system prompt and tool definitions are marked
  cacheable, and today's date goes into the user message so the prefix stays
  identical. Cached tokens are reported separately. The prefix is roughly
  1,000 tokens. Whether that reaches each model's caching minimum must be
  confirmed in a real run (`cache_read_input_tokens` above zero).
- **Second safeguard.** Set a monthly spend limit in the Anthropic Console as
  well. The code's budget only protects against this program's own calls.

## Setup

```bash
uv sync
cp .env.example .env        # fill in ANTHROPIC_API_KEY and STROM_USD_TO_EUR
uv run pytest               # free: fixtures and a mocked model
uv run --env-file .env strom-agent ask "..."
```

`MODEL_PRICES_USD` in `src/strom_agent/config.py` holds the prices for
`claude-haiku-5-5` (the default) and `claude-sonnet-5-5`, taken from the
[Anthropic pricing page](https://www.anthropic.com/pricing) on 2026-10-21. A
model without an entry cannot run. `STROM_USD_TO_EUR` is a fixed rate you set
yourself; this project used the ECB reference rate of the same day (0.8902).

### Docker

```bash
docker build -t strom-agent .
docker run --rm -it --env-file .env strom-agent ask "What was the cheapest hour yesterday?"
```

`-it` is needed for the approval prompt; without a terminal every approval is
rejected. The ledger, traces and reports live inside the container and are lost
with it. Mount them to keep them, and to make the total budget limit work
across runs:

```bash
mkdir -p traces reports data/cache && touch data/ledger.jsonl
docker run --rm -it --env-file .env \
  -v "$PWD/data/ledger.jsonl:/app/data/ledger.jsonl" -v "$PWD/data/cache:/app/data/cache" \
  -v "$PWD/traces:/app/traces" -v "$PWD/reports:/app/reports" \
  strom-agent ask "..."
```

## Eval results

`strom-agent evals --model … --model …` writes
[evals/report.md](evals/report.md) with the pass rate overall and per category,
average tool calls, tokens and latency, total cost, and the failed cases with
trace links. First run, 2026-10-24, 15 cases per model:

| Model | Passed | Avg tool calls | Avg tokens | Total cost | Avg latency |
|---|---|---|---|---|---|
| claude-haiku-5-5 | 15/15 | 1.2 | 4030 | €0.0038 | 4.0 s |
| claude-sonnet-5-5 | 14/15 | 1.4 | 4033 | €0.0628 | 5.1 s |

Sonnet's one failure was the check's fault, not the model's. Asked about
2027-01-15, it answered "I can't give you an average price for 2027-01-15. The
data source has no prices for that date." The regular expression for admitting
missing data knew "no data" and "can't provide", but not "no prices" or "can't
give". The pattern has been widened since. Re-checking all 30 recorded answers
with the new check gives 15/15 for both models and changes no other result.
`evals/report.md` still shows the original run.

This shows the main weakness of checking natural language with regular
expressions: every new phrasing needs a new pattern, a game of whack-a-mole.
A better fix would probably be an LLM judge for the free-text checks
(admitting missing data, not claiming a rejected report was published),
ideally a different model from the one being tested, while numbers and
times stay deterministic. That costs money per eval run and makes results
less reproducible, so it was left out here.

Dates and clock times (ISO, `26.10.2025`, `October 26, 2025`, `13:00`) are
removed before numbers are compared, so an hour or a day cannot pass as a
price. A number must match the tool value to two decimal places, the
precision the tools return; an answer that rounds further fails. Known
limits of the checks: a fact answer that lists many prices can
contain the expected number by chance. Comparison answers must start with
"Higher"/"Lower". The "no made-up number" check only looks for numbers
followed by a currency. Negation detection for "not published" is a
per-sentence regular expression.

## Costs

`strom-agent costs` sums `data/ledger.jsonl` by run type and model. First
real runs (four `ask` runs with `claude-haiku-5-5`): €0.0019 in total,
about €0.0002–0.0007 per question. First eval run: €0.0038 for Haiku and
€0.0628 for Sonnet (15 cases each).

## Limitations and next steps

- Questions about one day or a short range (at most 31 days); windows do not
  cross midnight.
- English answers; prices only in EUR/MWh.
- A run's ledger line is written after the run; two parallel runs could each
  pass the total-limit check.
- Harder eval cases. Both models passed every case in the first run, so the
  set no longer tells them apart. Candidates: questions over several days or
  ranges with missing days, the repeated 02:00 hour on the autumn DST day,
  questions that tempt the model to calculate ("how many percent higher?"),
  and requests that try to skip the approval step.
- Next steps (out of scope here): other bidding zones (different licenses),
  forecasting, a web UI, OAuth-based Slack/GitHub apps instead of a webhook.

## How this was built

Specified by the owner and implemented step by step with Claude Code. The
owner went through the step 1 walkthrough; the owner's file-by-file review of
steps 2 to 6 is still open.

## Data source and license

Prices: [Energy-Charts API](https://api.energy-charts.info/) (Fraunhofer ISE).
DE-LU data: Bundesnetzagentur | SMARD.de,
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Every published
report ends with this credit. Only the DE-LU bidding zone is supported,
because other zones come with different license terms.
