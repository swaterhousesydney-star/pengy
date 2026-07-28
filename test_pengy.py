#!/usr/bin/env python3
"""Asserts for the quota parser. `python3 test_pengy.py`

The parser is the whole product, so it gets tested against real cap messages
rather than invented ones. When you hit a new cap format, paste it in here
first and make it pass second.
"""

import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pengy
from pengy import _parse_duration, is_capped, parse_reset

NOW = datetime.now(timezone.utc)


def near(dt, seconds, tol=90):
    assert dt is not None, "expected a reset time, got None"
    delta = (dt - NOW).total_seconds()
    assert abs(delta - seconds) < tol, f"expected ~{seconds}s out, got {delta:.0f}s ({dt})"


# ---------------------------------------------------------- detection --

CAPPED = [
    "Claude usage limit reached. Your limit will reset at 3pm (UTC).",
    "Claude AI usage limit reached|1753549200",
    "5-hour limit reached ∙ resets 3pm",
    "You've hit your usage limit. Try again in 4 hours 32 minutes.",
    "Error: 429 Too Many Requests - rate limit exceeded",
    '{"error":{"type":"rate_limit_error","message":"..."}}',
    "429 RESOURCE_EXHAUSTED: Quota exceeded for quota metric 'Generate requests'",
    "You have reached your daily gemini-2.5-pro quota limit.",
    "stream error: rate limit reached; retrying",
]
for msg in CAPPED:
    assert is_capped(msg, 1), f"missed a cap: {msg!r}"

NOT_CAPPED = [
    "Done. Wrote src/checkout.ts and all 42 tests pass.",
    "I added a note to the README about rate limits and how to handle them.",
    "The retry logic now backs off when the upstream API is slow.",
    "",
]
for msg in NOT_CAPPED:
    assert not is_capped(msg, 0), f"false positive on: {msg!r}"

# An agent writing *about* limits while succeeding is not a cap. Same words,
# exit 0, so the weak patterns must not fire.
assert not is_capped("I documented the 'usage limit reached' error path.", 0)
# ...but the machine-format marker is trusted even on a clean exit, because no
# model writes a pipe-delimited epoch by accident.
assert is_capped("Claude AI usage limit reached|1753549200", 0)


# ------------------------------------------------------------- epochs --

soon = int((NOW + timedelta(hours=2)).timestamp())
near(parse_reset(f"Claude AI usage limit reached|{soon}"), 7200)
near(parse_reset(f"limit reached|{soon * 1000}"), 7200)  # milliseconds
# Out-of-range epochs are junk, not reset times.
assert parse_reset("usage limit reached|1000000000") is None


# ---------------------------------------------------------- durations --

near(parse_reset("You've hit your usage limit. Try again in 4 hours 32 minutes."), 4 * 3600 + 32 * 60)
near(parse_reset("Rate limit reached. Try again in 45 minutes."), 45 * 60)
near(parse_reset("Quota exceeded. Please retry in 39.2s"), 39, tol=5)
near(parse_reset('"retryDelay": "33s"'), 33, tol=5)
near(parse_reset("retry-after: 3600"), 3600)
near(parse_reset("Rate limited. Try again in 1h30m"), 5400)
assert _parse_duration("try again in a little while") is None


# ------------------------------------------------------------- clocks --

reset_3pm_utc = parse_reset("Claude usage limit reached. Your limit will reset at 3pm (UTC).")
assert reset_3pm_utc is not None
assert reset_3pm_utc.astimezone(timezone.utc).hour == 15, reset_3pm_utc
assert reset_3pm_utc > NOW, "a reset time is always in the future"

reset_iana = parse_reset("Your limit will reset at 9:30am (Europe/London).")
assert reset_iana is not None and reset_iana > NOW

reset_24h = parse_reset("resets at 03:00")
assert reset_24h is not None and reset_24h.astimezone().hour == 3

reset_iso = parse_reset("Rate limit exceeded. Try again after 2099-01-01T20:00:00Z")
assert reset_iso is not None and reset_iso.year == 2099

# A bare number is not a clock time — "try again in 5" must not become 05:00.
assert parse_reset("Something failed, try again at 5") is None


# ----------------------------------------------------------- unknowns --

# Rule 4: a cap with no readable reset time returns None. None means ask the
# user. It must never quietly become "five hours from now".
assert is_capped("You've reached your usage limit. Upgrade to Pro for higher limits.", 1)
assert parse_reset("You've reached your usage limit. Upgrade to Pro for higher limits.") is None
assert parse_reset("429 Too Many Requests") is None


# ------------------------------------------------- the adapter table --

