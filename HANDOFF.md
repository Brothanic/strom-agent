# Handoff

Updated: 2026-10-24, Europe/Berlin.

## Current state

- **Build steps 1–6 implemented** (owner asked on 2026-10-20 for steps 2–6
  without a stop between them). Nothing committed or pushed. 108 free tests pass
  (`uv run pytest`): fixtures plus a mocked Anthropic client, no network.
- **First real runs (step 3), 2026-10-24:** the owner ran four `ask` runs with
  `claude-haiku-5-5`: one fixture question, one live window question, and the
  publish flow once rejected and once approved (written to `reports/`). Total
  €0.0019 (ledger). All ended with `end_turn`; the most output in one call was
  417 tokens (thinking included), well under 1,024. Caching works: the
  1,359-token prefix was written once, then read on every later call.
- **Fixed after the owner's review of those runs:** (1) the report file name
  cut the title at 40 characters, turning "2026-10-23" into "2026-10-2"; the
  slug is no longer shortened (title ≤ 120 characters). (2) The model also
  wrote the data credit into the report body, so it appeared twice; the
  prompt and the tool description now say code appends it. (3) "at 23:00
  (hour starting 23:00)"; the prompt now says to name an hour once by its
  start time. (2) and (3) are prompt changes and not yet re-checked with a
  paid run. The one file written before the fix keeps its old name.
