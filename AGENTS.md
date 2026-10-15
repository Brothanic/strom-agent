# Project instructions

## Start here

1. Read `SPEC.md` in full. It is the owner's project specification.
2. Read `HANDOFF.md` for the current state and next step.
3. Keep `SPEC.md`, implementation and verification claims distinct. Do not
   silently change requirements to match the implementation.

## Working agreement

- This is an interview work sample. Prefer code the owner can explain over
  abstractions, frameworks or infrastructure. Use the manual official Anthropic
  Python SDK loop, Python 3.12 and uv as specified.
- Everything committed to this repository must be in English.
- Follow SPEC section 9 one step at a time. After each step, stop and provide a
  short walkthrough: files, behavior, decisions, alternatives, verification and
  remaining limitations. Wait for the owner's instruction before the next step.
- Target no more than 1,000 lines of Python excluding tests. Stop and ask the
  owner before exceeding this limit. Keep code readable and dependencies few;
  justify additions to the specified dependency list.
- No implementation has been started by the setup task. Do not interpret the
  existence of this repository as completion of build step 1.
- Update `HANDOFF.md` after meaningful work so a new T3 session can continue.

## Boundaries

- DE-LU only. Attribute the data source and license as specified. Calculate all
  numerical results in deterministic tools, never in the model.
- Keep approval in code. The model cannot approve itself. In evals reject writes
  without an interactive prompt; never actually post an eval report externally.
- Tests use fixtures and a mocked Anthropic client; do not incur API charges for
  ordinary tests. Record actual live/eval costs only from real usage.
- Before paid calls, verify available model IDs with the owner and require the
  owner-supplied current price table and API credentials. Do not guess prices or
  silently substitute a model. Clarify the currency conversion/accounting before
  enforcing euro-denominated budgets against provider prices.
- Never commit secrets, caches, traces or the ledger. Never read credentials
  from another project. Do not publish, push, configure a webhook, or start paid
  runs merely to prove this setup works.
- Before the first authorized push, scan the entire Git history for secrets.
- Web UI, other bidding zones, forecasting, RAG, multi-agent architecture and a
  database are out of scope. Do not import another project's manager workflow.