# Every adapter mistake found so far has been structural: a flag that means
# something else on that CLI, a prompt swallowed as a positional, a working
# directory silently ignored. These assertions catch that shape of error.
for name, spec in pengy.AGENTS.items():
    names = spec["bin"] if isinstance(spec["bin"], list) else [spec["bin"]]
    assert names and all(isinstance(n, str) for n in names), name

    for kind in ("run", "resume"):
        cmd = spec[kind]
        assert cmd, f"{name}.{kind} is empty"
        # The launcher rewrites cmd[0] to the resolved absolute path, so the
        # first element must be the binary and never a flag or a placeholder.
        assert cmd[0] in names or cmd[0] == "{bin}", f"{name}.{kind} must lead with the binary"
        assert not cmd[0].startswith("-"), f"{name}.{kind} leads with a flag"
        # The prompt has to reach the agent somehow: as an argument or on stdin.
        assert pengy.PROMPT in cmd or spec.get(f"{kind}_stdin"), f"{name}.{kind} drops the prompt"

    # A resume that needs a session id needs a way to have captured one.
    if pengy.SESSION in spec["resume"]:
        assert spec.get("session_re"), f"{name} resumes by session id but never reads one"
        assert re.compile(spec["session_re"]).groups == 1, f"{name}.session_re needs one group"

    for key in ("leash", "off_leash"):
        assert isinstance(spec[key], list), f"{name}.{key} must be a list"

# opencode ignores the process working directory, so its adapter must pass --dir
# explicitly. Without this it writes into whatever it decides the project root
# is — which overnight means editing the wrong repo.
assert pengy.DIR in pengy.AGENTS["opencode"]["run"], "opencode must be given an explicit --dir"
assert pengy.DIR in pengy.AGENTS["opencode"]["resume"], "opencode resume must be given --dir"

# droid prints its session id only in JSON output; the regex has to find it there.
droid_json = '{"type":"result","session_id":"dd6744db-7818-4e53-9a21-7d3fd4954059","is_error":false}'
found = re.search(pengy.AGENTS["droid"]["session_re"], droid_json)
assert found and found.group(1) == "dd6744db-7818-4e53-9a21-7d3fd4954059", found


# ------------------------------------------------ refusing bad input --

# Each of these used to be found out later than it should have been: an empty
# prompt burned a real agent call, and a mistyped --fallback only surfaced at
# 3am when the cap finally arrived and the rescue agent did not exist.
# These must hold on a machine with no agent CLIs at all — CI is exactly that,
# and a typo should be reported as a typo rather than as "no agents found".
def _run(*argv):
    return pengy.main(["run", *argv])


assert _run("   ", "-C", ".") == 2, "an empty prompt must not reach an agent"
assert _run("x", "-C", str(Path(tempfile.gettempdir()) / "pengy-no-such-dir")) == 2, "missing directory"
assert _run("x", "-a", "nonesuch", "-C", ".") == 2, "unknown agent"
assert _run("x", "--fallback", "nonesuch", "-C", ".") == 2, "unknown fallback"


# The UI polls a running job every couple of seconds. Reading the whole log
# each time costs ~110ms on a 20MB overnight log — 24 minutes of pure re-reading
# across one night — so only the tail is scanned, and it must still find the
# status lines that live at the end.
with tempfile.TemporaryDirectory() as tmp:
    big = Path(tmp) / "big.log"
    big.write_text("chatter\n" * 300_000 + "pengy claude capped. Resuming at 03:00 — 1h 45m to go.\n")
    assert big.stat().st_size > 2_000_000
    started = time.time()
    phase, note = pengy._job_phase(pengy._tail_bytes(big))
    assert time.time() - started < 0.05, "tail scan should be instant regardless of log size"
    assert phase == "capped" and "03:00" in note, (phase, note)


# ------------------------------------------------- the loop, end to end --

# A fake agent that caps once and succeeds on resume. Real caps arrive on the
# vendor's schedule, so this is the only way to test the thing that matters.
FAKE = """
import sys
if "--resume" in sys.argv:
    print("picked up where I left off")
    sys.exit(0)
print("You've hit your usage limit. Try again in 3 seconds.")
sys.exit(1)
"""

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    (tmp / "fake.py").write_text(FAKE)
    os.environ["XDG_STATE_HOME"] = str(tmp / "state")
    os.environ["LOCALAPPDATA"] = str(tmp / "state")
    pengy.notify = lambda *a, **k: None
    pengy.AGENTS["fake"] = {
        "bin": sys.executable,
        "run": [sys.executable, str(tmp / "fake.py"), pengy.PROMPT],
        "resume": [sys.executable, str(tmp / "fake.py"), "--resume", pengy.PROMPT],
        "leash": [],
        "off_leash": [],
    }

    # Needs a real installed agent, so it lives here with the fake one.
    assert pengy.main(["run", "x", "-a", "fake", "--fallback", "fake", "-C", str(tmp)]) == 2, \
        "handing a job to itself is not a fallback"

    started = time.time()
    code = pengy.main(["run", "build the thing", "-a", "fake", "-C", str(tmp)])
    elapsed = time.time() - started

    assert code == 0, f"capped run should recover, got exit {code}"
    assert 2.5 < elapsed < 20, f"should have waited out the cap, took {elapsed:.1f}s"
    assert pengy.read_ledger()["fake"]["state"] == "ok", "ledger should be clear after resume"

    # A cap with no readable reset time stops instead of guessing.
    (tmp / "fake.py").write_text('print("Usage limit reached. Upgrade for more.")\nimport sys; sys.exit(1)')
    started = time.time()
    assert pengy.main(["run", "x", "-a", "fake", "-C", str(tmp)]) == 75
    assert time.time() - started < 5, "must not sleep on an unknown reset time"
    assert pengy.read_ledger()["fake"]["resetsAt"] is None

