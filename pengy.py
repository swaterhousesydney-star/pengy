#!/usr/bin/env python3
"""Pengy — keeps your AI coding agents working after they hit the rate limit.

Hand it a job, close the laptop. When the agent hits its usage cap, Pengy reads
the reset time out of the agent's own output, waits, and resumes the session.
No API keys, no account, no daemon. Standard library only.

    pengy run "finish the checkout flow and run the tests"

The one rule that matters: a reset time is *parsed*, never guessed. If Pengy
cannot read a reset time it stops and says so rather than sleeping a default
interval and waking into a still-capped agent at 4am.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

__version__ = "0.2.0"

PROMPT = "{prompt}"
SESSION = "{session}"
DIR = "{dir}"

# --------------------------------------------------------------- agents --

# Every agent is one entry. A fifth agent is one more entry and nothing else.
AGENTS = {
    "claude": {
        "bin": "claude",
        "run": ["claude", "-p", PROMPT],
        "resume": ["claude", "-c", "-p", PROMPT],
        "leash": ["--permission-mode", "acceptEdits"],
        "off_leash": ["--permission-mode", "bypassPermissions"],
    },
    "codex": {
        "bin": "codex",
        "run": ["codex", "exec", "--skip-git-repo-check", PROMPT],
        # `codex exec resume` takes [SESSION_ID] [PROMPT] positionally, so a bare
        # prompt after --last is swallowed as a session id. `-` reads it from stdin.
        "resume": ["codex", "exec", "resume", "--last", "--skip-git-repo-check", "-"],
        "resume_stdin": True,
        "leash": ["--sandbox", "workspace-write"],
        "off_leash": ["--dangerously-bypass-approvals-and-sandbox"],
        "leash_resume": [],  # `codex exec resume` has no --sandbox; it keeps the session's
    },
    "gemini": {
        "bin": "gemini",
        # --skip-trust or Gemini silently downgrades --approval-mode to "default"
        # in a folder it has not seen before, and then blocks on a prompt forever.
        "run": ["gemini", "--skip-trust", "-p", PROMPT],
        "resume": ["gemini", "--skip-trust", "--resume", "latest", "-p", PROMPT],
        "leash": ["--approval-mode", "auto_edit"],
        "off_leash": ["--approval-mode", "yolo"],
    },
    "opencode": {
        "bin": "opencode",
        # `-p` here is --password, not --print. `run` is already non-interactive.
        # --dir is not optional: opencode ignores the process working directory
        # and will happily write into whatever it decides the project root is.
        "run": ["opencode", "run", "--dir", DIR, PROMPT],
        "resume": ["opencode", "run", "--dir", DIR, "-c", PROMPT],
        "leash": [],
        "off_leash": ["--auto"],
    },
    "kimi": {
        "bin": "kimi",
        # -C is --continue; -c is an alias for --command, which is the prompt.
        "run": ["kimi", "--print", "-w", DIR, "-p", PROMPT],
        "resume": ["kimi", "--print", "-w", DIR, "--continue", "-p", PROMPT],
        "leash": [],
        "off_leash": ["--yolo"],
        "leash_note": "kimi --print auto-approves tool calls — it has no edits-only mode.",
    },
    "antigravity": {
        "bin": ["antigravity-cli", "agy"],  # snap ships antigravity-cli; the binary calls itself agy
        "run": ["{bin}", "-p", PROMPT],
        "resume": ["{bin}", "-c", "-p", PROMPT],
        "leash": ["--mode", "accept-edits"],
        "off_leash": ["--dangerously-skip-permissions"],
    },
    "droid": {
        "bin": "droid",
        # droid exec resumes by session id only — no --last. JSON output is the
        # one place that id is reliably printed, so this adapter reads it back.
        "run": ["droid", "exec", "-o", "json", "--cwd", DIR, PROMPT],
        "resume": ["droid", "exec", "-o", "json", "--cwd", DIR, "-s", SESSION, PROMPT],
        "session_re": r'"session_id"\s*:\s*"([0-9a-fA-F-]{36})"',
        "leash": ["--auto", "low"],
        "off_leash": ["--auto", "high", "--skip-permissions-unsafe"],
    },
}

RESUME_PROMPT = "Continue where you left off. Do not restart work you already finished."


# Agent CLIs install themselves outside the default PATH and rely on a shell
# profile to add it. A detached job, a cron entry or a GUI-launched MCP host
# never sources that profile, so look in the known install dirs too.
EXTRA_BIN_DIRS = [
    Path.home() / ".local/bin",
    Path.home() / ".opencode/bin",
    Path.home() / ".factory/bin",
    Path.home() / ".npm-global/bin",
    Path.home() / ".bun/bin",
    Path.home() / ".deno/bin",
    Path("/snap/bin"),
    Path("/usr/local/bin"),
    Path("/opt/homebrew/bin"),
]


def agent_bin(agent: str) -> str | None:
    """Absolute path to an agent's binary, or None. Tries each candidate name."""
    names = AGENTS[agent]["bin"]
    for name in [names] if isinstance(names, str) else names:
        found = shutil.which(name)
        if found:
            return found
        for directory in EXTRA_BIN_DIRS:
            for candidate in (directory / name, directory / f"{name}.exe"):
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
    return None


