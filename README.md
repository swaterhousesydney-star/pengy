<div align="center">

<img src="site/assets/pengy.jpg" width="120" alt="Pengy" />

# Pengy

**Your coding agent hits its usage cap at 1:15am. Pengy waits, resumes, and tells you at breakfast.**

[![CI](https://github.com/swaterhousesydney-star/pengy/actions/workflows/ci.yml/badge.svg)](https://github.com/swaterhousesydney-star/pengy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-black.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-black.svg)](https://www.python.org/)
[![Zero dependencies](https://img.shields.io/badge/dependencies-0-black.svg)](pengy.py)

</div>

```bash
curl -fsSL https://raw.githubusercontent.com/swaterhousesydney-star/pengy/main/install.sh | sh
```

---

Every tool in this category sells you **visibility** — more terminals, faster diffs, all in one window. That is the consolation prize for having to sit there.

Pengy sells the opposite thing. You give it a job and go to bed.

```console
$ pengy run "finish the checkout flow and run the tests"

pengy $ claude -p …
  … 71 minutes of work …
  Claude usage limit reached. Your limit will reset at 3am.

pengy claude capped. Resuming at 03:00 — 1h 45m to go.
pengy window reset. Resuming claude.

pengy $ claude -c -p …
  … carries on from exactly where it stopped …

pengy claude finished in 7h 12m across 1 cap
```

One notification, at the end. Not one per agent, per turn, per file.

## Why this doesn't already exist

Because no model vendor has a reason to build it. Anthropic has no commercial incentive to help you route around Anthropic's rate limits, and considerably less to hand your unfinished task to Google's model when they cap you. The gap isn't an oversight — it's a position none of them can comfortably occupy.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/swaterhousesydney-star/pengy/main/install.sh | sh
```

Or, if you'd rather use a package manager:

```bash
pipx install git+https://github.com/swaterhousesydney-star/pengy
```

Or just take the file. It's one script with no dependencies:

```bash
curl -fsSL https://raw.githubusercontent.com/swaterhousesydney-star/pengy/main/pengy.py -o ~/.local/bin/pengy && chmod +x ~/.local/bin/pengy
```

> Not on PyPI: the name `pengy` there belongs to an unrelated project, so `pip install pengy`
> gets you somebody else's package. Use one of the three commands above.

Pengy needs Python 3.9+ and at least one agent CLI already installed and logged in. It does not want your API keys — it drives the subscriptions you already pay for.

```console
$ pengy doctor
pengy 0.2.0  ·  python 3.12.3  ·  linux
ledger    ~/.local/state/pengy/ledger.json
notify    notify-send
agents    claude, codex, gemini, opencode, kimi, antigravity, droid
parser    ok
```

## Use

```bash
# the whole product
pengy run "migrate the auth module to the new SDK and keep the tests green"

# pick an agent and a directory
pengy run "write the missing tests" --agent codex -C ~/code/checkout

# let it run commands unattended — this is the agent's own yolo mode, see below
pengy run "get CI green" --off-leash

# don't wait out the cap, hand the job to an agent that still has budget
pengy run "refactor the parser" --agent claude --fallback opencode

# background it — survives closing the terminal, logs to a file
pengy run "fix every failing test" --detach
pengy jobs

# what's installed, and who's capped
pengy agents
```

| Agent | Binary | Resumes with | Notes |
|---|---|---|---|
| Claude Code | `claude` | `claude -c` | |
| Codex | `codex` | `codex exec resume --last` | prompt goes over stdin — a bare one is read as a session id |
| Gemini CLI | `gemini` | `gemini --resume latest` | needs `--skip-trust`, or the approval mode is silently downgraded |
| OpenCode | `opencode` | `opencode run -c` | **must** be given `--dir`; it ignores the process working directory |
| Kimi | `kimi` | `kimi --continue` | `--print` auto-approves tool calls — no edits-only mode exists |
| Antigravity | `antigravity-cli` / `agy` | `agy -c` | |
| Droid | `droid` | `droid exec -s <id>` | resumes by session id only, read back from its JSON output |

Pengy finds these even when they're not on your `PATH` — `~/.opencode/bin`, `~/.factory/bin`,
`/snap/bin` and friends are checked directly, because a detached job never sources your shell
profile.

Adding another agent is [one dict entry](pengy.py). Pull requests welcome — `test_pengy.py`
checks the shape of every adapter, so a mistake in one shows up before a user finds it.

## Use it from inside your agent

Pengy is also an MCP server, so the agent you're already talking to can hand work to Pengy — check who still has quota, start a background job that survives caps, and read back the status. One command per host:

```bash
claude mcp add pengy -- pengy mcp     # Claude Code
codex  mcp add pengy -- pengy mcp     # Codex
gemini mcp add pengy pengy mcp        # Gemini CLI
```

Any other MCP host (Cursor, Zed, Windsurf) takes the same stdio server: command `pengy`, args `["mcp"]`.

Then, in your session: *"check what still has quota and hand the migration to codex overnight"*.

| Tool | Does |
|---|---|
| `pengy_quota` | Which agents are installed, and which are capped until when |
| `pengy_run` | Start a background job that survives caps. Returns a job id immediately |
| `pengy_jobs` | Status of running and finished jobs |

**An agent cannot grant itself a bypass mode.** Jobs started over MCP run on the leash. Only the human running the server can change that, by setting `PENGY_MCP_ALLOW_OFF_LEASH=1` in the environment.

### As a Claude Code plugin

Gets you the MCP server *and* a skill that knows when to delegate, in two lines:

```
/plugin marketplace add swaterhousesydney-star/pengy
/plugin install pengy@pengy
```

## The one rule

**A reset time is parsed, never guessed.**

Pengy reads the reset time out of the agent's own output. It never assumes "caps are five hours, so add five hours" — vendors change their windows, plans differ, and a wrong guess means waking at 4am, firing into a still-capped agent, and burning your morning.

If a cap message contains no readable reset time, Pengy stops and says so:

```console
pengy claude is capped and did not say when it resets.
pengy Pengy does not guess reset times. Re-run when your window is back,
pengy or pass --fallback <agent> to hand the job to one that still has budget.
```

That's the honest failure, and it's deliberately louder than a wrong sleep would be.

## Help the parser (30 seconds, genuinely useful)

The parser is only as good as the cap messages it has seen. **Next time an agent tells you you're rate limited, copy the message and [open an issue](https://github.com/swaterhousesydney-star/pengy/issues/new?title=cap+message&body=Agent%3A%20%0AMessage%3A%20%0A) with it.** Verbatim, redact nothing but your own paths.

Every message makes overnight runs work for everyone using it. Currently handled:

- `Claude usage limit reached. Your limit will reset at 3pm (UTC).`
- `Claude AI usage limit reached|1753549200` — epoch
- `5-hour limit reached ∙ resets 3pm`
- `You've hit your usage limit. Try again in 4 hours 32 minutes.`
- `429 RESOURCE_EXHAUSTED: Quota exceeded…` · `"retryDelay": "33s"` · `retry-after: 3600`
- ISO timestamps, `1h30m` durations, IANA zones, UTC offsets

## What Pengy does not do

Being straight about this, because a tool that runs while you're asleep earns trust by under-claiming:

- **It is not a safety layer.** `--off-leash` passes your agent's *own* bypass flag (`--permission-mode bypassPermissions`, `--dangerously-bypass-approvals-and-sandbox`, `--approval-mode yolo`). Pengy does not sit between the agent and your filesystem, and it does not stop an agent that decides to `rm -rf`. Use a git repo. Default is on the leash, which is the agent's own edits-only mode.
- **It is not a daemon.** `--detach` gives you a background process that outlives the terminal, but nothing restarts it if the machine reboots or sleeps through the reset. A long cap on a laptop that suspends will resume when the laptop does, not before.
- **`--fallback` starts a fresh context.** A different vendor's agent cannot read Claude's session. It gets your original prompt and the working directory, not the reasoning so far.
- **It doesn't watch your agents.** No dashboard, no live diffs, no terminal multiplexing. [Twelve other products](https://www.codeagentswarm.com/en) do that, several of them well.
- **It sends nothing anywhere.** No telemetry, no account, no server. The only network call it can make is a webhook *you* set in `PENGY_NOTIFY_URL`.

## Notifications

Desktop by default (`notify-send` on Linux, Notification Center on macOS). For your phone, point it at anything that accepts a JSON POST — [ntfy](https://ntfy.sh), a Discord or Slack webhook:

```bash
export PENGY_NOTIFY_URL="https://ntfy.sh/your-private-topic"
```

You get pinged when the work is done, when it's genuinely stuck, and when it starts waiting out a cap. That's the whole list. A tool that pings you constantly is a tool you mute by Thursday.

## Develop

```bash
git clone https://github.com/swaterhousesydney-star/pengy && cd pengy
python3 test_pengy.py     # asserts, no framework, ~3 seconds
python3 pengy.py doctor
```

`pengy.py` is one file, standard library only, and intends to stay that way. The tests include a fake agent that really does cap and really does get resumed, because real caps arrive on the vendor's schedule rather than yours.

`site/` is the landing page. The `pages` workflow publishes it to GitHub Pages along with
`install.sh` and `pengy.py`, so once Pages is enabled (Settings → Pages → Source: GitHub Actions)
the installer is also served from `https://swaterhousesydney-star.github.io/pengy/install.sh`.

## Licence

MIT. Do what you like with it.

<div align="center"><br><sub>Go to bed.</sub></div>