# -------------------------------------------------------------- the board --

# The swarm has no orchestrator, so the merged board is the only thing that
# makes "who claimed what, and who was first" answerable. Ordering is the logic.
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    lanes = pengy.board_dir(tmp)
    (lanes / "claude.md").write_text(
        "- 09:05 CLAIM the parser\n  and everything under lib/parse\n- 09:20 DONE the parser\n")
    (lanes / "codex.md").write_text("- 09:10 CLAIM the tests\n")

    merged = pengy.read_board(tmp).splitlines()
    assert [line.split()[0] for line in merged] == ["claude", "claude", "codex", "claude"], merged
    assert "lib/parse" in merged[1], "a continuation line must stay under its own entry, not float to the top"
    assert "(nothing on the board" in pengy.read_board(tmp / "elsewhere"), "an unused directory is not an error"

    # Refusals, on a machine with no agent CLIs — which is exactly what CI is.
    assert pengy.main(["swarm", "   ", "-C", str(tmp)]) == 2, "a swarm needs a goal"
    assert pengy.main(["swarm", "x", "--agents", "claude,nonesuch", "-C", str(tmp)]) == 2, "unknown agent"
    assert pengy.main(["swarm", "x", "--agents", "fake", "-C", str(tmp)]) == 2, "one agent is not a swarm"

    # Every agent must be told its own lane and no one else's, or two of them
    # write the same file and lose each other's lines.
    brief = pengy.SWARM_BRIEF.format(me="codex", n=2, peers="claude", goal="ship it")
    assert ">> .pengy/board/codex.md" in brief and "board/claude.md" not in brief, brief

# An overnight board is the whole product, and string-sorting bare HH:MM puts
# 00:15 above 23:40 — the DONE above its own CLAIM. Dated stamps sort right.
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    lanes = pengy.board_dir(tmp)
    (lanes / "claude.md").write_text(
        "- 2026-07-27 23:40 CLAIM src/auth\n- 2026-07-28 00:15 DONE src/auth\n")
    (lanes / "codex.md").write_text(
        "- 2026-07-27 23:55 CLAIM the tests\n- 2026-07-28 01:30 NOTE auth moved\n")
    order = [f"{l.split()[0]} {l.split()[3]}" for l in pengy.read_board(tmp).splitlines()]
    assert order == ["claude 23:40", "codex 23:55", "claude 00:15", "codex 01:30"], order
    # and the brief must ask for that date, or nothing writes one
    assert "%Y-%m-%d" in pengy.SWARM_BRIEF, "the brief has to ask for a dated stamp"

# The board is written by agents on the default leash, which is edits-only for
# claude, gemini and droid — a brief that forbids their file tools leaves them
# unable to post at all.
assert "your normal file-editing tool is fine" in pengy.SWARM_BRIEF

# `pengy` on its own opens the window. It used to print usage, which taught
# nobody anything and read as a broken install.
_parsed = []
_chat = pengy.do_chat
pengy.do_chat = lambda a: _parsed.append(a.cmd) or 0
try:
    assert pengy.main([]) == 0
finally:
    pengy.do_chat = _chat
assert _parsed == ["chat"], _parsed

# One version, four files. The 0.5.0 release shipped 0.4.0 in all of them.
_root = Path(__file__).parent
_seen = {"pengy.py": pengy.__version__}
_seen["pyproject.toml"] = re.search(r'^version = "(.+?)"', (_root / "pyproject.toml").read_text(),
                                    re.M).group(1)
for _f in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
    for _v in set(re.findall(r'"version":\s*"(.+?)"', (_root / _f).read_text())):
        _seen[f"{_f}:{_v}"] = _v
assert len(set(_seen.values())) == 1, f"versions disagree: {_seen}"

print("all good")