def installed() -> list[str]:
    return [a for a in AGENTS if agent_bin(a)]


# ------------------------------------------------------- the cap parser --

# Strong signals are unambiguous machine output — trusted on their own.
CAP_STRONG = [
    r"usage limit reached\s*\|\s*\d{9,13}",
    r'"type"\s*:\s*"rate_limit_error"',
    r"\brate_limit_error\b",
    r"\bRESOURCE_EXHAUSTED\b",
    r"\b429\b[^\n]{0,80}?(?:too many requests|rate.?limit|quota)",
    r"(?:too many requests|rate.?limit|quota)[^\n]{0,80}?\b429\b",
]

# Weak signals are prose the model could plausibly have written itself, so they
# only count when the agent also exited non-zero.
CAP_WEAK = [
    r"usage limit reached",
    r"\d+\s*-\s*hour limit reached",
    r"limit will reset",
    r"rate limit(?:ed|s)?\b[^\n]{0,40}(?:reached|exceeded|hit)",
    r"(?:reached|exceeded|hit)[^\n]{0,40}\brate limit",
    r"quota (?:exceeded|exhausted)",
    r"\byou(?:'ve|'re| have| are)?\b[^\n]{0,25}(?:hit|reached|exceeded|out of)[^\n]{0,45}\blimit",
    r"too many requests",
    r"upgrade to (?:pro|max)[^\n]{0,40}higher limits",
]

TAIL_LINES = 80


def is_capped(tail: str, returncode: int) -> bool:
    """True when `tail` is an agent telling us it ran out of quota.

    Weak prose patterns require a failed exit so that an agent merely *writing*
    about rate limits (editing this file, say) is not mistaken for hitting one.
    """
    low = tail.lower()
    if any(re.search(p, low) for p in CAP_STRONG):
        return True
    return returncode != 0 and any(re.search(p, low) for p in CAP_WEAK)


# Anchors that must appear near a timestamp before we believe it is a reset time.
_NEAR = r"(?:reset[st]?|resets? at|try again|retry|available again|back at|until)\b"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tzinfo(name: str | None):
    """Resolve 'UTC', '+01:00' or an IANA name. None means the local zone."""
    if not name:
        return None
    name = name.strip().strip("()[]").strip()
    if not name:
        return None
    up = name.upper()
    if up in ("UTC", "GMT", "Z"):
        return timezone.utc
    m = re.fullmatch(r"(?:UTC|GMT)?([+-])(\d{1,2}):?(\d{2})?", up)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return None  # unknown zone: fall back to local, which is what the user sees


