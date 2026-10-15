# strom-agent

A small, transparent tool-using LLM agent for questions about the German DE-LU
day-ahead electricity market, with deterministic calculations, human approval,
cost accounting and reproducible evaluations.

**Status: setup only.** This is not a working application yet. The authoritative
scope and six incremental build steps are in [SPEC.md](SPEC.md).

## Working in T3 Code

Open the `strom-agent` project on the existing Mac mini T3 server. Coding-agent
entry instructions are in [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md);
[HANDOFF.md](HANDOFF.md) records the state and next step.

Suggested first message:

> Read AGENTS.md, SPEC.md and HANDOFF.md. Implement only build step 1 from
> section 9. Keep the code simple enough for me to explain in an interview.
> Run the free tests, give me the short code walkthrough, and stop before step 2.

## Constraints

- Python 3.12 and uv; manual loop against the official Anthropic Python SDK.
- No agent framework; target at most 1,000 Python lines excluding tests.
- The LLM chooses tools and writes explanations; tools calculate the numbers.
- DE-LU only; frozen fixtures and deterministic checks for evals.
- Approval is enforced in code; tests and evals must not publish externally.
- Model IDs and owner-supplied prices must be verified before paid calls. API
  usage is separate from an existing Claude Code subscription.
- No API credentials, webhook, remote repository or paid run is configured by
  this initial setup.

## Data attribution

Planned source: [Energy-Charts API](https://api.energy-charts.info/), Fraunhofer
ISE. DE-LU price-data attribution specified by the owner: Bundesnetzagentur |
SMARD.de, [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Preserve
the actual response license metadata when recording fixtures and attribute
the source in every generated report.

## How this is being built

Specified by the owner. Initial repository preparation by J.A.R.V.I.S.; intended
incremental implementation with Claude Code through T3 and owner walkthroughs.
Implementation, owner file reviews, measured costs and eval results are not yet
complete. The final README will report actual outcomes rather than placeholders.