- **First eval run (step 5), 2026-10-24**, run by the owner: Haiku 5.5 15/15
  (€0.0038), Sonnet 5.5 14/15 (€0.0628). Ledger total so far €0.0685. The
  Sonnet failure (`unanswerable-future-date`) was a false negative of the
  check: "can't give" / "no prices" were not in `ADMITS_MISSING`. The pattern
  was widened (plus curly apostrophes), and re-checking all 30 recorded
  answers gives 15/15 for both, with no other result changed.
  `evals/report.md` is left as generated. The README shows both and notes an
  LLM judge for free-text checks as the better long-term fix (owner's idea).
  Sonnet: all calls ended normally, at most 576 output tokens per call.
- **Docker image not built or tested:** Docker is not installed on this Mac.
- Dependencies: `anthropic` 1.16 and `pyyaml` added to `httpx` and `pydantic`
  (all from the spec's list). `strom-agent` console script in `pyproject.toml`.
- Files: `tools.py`, `agent.py`, `budget.py`, `trace.py`, `approval.py`,
  `config.py`, `cli.py`, `evals/cases.yaml`, `evals/run_evals.py`, `Dockerfile`,
  `.dockerignore`, `.env.example`, tests `test_tools.py`, `test_agent.py`,
  `test_evals.py`.

## Repository (2026-10-24)

- Work committed on `main` at the owner's request. Before that, the whole Git
  history (all refs, including T3 checkpoint refs) was scanned for secret
  files and patterns, and for the real API key value from `.env`: nothing
  found. No dedicated scanner (gitleaks/trufflehog) is installed.
- `SYNCTHING.md`, `.stignore` and `syncthing-ignore.txt` describe the owner's
  device setup. At the owner's request they stay on disk but are ignored and
  were removed from `main`'s history. `AGENTS.md` still points to
  `SYNCTHING.md`, which exists only on the owner's machines.
- `.vscode/settings.json` only holds a workspace-relative interpreter path and
  stays in the repository.
- Pushed to the public repository https://github.com/Brothanic/strom-agent
  (`origin`, SSH), `main` only. Commits are authored as `Brothanic
  <47241463+Brothanic@users.noreply.github.com>`, set in this repository's
  local Git config; both commits were rewritten to that identity before the
  first push. T3 checkpoint refs stay local and are not pushed.

## Owner decisions for paid runs (resolved 2026-10-21)

1. **Model IDs.** `DEFAULT_MODEL = "claude-haiku-5-5"` as specified. The owner
   confirmed it with https://www.anthropic.com/claude-haiku-5-5 (released
   2026-10-07). An earlier draft wrongly used `claude-haiku-4-5`, based on an
   outdated model list. The comparison model is `claude-sonnet-5-5`.
2. **Price table.** The owner supplied a screenshot of the pricing page.
   `MODEL_PRICES_USD` now holds Haiku 5.5 (up-to-100k tier: 0.10 / 0.50 /
   0.125 / 0.01) and Sonnet 5.5 (2 / 10 / 2.50 / 0.10), as input / output /
   5-minute cache write / cache read in USD per MTok. Any other model still
   refuses to start.
3. **Currency.** At the owner's request, the rate was taken from the ECB
   reference rate of 2026-10-21 (1 EUR = 1.1234 USD), so
   `STROM_USD_TO_EUR=0.8902` in `.env`. It is fixed, not updated
   automatically. Every ledger line records the rate it used.
4. API key is in `.env` (CRLF line endings; `uv run --env-file` reads the
   values cleanly, checked). **Still open:** a Console spend limit, which the
   owner sets.
5. **Line count:** the owner accepts the current size (890 lines of code,
   1,267 physical lines). No trimming.

## Decisions worth explaining

- Approval is enforced in `Toolbox.call` (tools.py), not in agent.py or the
  prompt. The approver receives the exact rendered report plus its destination.
  Only an explicit `y`/`Y` approves; EOF rejects.
- Budget pre-call bound: `count_tokens` (free) for the input, priced at
  max(input, cache write), plus `MAX_TOKENS` (1024) of output. Checked against
  the per-run limit and the ledger total plus this run.
- Today's date is in the user message, not the system prompt, to keep the
  cacheable prefix stable. The prefix is ~1k tokens. Haiku 5.5's caching
  minimum is not known yet; check `cache_read_input_tokens` in the first runs.
- Haiku 5.5 has tiered prices (up to / above 100k tokens per prompt). The price
  table holds one rate per model, so the owner enters the up-to-100k tier;
  requests here are far smaller.
- `price_stats` returns found days plus `missing_days`, so a range over
  fixture gaps still works.
- Comparison eval questions ask the model to start with "Higher"/"Lower" so
  the direction check stays a deterministic regex.
- Eval number checks remove dates and clock times first (`DATES_AND_TIMES` in
  `evals/run_evals.py`), and a minus sign after a digit is a range dash, not
  a sign. With the current expected values no date or time was close enough
  to cause a false pass; this guards future cases.
- Eval number tolerance is 0.005 (owner decision, 2026-10-23): tool prices
  are rounded to two decimals and the model must copy them, so 66.0 for
  66.03 fails. The system prompt now says "copied exactly ..., without
  rounding it further" to match.
- Evals use `today = 2026-10-15`, fixture mode, no Slack URL, always-reject
  approval.

## Open risks / things to verify

- Thinking counts against `max_tokens=1024`. Haiku 5.5 stayed far below it
  in the first runs, and Sonnet 5.5 (adaptive thinking) in the eval run, with
  at most 576 output tokens per call. Longer questions could still hit it;
  watch for `stop_reason: max_tokens`.
- €0.05 per-run limit vs. Sonnet's worst-case estimate: about 3 calls fit;
  a run with more tool calls may stop on budget.
- README "every file reviewed by the owner" is not claimed; the owner review
  of steps 2–6 is open.

## Findings worth keeping

- 15-minute data starts on 2025-10-01; 2025-09-30 is hourly (observed).
- The API returns HTTP 429 with `Retry-After`; tools retry 429 like 503 with a
  fixed 1 s / 2 s backoff (Retry-After is not read).
- Subtracting aware datetimes that share one `ZoneInfo` uses wall-clock time;
  use Unix timestamps for durations (tests guard this, also for window ends).
- `round(91.565, 2)` gives 91.56 (binary floats); tool outputs use `round`.

## Next requested development step

Owner review of steps 2–6 and the Console spend limit. Re-run the report
question once to check the prompt fixes (credit line, hour wording). A new
eval run would regenerate `evals/report.md` with the widened check (only
when the owner asks for it). Docker build on a machine with Docker. Harder
eval cases are listed in the README as a next step (owner's idea), since
both models pass the current set.

## Mac VS Code environment (2026-10-15)

- Installed uv at `~/.local/bin/uv`; ran `uv sync --locked` on this Mac.
- Local `.venv` uses Python 3.12.15.
- `.vscode/settings.json` sets the default interpreter to `${workspaceFolder}/.venv`.
  VS Code had already selected Homebrew Python 3.13.2; if that selection
  persists, use **Python: Select Interpreter** and choose `.venv/bin/python`.