def _next_wall_clock(hour: int, minute: int, tzname: str | None, now: datetime) -> datetime:
    """The next moment the wall clock reads hour:minute in the given zone."""
    tz = _tzinfo(tzname) or _now().astimezone().tzinfo
    local = now.astimezone(tz)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def parse_reset(text: str) -> datetime | None:
    """Pull a reset time out of an agent's cap message. None when unreadable.

    Never extrapolates. A cap with no stated reset time returns None, and None
    means ask the user — not "assume five hours".
    """
    low = text.lower()

    # 1. Unix epoch. Claude Code emits `usage limit reached|1753549200`.
    m = re.search(r"limit reached\s*\|\s*(\d{9,13})", low)
    if m:
        return _from_epoch(m.group(1))
    m = re.search(_NEAR + r"[^\n]{0,30}?\b(\d{10,13})\b", low)
    if m:
        return _from_epoch(m.group(1))

    # 2. ISO 8601.
    m = re.search(
        _NEAR + r"[^\n]{0,30}?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)",
        text,
        re.IGNORECASE,
    )
    if m:
        stamp = m.group(1).replace(" ", "T")
        if stamp[-1] in "Zz":
            stamp = stamp[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(stamp)
            # A zoneless stamp from an API means UTC. Being wrong here costs one
            # early retry, not a wrong overnight sleep.
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # 3. A duration: "try again in 4 hours 32 minutes", "retryDelay: 33s".
    d = _parse_duration(low)
    if d is not None:
        return _now() + timedelta(seconds=d)

    # 4. A wall-clock time: "resets at 3pm (UTC)", "reset at 15:00 Europe/London".
    # Searched against the original text so IANA zone names keep their case.
    m = re.search(
        _NEAR + r"[^\n]{0,30}?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
        r"(?:\s*\(?\s*([A-Za-z]+/[A-Za-z_+-]+|UTC|GMT|(?:UTC|GMT)?[+-]\d{1,2}:?\d{0,2})\s*\)?)?",
        text,
        re.IGNORECASE,
    )
    if m and (m.group(3) or m.group(2)):  # need am/pm or :mm — a bare "3" is not a time
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return _next_wall_clock(hour, minute, m.group(4), _now())

    return None


def _from_epoch(raw: str) -> datetime | None:
    value = int(raw)
    if value > 10**11:  # milliseconds
        value //= 1000
    dt = datetime.fromtimestamp(value, timezone.utc)
    # Sanity: a reset time is in the future and not next year.
    if _now() - timedelta(minutes=5) <= dt <= _now() + timedelta(days=7):
        return dt
    return None


_DUR_UNITS = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hour": 3600, "d": 86400, "day": 86400}


def _parse_duration(low: str) -> float | None:
    m = re.search(r"(?:try again|retry|resets?|wait|available)[^\n]{0,20}?\bin\b(.{0,40})", low)
    if not m:
        m = re.search(r"retry[_-]?(?:delay|after)\"?\s*[:=]\s*\"?(.{0,40})", low)
    if not m:
        m = re.search(r"retry-after\s*:\s*(.{0,20})", low)
    if not m:
        return None
    chunk = m.group(1)
    # No \b after the unit: "1h30m" must read as two parts, not one.
    parts = re.findall(
        r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?=\d|[^a-z0-9]|$)",
        chunk,
    )
    if parts:
        total = 0.0
        for value, unit in parts:
            unit = unit.rstrip("s") if unit not in ("s",) else unit
            unit = {"hr": "h", "min": "m", "sec": "s"}.get(unit, unit)
            total += float(value) * _DUR_UNITS.get(unit, _DUR_UNITS.get(unit[0], 0))
        if total > 0:
            return total
    bare = re.match(r"\s*(\d+(?:\.\d+)?)\s*$", chunk)  # `retry-after: 3600` is seconds
    return float(bare.group(1)) if bare else None


# --------------------------------------------------------------- ledger --


def state_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    d = base / "pengy"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ledger_path() -> Path:
    return state_dir() / "ledger.json"


def jobs_dir() -> Path:
    d = state_dir() / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_ledger() -> dict:
    try:
        return json.loads(ledger_path().read_text())
    except (OSError, ValueError):
        return {}


def write_quota(agent: str, state: str, resets_at: datetime | None) -> None:
    """Persist what we know. A restart must not forget that Claude is capped."""
    led = read_ledger()
    led[agent] = {
        "state": state,
        "resetsAt": resets_at.isoformat() if resets_at else None,
        "source": "parsed",
        "seenAt": _now().isoformat(),
    }
    tmp = ledger_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(led, indent=2))
    tmp.replace(ledger_path())


def quota_ok(agent: str) -> bool:
    entry = read_ledger().get(agent)
    if not entry or entry.get("state") != "capped":
        return True
    resets = entry.get("resetsAt")
    if not resets:
        return False
    try:
        return datetime.fromisoformat(resets) <= _now()
    except ValueError:
        return True


