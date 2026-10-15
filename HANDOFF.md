# Handoff

Updated: 2026-10-15, Europe/Berlin.

## Current state

- Project setup only; no application, fixtures, tests or eval results yet.
- The supplied `SPEC.md` is preserved byte for byte.
- Project entry instructions, a starter README, Python version selection and
  ignore rules are present.
- No dependencies installed, API credentials configured, model calls made,
  data downloaded, external report published or Git remote configured.

## Next requested development step

When the owner asks to begin, implement **SPEC section 9, step 1 only**:

1. Minimal Python/uv package scaffold.
2. `prices.py`: DE-LU client, cache, frozen fixture mode and resolution detection.
3. Record the agreed fixture days with attribution and provenance.
4. Deterministic tests for aggregation and Europe/Berlin DST days.
5. Explain the files, decisions, alternatives and actual test outcomes; stop for
   the owner's walkthrough before implementing tools or the agent loop.

## Later prerequisites (not setup blockers)

- Confirm the example model IDs are actually available before step 3.
- The owner fills in current pricing and configures API credentials and a
  Console spend limit. Resolve price-table currency and euro-budget handling.
- Implement and explain the budget's actual pre-call protection; do not claim
  a pre-call check alone can bound an otherwise unbounded next response.
- A README statement that every file was owner-reviewed is not true yet. Record
  review status honestly rather than copying the future claim from the spec.
