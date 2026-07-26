---
name: pengy
description: Hand a long or overnight coding job to a background agent that survives usage caps. Use when the user says "run this overnight", "keep going after the rate limit", "do this in the background", "hand this to codex/gemini", "I'm going to bed", or when a job is large enough that hitting a usage cap partway through is likely. Also use to check which agents still have quota before delegating.
---

# Pengy

Pengy runs another coding agent as a background job. When that agent hits its usage cap, Pengy reads the reset time out of the agent's own output, waits, and resumes the session — so the work survives rate limits and outlives this conversation.

## When to reach for it

- The user is leaving: *"run this overnight"*, *"I'm going to bed"*, *"let it work while I'm out"*.
- The job is big enough that a cap mid-way is likely — a large migration, a full test suite to fix, a sweeping refactor.
- The user wants a different agent on it: *"give this to codex"*, *"have gemini do the docs"*.
- The user asks what still has quota.

Do **not** reach for it for short work you can just do. Delegating a two-minute edit to a background process is slower and harder to follow than doing it.

## How

Three MCP tools, provided by this plugin:

- **`pengy_quota`** — which agents are installed and which are capped. Check this before delegating, and prefer an agent whose quota is `ok`.
- **`pengy_run`** — start the job. Takes `prompt`, and optionally `agent`, `dir`, and `fallback` (another agent to hand the job to instead of waiting out a cap). Returns a job id straight away; it does not block.
- **`pengy_jobs`** — status of running and finished jobs.

Write the `prompt` as a complete, self-contained brief. The background agent starts with no memory of this conversation — it gets your prompt and the working directory, nothing else. Say which files, which commands to run, and what "done" looks like.

After starting a job, tell the user the job id and that `pengy jobs` shows progress. Do not poll `pengy_jobs` in a loop.

## What to tell the user honestly

- Background jobs run on the leash by default: the agent edits files but does not get a bypass mode. An agent cannot grant itself `--off-leash`; only the human can, via `PENGY_MCP_ALLOW_OFF_LEASH=1`.
- `fallback` starts a **fresh context** on the other agent. It cannot read the first agent's session.
- If a cap message contains no readable reset time, Pengy stops rather than guessing when to resume. That job will be waiting for the user.

## If the tools are missing

Pengy isn't installed. One command, no dependencies:

```bash
curl -fsSL https://pengy.app/install.sh | sh
```