# --------------------------------------------------------------- notify --


def notify(title: str, body: str) -> None:
    """One line to the phone or desktop. Best effort — never fails a run."""
    url = os.environ.get("PENGY_NOTIFY_URL")
    if url:
        try:
            import urllib.request

            data = json.dumps({"title": title, "message": body, "content": f"{title}: {body}"}).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).close()
        except Exception:
            pass
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", f'display notification {json.dumps(body)} with title {json.dumps(title)}'],
                check=False, capture_output=True, timeout=10,
            )
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body], check=False, capture_output=True, timeout=10)
    except Exception:
        pass


# ------------------------------------------------------------- the loop --

PINK = "\033[95m"
DIM = "\033[2m"
OFF = "\033[0m"


def say(msg: str) -> None:
    tint = PINK if sys.stderr.isatty() else ""
    end = OFF if sys.stderr.isatty() else ""
    print(f"{tint}pengy{end} {msg}", file=sys.stderr, flush=True)


def run_agent(agent: str, prompt: str, cwd: Path, resume: bool, off_leash: bool,
              session: str | None = None) -> tuple[int, str]:
    """Run one turn. Echoes the agent's output live, returns (exitcode, tail)."""
    spec = AGENTS[agent]
    template = spec["resume"] if resume else spec["run"]
    names = spec["bin"]
    binary = agent_bin(agent) or (names if isinstance(names, str) else names[0])

    def sub(part: str) -> str:
        return {PROMPT: prompt, SESSION: session or "", DIR: str(cwd)}.get(part, part)

    cmd = [sub(part) for part in template]
    cmd[0] = binary  # every template leads with the binary; use the resolved path
    key = "off_leash" if off_leash else "leash"
    cmd += spec.get(f"{key}_resume", spec[key]) if resume else spec[key]
    use_stdin = resume and spec.get("resume_stdin")

    say(f"{DIM}$ {' '.join(cmd[:3])} …{OFF}" if sys.stderr.isatty() else f"$ {' '.join(cmd[:3])} …")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    if use_stdin:
        proc.stdin.write(prompt)
        proc.stdin.close()

    tail: deque[str] = deque(maxlen=TAIL_LINES)
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            tail.append(line)
    except KeyboardInterrupt:
        proc.terminate()
        raise
    return proc.wait(), "".join(tail)


def sleep_until(deadline: datetime, agent: str) -> None:
    """Wait out a cap. Absolute deadline, so a suspended laptop wakes up right."""
    target = deadline.timestamp()
    while True:
        left = target - time.time()
        if left <= 0:
            return
        mins = int(left // 60)
        local = deadline.astimezone().strftime("%H:%M")
        line = f"{agent} capped. Resuming at {local} — {mins//60}h {mins%60:02d}m to go."
        if sys.stderr.isatty():
            print(f"\r{PINK}pengy{OFF} {line}   ", end="", file=sys.stderr, flush=True)
            time.sleep(min(30, left))
        else:
            say(line)
            time.sleep(min(900, left))
    # ponytail: polls a wall clock every 30s. Fine for an overnight wait; if
    # sub-second resume ever matters, sleep the whole interval in one call.


def _run_job(args: argparse.Namespace) -> int:
    have = installed()
    if not have:
        say("no agent CLIs found. Install one of: " + ", ".join(AGENTS))
        return 127

    agent = args.agent or have[0]
    if agent not in AGENTS:
        say(f"unknown agent '{agent}'. Known: {', '.join(AGENTS)}")
        return 2
    if agent not in have:
        say(f"'{agent}' is not installed (looked for `{AGENTS[agent]['bin']}` on PATH)")
        return 127

    cwd = Path(args.dir).expanduser().resolve()
    if not cwd.is_dir():
        say(f"no such directory: {cwd}")
        return 2

    if args.off_leash and not args.yes and sys.stdin.isatty():
        if not (cwd / ".git").exists():
            say(f"off the leash in a non-git directory: {cwd}")
            say("nothing the agent breaks here is recoverable. Continue? [y/N] ")
            if input().strip().lower() not in ("y", "yes"):
                return 1

    if args.detach:
        print(detach(args, agent, cwd))
        return 0

    note = AGENTS[agent].get("leash_note")
    if note and not args.off_leash:
        say(f"note: {note}")

    prompt = args.prompt
    started = time.time()
    waits = 0
    resume = False
    session: str | None = None

    while True:
        # Some agents resume only by session id. If we never captured one, a
        # fresh run with the original prompt beats resuming into nothing.
        if resume and SESSION in AGENTS[agent]["resume"] and not session:
            say(f"no {agent} session id was captured — starting the job again instead.")
            resume, prompt = False, args.prompt

        code, tail = run_agent(agent, prompt, cwd, resume, args.off_leash, session)

        session_re = AGENTS[agent].get("session_re")
        if session_re:
            found = re.search(session_re, tail)
            if found:
                session = found.group(1)

        if not is_capped(tail, code):
            write_quota(agent, "ok", None)
            elapsed = int(time.time() - started)
            if code == 0:
                summary = f"{agent} finished in {elapsed//3600}h {elapsed//60%60:02d}m" + (
                    f" across {waits} cap{'s' if waits != 1 else ''}" if waits else ""
                )
                say(summary)
                notify("Pengy — done", summary)
            else:
                summary = f"{agent} exited {code}. Not a rate limit, so this one needs you."
                say(summary)
                notify("Pengy — stuck", summary)
            return code

        resets = parse_reset(tail)
        write_quota(agent, "capped", resets)

        if args.fallback and args.fallback != agent and quota_ok(args.fallback):
            if args.fallback in have:
                say(f"{agent} is capped — handing the job to {args.fallback} (fresh context).")
                notify("Pengy — switched agent", f"{agent} capped, {args.fallback} took over.")
                agent, resume, session = args.fallback, False, None
                prompt = args.prompt
                note = AGENTS[agent].get("leash_note")
                if note and not args.off_leash:
                    say(f"note: {note}")
                continue
            say(f"fallback '{args.fallback}' is not installed — ignoring it.")

        if resets is None:
            say(f"{agent} is capped and did not say when it resets.")
            say("Pengy does not guess reset times. Re-run when your window is back,")
            say("or pass --fallback <agent> to hand the job to one that still has budget.")
            notify("Pengy — capped", f"{agent} hit its limit and gave no reset time.")
            return 75  # EX_TEMPFAIL

        waits += 1
        if waits > args.max_waits:
            say(f"hit {args.max_waits} caps already — stopping rather than looping all week.")
            notify("Pengy — gave up", f"{agent} capped {waits} times.")
            return 75

        notify("Pengy — waiting", f"{agent} capped until {resets.astimezone():%H:%M}. Sleeping.")
        sleep_until(resets, agent)
        if sys.stderr.isatty():
            print(file=sys.stderr)
        write_quota(agent, "ok", None)
        say(f"window reset. Resuming {agent}.")
        prompt = RESUME_PROMPT
        resume = True


def do_run(args: argparse.Namespace) -> int:
    code = _run_job(args)
    if getattr(args, "job_id", None):
        patch_job(args.job_id, state="done", exit=code, finished=_now().isoformat())
    return code


# ------------------------------------------------------------- detached --


def job_meta(jid: str) -> dict:
    try:
        return json.loads((jobs_dir() / f"{jid}.json").read_text())
    except (OSError, ValueError):
        return {}


def patch_job(jid: str, **fields) -> None:
    meta = job_meta(jid)
    meta.update(fields)
    (jobs_dir() / f"{jid}.json").write_text(json.dumps(meta, indent=2))


def detach(args: argparse.Namespace, agent: str, cwd: Path) -> str:
    """Run the job in a process that outlives this terminal. Log goes to a file."""
    jid = f"{datetime.now():%m%d-%H%M}-{uuid.uuid4().hex[:4]}"
    cmd = [
        sys.executable, os.path.abspath(__file__), "run", args.prompt,
        "--agent", agent, "-C", str(cwd),
        "--max-waits", str(args.max_waits), "--job-id", jid, "--yes",
    ]
    if args.off_leash:
        cmd.append("--off-leash")
    if args.fallback:
        cmd += ["--fallback", args.fallback]

    log = (jobs_dir() / f"{jid}.log").open("w")
    # Detached so closing the terminal (or the MCP host quitting) doesn't kill it.
    spawn = {"creationflags": 0x00000008} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, **spawn)
    patch_job(jid, id=jid, agent=agent, dir=str(cwd), prompt=args.prompt,
              started=_now().isoformat(), pid=proc.pid, state="running")
    say(f"job {jid} running in the background ({agent}).")
    say(f"{DIM}pengy jobs   ·   tail -f {jobs_dir() / f'{jid}.log'}{OFF}")
    return jid


def do_jobs(_args: argparse.Namespace) -> int:
    metas = sorted((job_meta(p.stem) for p in jobs_dir().glob("*.json")),
                   key=lambda m: m.get("started", ""), reverse=True)
    if not metas:
        print("no jobs yet. `pengy run \"…\" --detach` starts one.")
        return 0
    for m in metas[:20]:
        state = m.get("state", "?")
        if state == "done":
            state = "done" if m.get("exit") == 0 else f"failed ({m.get('exit')})"
        elif not _alive(m.get("pid")):
            state = "gone"
        print(f"{m.get('id', '?'):<18} {state:<14} {m.get('agent', '?'):<12} {m.get('prompt', '')[:48]}")
    print(f"\nlogs in {jobs_dir()}")
    return 0


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)  # signal 0 only checks for existence
        return True
    except (OSError, TypeError):
        return False


# ------------------------------------------------------------------ mcp --

# One stdio MCP server, so every host that speaks MCP — Claude Code, Codex,
# Gemini CLI, Cursor — gets the same three tools with no per-host code.

MCP_TOOLS = [
    {
        "name": "pengy_quota",
        "description": "Which coding agents are installed on this machine, and which have "
                       "hit their usage cap. Check this before delegating long work.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "pengy_run",
        "description": "Hand a coding job to another agent in the background. Returns immediately "
                       "with a job id. Pengy waits out any usage cap and resumes the session, so "
                       "the job survives rate limits and outlives this conversation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What the agent should do."},
                "agent": {"type": "string", "enum": list(AGENTS), "description": "Defaults to the first installed."},
                "dir": {"type": "string", "description": "Working directory. Defaults to the current one."},
                "fallback": {"type": "string", "enum": list(AGENTS),
                             "description": "Hand the job here instead of waiting out a cap."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "pengy_jobs",
        "description": "Status of background jobs Pengy is running or has run.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _mcp_call(name: str, params: dict) -> str:
    if name == "pengy_quota":
        led = read_ledger()
        lines = []
        for a in AGENTS:
            if not agent_bin(a):
                continue
            entry = led.get(a, {})
            if entry.get("state") == "capped" and not quota_ok(a):
                lines.append(f"{a}: capped until {entry.get('resetsAt') or 'an unknown time'}")
            else:
                lines.append(f"{a}: ok")
        return "\n".join(lines) or "No agent CLIs are installed on this machine."

    if name == "pengy_jobs":
        metas = sorted((job_meta(p.stem) for p in jobs_dir().glob("*.json")),
                       key=lambda m: m.get("started", ""), reverse=True)[:20]
        return json.dumps(metas, indent=2) if metas else "No jobs."

    if name == "pengy_run":
        prompt = (params.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        agent = params.get("agent") or (installed() or [None])[0]
        if not agent:
            raise ValueError("No agent CLIs installed. Install claude, codex or gemini first.")
        if agent not in AGENTS:
            raise ValueError(f"Unknown agent {agent!r}. Known: {', '.join(AGENTS)}")
        cwd = Path(params.get("dir") or ".").expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError(f"No such directory: {cwd}")
        args = argparse.Namespace(
            prompt=prompt, agent=agent, dir=str(cwd), max_waits=4,
            fallback=params.get("fallback"), job_id=None,
            # An agent must not be able to grant itself its own bypass mode. Only
            # the human running the server can turn this on, from the environment.
            off_leash=os.environ.get("PENGY_MCP_ALLOW_OFF_LEASH") == "1",
        )
        jid = detach(args, agent, cwd)
        return (f"Started job {jid} on {agent} in {cwd}.\n"
                f"It survives usage caps and outlives this conversation. "
                f"Check it with pengy_jobs.")

    raise ValueError(f"Unknown tool: {name}")


def do_mcp(_args: argparse.Namespace) -> int:
    """Newline-delimited JSON-RPC 2.0 over stdio. Nothing else may touch stdout."""
    def reply(mid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": mid}
        msg["error" if error else "result"] = error or result
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        mid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
        if mid is None:
            continue  # a notification: nothing to answer
        try:
            if method == "initialize":
                reply(mid, {
                    "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "pengy", "version": __version__},
                })
            elif method == "tools/list":
                reply(mid, {"tools": MCP_TOOLS})
            elif method == "tools/call":
                text = _mcp_call(params.get("name", ""), params.get("arguments") or {})
                reply(mid, {"content": [{"type": "text", "text": text}]})
            elif method == "ping":
                reply(mid, {})
            else:
                reply(mid, error={"code": -32601, "message": f"Method not found: {method}"})
        except Exception as exc:  # a crashed tool must not kill the server
            reply(mid, error={"code": -32603, "message": str(exc)})
    return 0


# ----------------------------------------------------------------- misc --


def do_agents(_args: argparse.Namespace) -> int:
    led = read_ledger()
    have = installed()
    for name in AGENTS:
        path = agent_bin(name)
        entry = led.get(name, {})
        if not path:
            state = "not installed"
        elif entry.get("state") == "capped" and not quota_ok(name):
            resets = entry.get("resetsAt")
            when = datetime.fromisoformat(resets).astimezone().strftime("%H:%M") if resets else "unknown"
            state = f"capped until {when}"
        else:
            state = "ok"
        mark = "*" if have and name == have[0] else " "
        print(f"{mark} {name:<12} {state:<24} {path or ''}")
    if have:
        print("\n* default agent. Override with `pengy run --agent <name>`.")
    return 0


def do_doctor(_args: argparse.Namespace) -> int:
    print(f"pengy {__version__}  ·  python {sys.version.split()[0]}  ·  {sys.platform}")
    print(f"ledger    {ledger_path()}")
    print(f"notify    {'webhook + ' if os.environ.get('PENGY_NOTIFY_URL') else ''}"
          f"{'osascript' if sys.platform == 'darwin' else 'notify-send' if shutil.which('notify-send') else 'stderr only'}")
    have = installed()
    print(f"agents    {', '.join(have) if have else 'NONE FOUND — install claude, codex or gemini'}")
    sample = "Claude usage limit reached. Your limit will reset at 3pm (UTC)."
    got = parse_reset(sample)
    print(f"parser    {'ok' if got else 'FAILED'} — {sample!r} -> {got}")
    return 0 if have and got else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pengy",
        description="Keeps your AI coding agents working after they hit the rate limit.",
    )
    p.add_argument("--version", action="version", version=f"pengy {__version__}")
    subs = p.add_subparsers(dest="cmd")

    r = subs.add_parser("run", help="run a job, surviving usage caps")
    r.add_argument("prompt", help="what you want done")
    r.add_argument("-a", "--agent", help=f"one of: {', '.join(AGENTS)} (default: first installed)")
    r.add_argument("-C", "--dir", default=".", help="working directory (default: .)")
    r.add_argument("--off-leash", action="store_true", help="let the agent run commands unattended")
    r.add_argument("--fallback", help="hand the job to this agent instead of waiting out a cap")
    r.add_argument("--max-waits", type=int, default=4, help="stop after this many caps (default: 4)")
    r.add_argument("-y", "--yes", action="store_true", help="skip the off-leash confirmation")
    r.add_argument("-d", "--detach", action="store_true",
                   help="run in the background, outliving this terminal")
    r.add_argument("--job-id", help=argparse.SUPPRESS)  # set when re-invoked by --detach
    r.set_defaults(func=do_run)

    subs.add_parser("agents", help="which agents are installed, and their quota state").set_defaults(func=do_agents)
    subs.add_parser("jobs", help="background jobs, running and finished").set_defaults(func=do_jobs)
    subs.add_parser("mcp", help="run as an MCP server so other agents can use Pengy").set_defaults(func=do_mcp)
    subs.add_parser("doctor", help="check the install").set_defaults(func=do_doctor)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        say("stopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
