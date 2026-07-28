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
import base64
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

__version__ = "0.6.0"

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
    # What you typed is checked before what you have installed, so a typo is
    # reported as a typo rather than hiding behind "no agents found".
    if not args.prompt.strip():
        say("nothing to do — give me a job.")
        return 2

    cwd = Path(args.dir).expanduser().resolve()
    if not cwd.is_dir():
        say(f"no such directory: {cwd}")
        return 2

    if args.agent and args.agent not in AGENTS:
        say(f"unknown agent '{args.agent}'. Known: {', '.join(AGENTS)}")
        return 2
    if args.fallback and args.fallback not in AGENTS:
        say(f"unknown fallback '{args.fallback}'. Known: {', '.join(AGENTS)}")
        return 2

    have = installed()
    if not have:
        say("no agent CLIs found. Install one of: " + ", ".join(AGENTS))
        return 127

    agent = args.agent or have[0]
    if agent not in have:
        say(f"'{agent}' is not installed (looked for `{AGENTS[agent]['bin']}` on PATH)")
        return 127

    # Checked now rather than at 3am, when the cap arrives and the fallback
    # turns out to be a typo.
    if args.fallback:
        if args.fallback == agent:
            say(f"fallback and agent are both '{agent}' — that would hand the job to itself.")
            return 2
        if args.fallback not in have:
            say(f"fallback '{args.fallback}' is not installed, so a cap would just stop the job.")
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
    metas = sorted((m for m in (job_meta(p.stem) for p in jobs_dir().glob("*.json")) if m.get("id")),
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
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, we just may not signal it
    except (OSError, TypeError):
        return False


# ---------------------------------------------------------------- swarm --

# Several agents, one goal, at the same time. There is no conductor process:
# every agent reads the same board and claims its own work off it. The board is
# a directory of plain markdown, one file per agent, and an agent writes only
# its own — so two agents posting at the same moment cannot lose each other's
# lines, which a single shared file edited by tools would do silently at 3am.

SWARM_BRIEF = """You are `{me}`, one of {n} coding agents working this goal at the same time.
The others are: {peers}. Nobody is in charge. The board is how you stay out of
each other's way.

GOAL
{goal}

THE BOARD — .pengy/board/
Read it before you do anything, and whenever you want to know what the others are up to:

    cat .pengy/board/*.md

Write only your own lane, `.pengy/board/{me}.md`, and only by adding lines to the end.
You are the only writer of that file, so your normal file-editing tool is fine for it.
Never write another agent's lane: two writers on one file lose each other's lines
silently. Include the UTC date and time on every entry, or an overnight board sorts
into the wrong order. If you have a shell, this appends one:

    printf '%s\\n' "- $(date -u +'%Y-%m-%d %H:%M') CLAIM src/auth/* — moving to the new SDK" >> .pengy/board/{me}.md

POST
- CLAIM    before you touch anything. What you are taking, in one line. Keep it small.
- NOTE     anything the others must know: an interface you changed, a decision you made.
- BLOCKED  you need something another agent holds. Say what, then go do something else.
- DONE     a piece has landed.

RULES
Read the board first. If a claim overlaps yours, take something else — the earlier claim
wins. Stay inside what you claimed; do not refactor across someone else's area. If the
goal already looks finished, post that and stop rather than inventing work.
"""


def board_dir(cwd: Path) -> Path:
    d = cwd / ".pengy" / "board"
    d.mkdir(parents=True, exist_ok=True)
    ignore = d.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")  # the board is scratch, not source
    return d


def read_board(cwd: Path) -> str:
    """Every lane merged into one chronology. Read-only: the lanes are the truth,
    so there is nothing to regenerate and no writer to serialise against."""
    rows = []
    for lane in sorted((cwd / ".pengy" / "board").glob("*.md")):
        try:
            text = lane.read_text(errors="replace")
        except OSError:
            continue
        stamp = ""
        for line in text.splitlines():
            if not line.strip():
                continue
            # A dated stamp sorts as a string exactly as it sorts in time. Bare
            # HH:MM is still read, from boards written before dates were asked
            # for — but it is what makes an overnight swarm sort 00:15 above
            # 23:40, so the brief now asks for the date.
            found = re.match(r"\s*[-*]?\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}|\d{2}:\d{2})", line)
            if found:
                stamp = found.group(1)
            # A continuation line inherits its entry's time so it sorts underneath
            # it rather than floating to the top of the whole board.
            rows.append((stamp, lane.stem, line.strip()))
    rows.sort(key=lambda r: r[0])
    return "\n".join(f"{who:<11} {text}" for _, who, text in rows) or "(nothing on the board yet)"


def start_swarm(goal: str, agents: list[str], cwd: Path, off_leash: bool,
                max_waits: int = 4) -> list[str]:
    """Start one detached job per agent, all on `goal`. Returns the job ids."""
    goal = goal.strip()
    if not goal:
        raise ValueError("a swarm needs a goal.")
    if not cwd.is_dir():
        raise ValueError(f"no such directory: {cwd}")
    have = installed()
    agents = list(dict.fromkeys(agents or have))  # dedupe: one lane per agent
    for name in agents:
        if name not in AGENTS:
            raise ValueError(f"unknown agent {name!r}. Known: {', '.join(AGENTS)}")
        if name not in have:
            raise ValueError(f"'{name}' is not installed, so it cannot join the swarm.")
    if len(agents) < 2:
        raise ValueError("a swarm needs at least two installed agents — `pengy run` is the one-agent case.")

    board_dir(cwd)
    ids = []
    for name in agents:
        # A capped agent is started anyway: its job waits out the window and joins
        # the swarm late, which is the whole point of Pengy being underneath this.
        brief = SWARM_BRIEF.format(me=name, n=len(agents), goal=goal,
                                   peers=", ".join(a for a in agents if a != name))
        job = argparse.Namespace(prompt=brief, off_leash=off_leash,
                                 max_waits=max_waits, fallback=None)
        jid = detach(job, name, cwd)
        patch_job(jid, prompt=goal, lane=name)  # the job list shows the goal, not the brief
        ids.append(jid)
    return ids


def do_swarm(args: argparse.Namespace) -> int:
    cwd = Path(args.dir).expanduser().resolve()
    if args.off_leash and not args.yes and sys.stdin.isatty() and not (cwd / ".git").exists():
        say(f"off the leash in a non-git directory: {cwd}")
        say("every agent in the swarm can run commands here, at once. Continue? [y/N] ")
        if input().strip().lower() not in ("y", "yes"):
            return 1
    try:
        ids = start_swarm(args.prompt, [a.strip() for a in (args.agents or "").split(",") if a.strip()],
                          cwd, args.off_leash, args.max_waits)
    except ValueError as exc:
        say(str(exc))
        return 2
    say(f"{len(ids)} agents on the same goal. They coordinate through {cwd / '.pengy/board'}")
    say(f"{DIM}pengy board -f -C {cwd}{OFF}")
    return 0


def do_board(args: argparse.Namespace) -> int:
    cwd = Path(args.dir).expanduser().resolve()
    while True:
        text = read_board(cwd)
        if args.follow and sys.stdout.isatty():
            print("\033[H\033[2J", end="")
        print(text, flush=True)
        if not args.follow:
            return 0
        time.sleep(3)


# ------------------------------------------------------------------ mcp --

# One stdio MCP server, so every host that speaks MCP — Claude Code, Codex,
# Gemini CLI, Cursor — gets the same five tools with no per-host code.

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
    {
        "name": "pengy_swarm",
        "description": "Put several coding agents on the same goal at once, in the background. "
                       "They coordinate by claiming work on a shared board rather than being "
                       "assigned it, so no plan is needed up front. Each one survives its own "
                       "usage cap independently. Read progress with pengy_board.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The goal. Every agent gets this one."},
                "agents": {"type": "array", "items": {"type": "string", "enum": list(AGENTS)},
                           "description": "Defaults to every installed agent."},
                "dir": {"type": "string", "description": "Working directory. Defaults to the current one."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "pengy_board",
        "description": "The swarm's shared board for a directory: who claimed what, what landed, "
                       "what is blocked. Every agent's lane, merged into one chronology.",
        "inputSchema": {
            "type": "object",
            "properties": {"dir": {"type": "string", "description": "Working directory. Defaults to the current one."}},
        },
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
        metas = sorted((m for m in (job_meta(p.stem) for p in jobs_dir().glob("*.json")) if m.get("id")),
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

    if name == "pengy_board":
        return read_board(Path(params.get("dir") or ".").expanduser().resolve())

    if name == "pengy_swarm":
        cwd = Path(params.get("dir") or ".").expanduser().resolve()
        ids = start_swarm(
            params.get("prompt") or "", params.get("agents") or [], cwd,
            # Same rule as pengy_run, and it matters more here: a swarm off the
            # leash is several bypass-mode agents in one directory at once.
            off_leash=os.environ.get("PENGY_MCP_ALLOW_OFF_LEASH") == "1",
        )
        return (f"Swarm of {len(ids)} started in {cwd} — jobs {', '.join(ids)}.\n"
                f"They claim work off .pengy/board/ as they go. Read it with pengy_board.")

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


# ------------------------------------------------------------------ ui --

# `pengy chat` is a local page, not a terminal. Browser rather than tkinter:
# python3-tk is a separate package on most Linux distributions, and "zero
# dependencies" has to stay true. This also inherits the brand from the site.

# One face everywhere: the same artwork is the favicon, the window, the app
# menu icon and the thing floating over your desktop. It lives base64 at the
# foot of this file so `pengy.py` is still one file with nothing to fetch.


def icon_png(small: bool = False) -> bytes:
    return base64.b64decode(PENGY_ICON_SMALL if small else PENGY_ICON)


def icon_img(cls: str = "peng") -> str:
    return f'<img class="{cls}" src="/pengy.png" alt="Pengy">'

CHAT_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pengy</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0b090d;--panel:#100e12;--raise:#171319;--raise2:#1c171f;--line:#2b242e;
  --line2:#3a3040;--ink:#f8f3f6;--muted:#a69aa5;--dim:#756b76;
  --pink:#ff3dbe;--soft:#ff8ad9;--gold:#f5c542;--green:#60d394;--danger:#ff7a8a;
  --mono:ui-monospace,"SFMono-Regular",Menlo,Consolas,monospace;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif
}
html,body{height:100%}
body{background:
  radial-gradient(900px 620px at 76% -15%,rgba(255,61,190,.11),transparent 62%),var(--bg);
  color:var(--ink);font:15px/1.5 var(--sans);overflow:hidden;-webkit-font-smoothing:antialiased}
button,input,select,textarea{font:inherit}
button{color:inherit}
.topbar{height:66px;display:flex;align-items:center;gap:.8rem;padding:0 1.2rem;border-bottom:1px solid var(--line);
  background:rgba(11,9,13,.86);backdrop-filter:blur(18px);position:relative;z-index:20}
.topbar .peng{width:40px;height:40px;flex:none}
.brand{display:flex;flex-direction:column;line-height:1.08}
.name{font-weight:850;letter-spacing:-.035em;font-size:1.08rem}
.tagline{color:var(--dim);font-size:.72rem;margin-top:.22rem}
.top-actions{margin-left:auto;display:flex;align-items:center;gap:.6rem}
.local{font:650 .67rem/1 var(--mono);letter-spacing:.07em;text-transform:uppercase;color:var(--dim);
  padding:.45rem .6rem;border:1px solid var(--line);border-radius:7px}
.status{display:flex;align-items:center;gap:.5rem;min-height:34px;padding:.45rem .72rem;border:1px solid var(--line);
  border-radius:999px;background:var(--raise);font:650 .69rem/1 var(--mono);letter-spacing:.055em;text-transform:uppercase;color:var(--muted)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--dim);box-shadow:0 0 0 4px rgba(117,107,118,.08)}
.dot.work{background:var(--pink);box-shadow:0 0 0 4px rgba(255,61,190,.11)}
.dot.cap{background:var(--gold);box-shadow:0 0 0 4px rgba(245,197,66,.1)}
.dot.ready{background:var(--green);box-shadow:0 0 0 4px rgba(96,211,148,.1)}
.panel-toggle{display:none;border:1px solid var(--line);background:var(--raise);border-radius:9px;width:36px;height:36px;cursor:pointer}
.shell{height:calc(100% - 66px);display:grid;grid-template-columns:284px minmax(0,1fr)}
.sidebar{background:linear-gradient(180deg,rgba(23,19,25,.72),rgba(16,14,18,.96));border-right:1px solid var(--line);
  min-width:0;padding:1.25rem 1rem;display:flex;flex-direction:column;gap:1.35rem;overflow-y:auto}
.side-section{display:grid;gap:.6rem}
.side-head{display:flex;align-items:center;justify-content:space-between;padding:0 .25rem}
.side-title{font:700 .66rem/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
.side-count{font:650 .66rem/1 var(--mono);color:var(--dim)}
.agent-list,.job-list{display:grid;gap:.42rem}
.agent-row{display:grid;grid-template-columns:8px minmax(0,1fr) auto;align-items:center;gap:.62rem;padding:.67rem .7rem;
  border:1px solid transparent;border-radius:10px;color:var(--muted)}
.agent-row.selected{border-color:var(--line);background:var(--raise)}
.agent-row.missing{opacity:.42}
.agent-dot{width:7px;height:7px;border-radius:50%;background:var(--dim)}
.agent-dot.ready{background:var(--green)}.agent-dot.capped{background:var(--gold)}
.agent-name{font-weight:680;color:var(--ink);text-transform:capitalize}
.agent-state{font:600 .66rem/1 var(--mono);color:var(--dim);white-space:nowrap}
.agent-state.capped{color:var(--gold)}
.job{width:100%;text-align:left;background:transparent;border:1px solid transparent;border-radius:11px;
  padding:.68rem .7rem;cursor:pointer;transition:.14s ease}
.job:hover,.job.active{background:var(--raise);border-color:var(--line)}
.job-top{display:flex;align-items:center;gap:.45rem;margin-bottom:.25rem}
.job-agent{font:700 .63rem/1 var(--mono);letter-spacing:.07em;text-transform:uppercase;color:var(--soft)}
.job-time{margin-left:auto;color:var(--dim);font-size:.68rem}
.job-prompt{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);font-size:.81rem}
.job-state{width:6px;height:6px;border-radius:50%;background:var(--dim)}
.job-state.running{background:var(--pink)}.job-state.done{background:var(--green)}.job-state.failed,.job-state.gone{background:var(--danger)}
.empty-side{color:var(--dim);font-size:.8rem;padding:.5rem .7rem}
.privacy{margin-top:auto;padding:.85rem;border:1px solid var(--line);border-radius:12px;color:var(--dim);font-size:.75rem;
  background:rgba(11,9,13,.45)}
.privacy strong{display:block;color:var(--muted);font-size:.78rem;margin-bottom:.22rem}
.privacy svg{width:13px;height:13px;vertical-align:-2px;margin-right:.3rem}
/* min-height:0 on both, or the flex item refuses to shrink below its content:
   #log grows with the transcript, pushes the composer off the bottom, and
   body{overflow:hidden} means there is no page scroll to rescue it. */
.main{min-width:0;min-height:0;display:flex;flex-direction:column;position:relative}
#log{flex:1;min-height:0;overflow-y:auto;padding:clamp(1.3rem,4vw,3.2rem) clamp(1rem,4vw,3rem) 2rem;
  scroll-behavior:smooth;scrollbar-color:var(--line2) transparent}
.log-inner{width:100%;max-width:840px;margin-inline:auto;display:flex;flex-direction:column;gap:1rem;min-height:100%}
.welcome{margin:auto;padding:2rem 0 3rem;width:100%;max-width:720px}
.welcome-mark{width:70px;height:70px;display:grid;place-items:center;border:1px solid rgba(255,61,190,.24);
  border-radius:22px;background:linear-gradient(145deg,rgba(255,61,190,.15),rgba(255,61,190,.03));box-shadow:0 20px 70px rgba(255,61,190,.08)}
.welcome-mark .peng{width:58px;height:58px}
.welcome h1{font-size:clamp(2rem,5vw,3.1rem);line-height:1;letter-spacing:-.055em;margin:1.45rem 0 .85rem;max-width:560px}
.welcome-copy{color:var(--muted);font-size:clamp(.98rem,2vw,1.08rem);max-width:570px}
.suggest-label{margin-top:2rem;font:700 .65rem/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
.suggestions{display:grid;grid-template-columns:repeat(3,1fr);gap:.65rem;margin-top:.75rem}
.suggestion{text-align:left;min-height:92px;padding:.85rem .9rem;background:var(--raise);border:1px solid var(--line);
  border-radius:13px;color:var(--muted);cursor:pointer;transition:transform .14s ease,border-color .14s ease,color .14s ease}
.suggestion:hover{transform:translateY(-2px);border-color:rgba(255,61,190,.48);color:var(--ink)}
.suggestion b{display:block;color:var(--ink);font-size:.83rem;margin-bottom:.28rem}
.suggestion span{font-size:.78rem;line-height:1.35}
.turn{display:flex;gap:.7rem;max-width:min(720px,100%);align-items:flex-start}
.turn.me{align-self:flex-end;flex-direction:row-reverse}
.av{width:32px;height:32px;flex:none;border-radius:10px;overflow:hidden}
.turn.me .av{background:var(--line);display:grid;place-items:center;color:var(--muted);font-size:.62rem;font-weight:750;text-transform:uppercase}
.bub{background:var(--raise);border:1px solid var(--line);border-radius:5px 16px 16px 16px;padding:.76rem 1rem;
  white-space:pre-wrap;word-break:break-word;color:var(--muted)}
.turn:not(.me) .bub::first-line{color:var(--ink)}
.turn.me .bub{color:var(--ink);background:linear-gradient(160deg,rgba(255,61,190,.16),transparent),var(--raise);
  border-color:rgba(255,61,190,.32);border-radius:16px 5px 16px 16px}
.opts{display:flex;flex-wrap:wrap;gap:.45rem;margin-top:.75rem}
.opt{background:transparent;border:1px solid var(--line2);color:var(--muted);border-radius:999px;
  padding:.38rem .82rem;font-size:.8rem;cursor:pointer}
.opt:hover{border-color:var(--pink);color:var(--ink)}
.opt.on{border-color:var(--pink);color:var(--soft);background:rgba(255,61,190,.1)}
.progress{margin-top:.8rem;min-width:min(560px,65vw);border:1px solid var(--line);border-radius:11px;background:#0b090d;overflow:hidden}
.progress summary{list-style:none;display:flex;align-items:center;gap:.55rem;padding:.62rem .75rem;color:var(--dim);
  font:650 .69rem/1 var(--mono);cursor:pointer;user-select:none}
.progress summary::-webkit-details-marker{display:none}
.progress summary::before{content:"›";font-size:1rem;transition:transform .15s}
.progress[open] summary::before{transform:rotate(90deg)}
.live{width:6px;height:6px;background:var(--pink);border-radius:50%;margin-left:auto;animation:pulse 1.8s ease-in-out infinite}
pre.out{font-family:var(--mono);font-size:.72rem;color:#a89ba7;border-top:1px solid var(--line);padding:.75rem;
  max-height:250px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.nap{position:relative;display:flex;align-items:center;gap:1.15rem;margin-top:.85rem;padding:1.1rem 1.2rem;
  border:1px solid rgba(245,197,66,.3);background:linear-gradient(150deg,rgba(245,197,66,.09),transparent 72%),#0e0c12;border-radius:14px}
.nap .peng{width:62px;height:62px;flex:none}
.napt{font-size:.93rem;font-weight:680;color:var(--ink)}
.napclock{font:700 1.7rem/1.15 var(--mono);letter-spacing:-.04em;color:var(--gold);margin:.16rem 0}
.naps{color:var(--muted);font-size:.78rem}
.zzz{position:absolute;left:68px;top:1px;font-family:var(--mono);font-size:.9rem;color:var(--gold)}
.zzz span{position:absolute;opacity:0;animation:rise 3s linear infinite}
.zzz span:nth-child(2){animation-delay:1s;left:8px}.zzz span:nth-child(3){animation-delay:2s;left:16px}
.composer-shell{flex:none;padding:.8rem clamp(.8rem,3vw,1.6rem) 1rem;background:linear-gradient(180deg,transparent,rgba(11,9,13,.98) 24%)}
.composer{width:100%;max-width:880px;margin-inline:auto;border:1px solid var(--line2);border-radius:17px;
  background:rgba(23,19,25,.96);box-shadow:0 16px 50px rgba(0,0,0,.28);overflow:hidden}
.runbar{display:flex;align-items:center;gap:.55rem;padding:.58rem .72rem;border-bottom:1px solid var(--line)}
.field{display:flex;align-items:center;gap:.4rem;min-width:0}
.field.folder{flex:1}
.field label{font:700 .62rem/1 var(--mono);letter-spacing:.07em;text-transform:uppercase;color:var(--dim)}
select,input[type=text]{background:transparent;color:var(--muted);border:0;min-width:0;padding:.25rem .2rem;font-size:.78rem}
select{max-width:118px;text-transform:capitalize}
input[type=text]{width:100%;font-family:var(--mono);text-overflow:ellipsis}
/* Two states, and which one you are in has to be readable at a glance: green
   dot and "Leash on" is the safe default, gold and "Leash off" is not. */
.leash{white-space:nowrap;margin-left:auto;display:flex;align-items:center;gap:.42rem;
  border-color:rgba(96,211,148,.4);color:var(--green)}
.leash::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--green);flex:none}
.leash:hover{border-color:var(--green);color:var(--green)}
.leash.off{border-color:rgba(245,197,66,.55);color:var(--gold);background:rgba(245,197,66,.1)}
.leash.off::before{background:var(--gold);box-shadow:0 0 0 3px rgba(245,197,66,.16)}
.leash.off:hover{border-color:var(--gold);color:var(--gold)}
.compose{display:flex;gap:.65rem;align-items:flex-end;padding:.72rem}
textarea{flex:1;background:transparent;color:var(--ink);border:0;padding:.38rem .3rem;font:inherit;resize:none;min-height:46px;max-height:160px}
textarea::placeholder{color:var(--dim)}
textarea:focus,select:focus,input:focus{outline:none}
.send{display:flex;align-items:center;gap:.45rem;background:var(--pink);color:#180510;border:0;border-radius:11px;
  padding:.72rem 1rem;font-weight:780;cursor:pointer;flex:none;transition:transform .14s ease,background .14s ease}
.send:hover{background:var(--soft);transform:translateY(-1px)}
.send:disabled{opacity:.42;cursor:not-allowed;transform:none}
.send svg{width:14px;height:14px}
.hint{color:var(--dim);font-size:.68rem;padding:0 .95rem .68rem}
.peng{display:block;object-fit:contain}
.av .peng{width:100%;height:100%}
[data-mood=working] .peng{animation:bob 1.5s ease-in-out infinite;transform-origin:50% 90%}
[data-mood=sleeping] .peng{animation:breathe 3.6s ease-in-out infinite;transform-origin:50% 95%;
  filter:saturate(.5) brightness(.72)}
@keyframes bob{0%,100%{transform:translateY(0) rotate(0)}50%{transform:translateY(-2px) rotate(-2.5deg)}}
@keyframes breathe{0%,100%{transform:scale(1)}50%{transform:scale(1.045)}}
@keyframes pulse{0%,100%{opacity:.35}50%{opacity:1}}
@keyframes rise{0%{opacity:0;transform:translateY(6px) scale(.7)}25%{opacity:.95}100%{opacity:0;transform:translateY(-26px) scale(1.25)}}
@media(max-width:900px){.shell{grid-template-columns:245px minmax(0,1fr)}.sidebar{padding-inline:.75rem}.suggestions{grid-template-columns:1fr}.suggestion{min-height:0}}
@media(max-width:700px){
  .topbar{height:60px;padding:0 .85rem}.topbar .peng{width:34px;height:34px}.tagline,.local{display:none}.panel-toggle{display:block}
  .shell{height:calc(100% - 60px);display:block}.sidebar{display:none;position:fixed;z-index:15;inset:60px 0 0 auto;width:min(310px,88vw);
    box-shadow:-24px 0 70px rgba(0,0,0,.45);border-left:1px solid var(--line);border-right:0}
  body.panel-open .sidebar{display:flex}.main{height:100%}.welcome{padding-top:1rem}.welcome-mark{width:58px;height:58px}.welcome-mark .peng{width:47px;height:47px}
  #log{padding-top:1.3rem}.runbar{flex-wrap:wrap}.field.folder{order:3;flex-basis:100%;border-top:1px solid var(--line);padding-top:.45rem}
  .progress{min-width:0;width:100%}.turn{max-width:100%}.bub{max-width:calc(100vw - 4.5rem)}
  .send span{display:none}.send{width:42px;height:42px;display:grid;place-items:center;padding:0;border-radius:50%}.hint{display:none}
}
/* Taking the leash off is the one genuinely dangerous thing here, so it gets a
   real dialog rather than a browser confirm() stamped "127.0.0.1 says". */
/* margin:auto is what centres a modal dialog, and the *{margin:0} reset above
   strips the UA default — without this it pins to the top-left corner. */
dialog.sheet{border:0;padding:0;margin:auto;background:transparent;color:var(--ink);
  width:min(460px,92vw);max-height:90vh}
dialog.sheet::backdrop{background:rgba(6,4,8,.72);backdrop-filter:blur(4px)}
.sheet-in{border:1px solid var(--line2);border-radius:18px;padding:1.5rem;
  background:linear-gradient(165deg,rgba(245,197,66,.09),transparent 58%),var(--raise);
  box-shadow:0 30px 90px rgba(0,0,0,.55)}
.sheet-mark{width:44px;height:44px;border-radius:13px;display:grid;place-items:center;font-size:1.35rem;
  border:1px solid rgba(245,197,66,.36);background:rgba(245,197,66,.1);margin-bottom:1rem}
.sheet h2{font-size:1.3rem;font-weight:800;letter-spacing:-.03em;margin-bottom:.55rem}
.sheet p{color:var(--muted);font-size:.92rem;margin-bottom:.7rem}
.sheet ul{list-style:none;display:grid;gap:.4rem;margin:0 0 1.15rem}
.sheet li{color:var(--muted);font-size:.86rem;display:flex;gap:.55rem}
.sheet li::before{content:"—";color:var(--gold);flex:none}
.sheet-note{color:var(--dim);font-size:.79rem;margin-bottom:1.25rem}
.sheet-actions{display:flex;gap:.6rem;justify-content:flex-end}
.sheet button{border:1px solid var(--line2);background:var(--raise2);border-radius:10px;
  padding:.62rem 1rem;font-weight:700;font-size:.87rem;cursor:pointer}
.sheet button:hover{border-color:var(--dim)}
.sheet .go{background:var(--gold);border-color:var(--gold);color:#1a1204}
.sheet .go:hover{filter:brightness(1.08)}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;animation:none!important;transition:none!important}}
</style></head><body data-mood="idle">
<dialog class="sheet" id="leash-sheet">
  <form method="dialog" class="sheet-in">
    <div class="sheet-mark" aria-hidden="true">⚠</div>
    <h2>Take Pengy off the leash?</h2>
    <p>The agent gets its own bypass mode for this job. In that directory it can:</p>
    <ul>
      <li>run any command, without asking first</li>
      <li>install packages and change files it was not pointed at</li>
      <li>keep doing both while you are asleep</li>
    </ul>
    <p class="sheet-note">Pengy is not a sandbox — it passes the flag through to the agent. Use a directory under git.</p>
    <div class="sheet-actions">
      <button value="cancel" autofocus>Keep the leash on</button>
      <button value="go" class="go">Take it off</button>
    </div>
  </form>
</dialog>
<header class="topbar">
  __SVG__
  <div class="brand"><div class="name">Pengy</div><div class="tagline">Managing your agents, while you're away xo</div></div>
  <div class="top-actions">
    <span class="local">local only</span>
    <div class="status" role="status"><span class="dot ready" id="dot"></span><span id="st">getting ready</span></div>
    <button class="panel-toggle" id="panel-toggle" aria-label="Open activity panel" aria-expanded="false">☷</button>
  </div>
</header>
<div class="shell">
  <aside class="sidebar" id="sidebar">
    <section class="side-section">
      <div class="side-head"><h2 class="side-title">Agents</h2><span class="side-count" id="agent-count">—</span></div>
      <div class="agent-list" id="agents"></div>
    </section>
    <section class="side-section">
      <div class="side-head"><h2 class="side-title">Recent work</h2><span class="side-count" id="job-count">0</span></div>
      <div class="job-list" id="jobs"><div class="empty-side">No jobs yet.</div></div>
    </section>
    <div class="privacy">
      <strong>
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 7V5a3.5 3.5 0 0 1 7 0v2M3 7h10v7H3z" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>
        Stays on this machine
      </strong>
      This page is served from 127.0.0.1. Pengy has no account, cloud, or telemetry.
    </div>
  </aside>
  <main class="main">
    <div id="log" aria-live="polite">
      <div class="log-inner" id="log-inner">
        <section class="welcome" id="welcome">
          <div class="welcome-mark">__SVG__</div>
          <h1>What should run while you're away?</h1>
          <p class="welcome-copy">Give Pengy an outcome. It will run the agent, wait through usage caps, and only interrupt when the work genuinely needs you.</p>
          <div class="suggest-label">Try a job like</div>
          <div class="suggestions">
            <button class="suggestion" data-prompt="Fix the failing tests and keep the suite green.">
              <b>Get the build green</b><span>Fix the failing tests and keep the suite green.</span>
            </button>
            <button class="suggestion" data-prompt="Finish the checkout flow, including validation and tests.">
              <b>Finish a feature</b><span>Complete the checkout flow, validation, and tests.</span>
            </button>
            <button class="suggestion" data-prompt="Review this repository and improve the highest-impact issue you find.">
              <b>Improve this project</b><span>Audit the repo and act on the biggest useful change.</span>
            </button>
          </div>
        </section>
      </div>
    </div>
    <div class="composer-shell">
      <div class="composer">
        <div class="runbar">
          <div class="field"><label for="agent">Agent</label><select id="agent" aria-label="Agent"></select></div>
          <div class="field folder"><label for="dir">Folder</label><input type="text" id="dir" aria-label="Working folder" spellcheck="false"></div>
          <button class="opt leash" id="leash" aria-pressed="false" title="Leash on: the agent edits files but keeps its normal safety checks. Click to take it off.">Leash on</button>
        </div>
        <div class="compose">
          <textarea id="msg" aria-label="Job description" placeholder="Describe the outcome you want…" rows="2"></textarea>
          <button class="send" id="send">
            <span>Start job</span>
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 8h11M9 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
        </div>
        <div class="hint">Enter to start · Shift+Enter for a new line · jobs keep running if you close this window</div>
      </div>
    </div>
  </main>
</div>
<script>
const KEY = new URLSearchParams(location.search).get('k') || '';
const $ = s => document.querySelector(s);
const AV = `__SVG__`;
let offLeash = false, current = null, watchSequence = 0, appState = null;

async function api(path, options={}) {
  const response = await fetch(path, {
    ...options,
    headers:{'X-Pengy-Key':KEY,'Content-Type':'application/json',...(options.headers||{})}
  });
  if (!response.ok) throw new Error(`Pengy returned ${response.status}`);
  return response.json();
}

function hideWelcome() {
  const welcome = $('#welcome');
  if (welcome) welcome.remove();
}

function say(text, opts) {
  hideWelcome();
  const t = document.createElement('div'); t.className = 'turn';
  t.innerHTML = `<div class="av">${AV}</div><div class="bub"></div>`;
  t.querySelector('.bub').append(...render(text));
  if (opts) {
    const row = document.createElement('div'); row.className = 'opts';
    opts.forEach(o => { const b = document.createElement('button'); b.className='opt'; b.textContent=o.label;
      b.onclick = () => { row.remove(); o.run(); }; row.appendChild(b); });
    t.querySelector('.bub').appendChild(row);
  }
  $('#log-inner').appendChild(t); scroll(); return t;
}
function render(text){ const f=document.createDocumentFragment(); f.append(document.createTextNode(text)); return [f]; }
function me(text) {
  hideWelcome();
  const t = document.createElement('div'); t.className = 'turn me';
  t.innerHTML = `<div class="av">you</div><div class="bub"></div>`;
  t.querySelector('.bub').textContent = text; $('#log-inner').appendChild(t); scroll();
}
function scroll(){ $('#log').scrollTop = $('#log').scrollHeight; }
function setDot(cls, label) {
  $('#dot').className = 'dot ' + cls;
  $('#st').textContent = label;
  document.body.dataset.mood = cls === 'work' ? 'working' : cls === 'cap' ? 'sleeping' : 'idle';
}
function shortTime(stamp) {
  if (!stamp) return '';
  const date = new Date(stamp);
  const today = new Date().toDateString() === date.toDateString();
  return today ? date.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})
    : date.toLocaleDateString([], {month:'short',day:'numeric'});
}
function jobStatus(job) {
  if (job.state === 'running') return 'running';
  if (job.state === 'gone') return 'gone';
  return job.exit === 0 ? 'done' : 'failed';
}
function renderAgents(state) {
  const installed = state.agents.filter(a => a.installed);
  const available = installed.filter(a => !a.capped);
  const selected = $('#agent').value || localStorage.getItem('pengy-agent') || installed[0]?.name;
  $('#agent-count').textContent = installed.length === available.length
    ? `${available.length} ready` : `${available.length}/${installed.length} ready`;
  $('#agents').replaceChildren(...state.agents.map(agent => {
    const row = document.createElement('div');
    const active = agent.name === selected;
    row.className = `agent-row${active?' selected':''}${agent.installed?'':' missing'}`;
    const dot = document.createElement('span');
    dot.className = `agent-dot ${agent.installed ? (agent.capped?'capped':'ready') : ''}`;
    const name = document.createElement('span'); name.className = 'agent-name'; name.textContent = agent.name;
    const status = document.createElement('span');
    status.className = `agent-state${agent.capped?' capped':''}`;
    status.textContent = !agent.installed ? 'not found' : agent.capped ? (agent.until || 'capped') : 'ready';
    row.append(dot,name,status);
    if (agent.installed) row.onclick = () => {
      $('#agent').value = agent.name; localStorage.setItem('pengy-agent',agent.name); renderAgents(appState);
      document.body.classList.remove('panel-open'); $('#panel-toggle').setAttribute('aria-expanded','false');
    };
    return row;
  }));
  const keep = installed.some(a => a.name === selected) ? selected : installed[0]?.name;
  $('#agent').innerHTML = installed.map(a => `<option value="${a.name}">${a.name}${a.capped?' · capped':''}</option>`).join('')
    || '<option value="">none found</option>';
  if (keep) $('#agent').value = keep;
}
function renderJobs(state) {
  const jobs = state.jobs || [];
  $('#job-count').textContent = jobs.length;
  if (!jobs.length) {
    const empty = document.createElement('div'); empty.className='empty-side'; empty.textContent='No jobs yet.';
    $('#jobs').replaceChildren(empty); return;
  }
  $('#jobs').replaceChildren(...jobs.map(job => {
    const button = document.createElement('button');
    button.className = `job${job.id===current?' active':''}`;
    const top = document.createElement('span'); top.className='job-top';
    const stateDot = document.createElement('span'); stateDot.className=`job-state ${jobStatus(job)}`;
    const agent = document.createElement('span'); agent.className='job-agent'; agent.textContent=job.agent || 'agent';
    const time = document.createElement('span'); time.className='job-time'; time.textContent=shortTime(job.started);
    const prompt = document.createElement('span'); prompt.className='job-prompt'; prompt.textContent=job.prompt || 'Untitled job';
    top.append(stateDot,agent,time); button.append(top,prompt);
    button.title = job.prompt || '';
    button.onclick = () => {
      watch(job.id, job);
      document.body.classList.remove('panel-open'); $('#panel-toggle').setAttribute('aria-expanded','false');
    };
    return button;
  }));
}
function renderState(state) {
  appState = state;
  renderAgents(state);
  renderJobs(state);
}
function nap(agent, resetsAt) {
  const card = document.createElement('div'); card.className = 'nap';
  card.innerHTML = `<div class="zzz"><span>z</span><span>z</span><span>z</span></div>${AV}
    <div><div class="napt">${agent} is capped. I'll wait.</div>
    <div class="napclock">—</div><div class="naps"></div></div>`;
  const clock = card.querySelector('.napclock'), sub = card.querySelector('.naps');
  const when = new Date(resetsAt);
  sub.textContent = 'resuming at ' + when.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  const tick = () => {
    let left = Math.max(0, (when - new Date()) / 1000);
    if (!left) { clock.textContent = 'now'; return; }
    const h = Math.floor(left/3600), m = Math.floor(left%3600/60), sec = Math.floor(left%60);
    clock.textContent = (h ? h + 'h ' : '') + m + 'm ' + String(sec).padStart(2,'0') + 's';
    card.dataset.t = setTimeout(tick, 1000);
  };
  tick(); return card;
}

async function boot() {
  try {
    const state = await api('/api/state');
    const savedDir = localStorage.getItem('pengy-dir');
    $('#dir').value = savedDir || state.cwd;
    renderState(state);
    const ready = state.agents.filter(a=>a.installed);
    if (!ready.length) {
      say("I can't find an agent CLI yet. Install Claude Code, Codex, Gemini, OpenCode, Kimi, Antigravity or Droid, then reopen Pengy.");
      $('#send').disabled = true; setDot('idle','no agents found'); return;
    }
    const available = ready.filter(agent => !agent.capped);
    setDot(available.length ? 'ready' : 'cap', available.length
      ? `${available.length} agent${available.length===1?'':'s'} ready`
      : 'all agents capped');
    const running = state.jobs.find(j=>j.state==='running');
    if (running) {
      say(`A job from earlier is still running with ${running.agent}.`, [{label:'Open live view',run:()=>watch(running.id,running)}]);
    }
  } catch (error) {
    say(`I couldn't reach the local Pengy service. ${error.message}`);
    setDot('idle','disconnected'); $('#send').disabled = true;
  }
}

async function send() {
  const text = $('#msg').value.trim(); if (!text) return;
  const agent = $('#agent').value;
  if (!agent) { say('Choose an installed agent first.'); return; }
  me(text);
  $('#msg').value = ''; $('#send').disabled = true; $('#send span').textContent = 'Starting';
  setDot('work','starting job');
  const body = { prompt:text, agent:$('#agent').value, dir:$('#dir').value, off_leash:offLeash };
  try {
    const result = await api('/api/run', {method:'POST',body:JSON.stringify(body)});
    if (result.error) throw new Error(result.error);
    localStorage.setItem('pengy-dir',$('#dir').value);
    localStorage.setItem('pengy-agent',$('#agent').value);
    watch(result.id,{agent:result.agent,prompt:text,state:'running'});
    refreshState();
  } catch (error) {
    say('That job did not start. ' + error.message);
    setDot('ready','ready');
  } finally {
    $('#send').disabled = false; $('#send span').textContent = 'Start job';
  }
}

function watch(id, meta={}) {
  const sequence = ++watchSequence;
  current = id;
  let seen = 0, lastPhase = '', failures = 0, board = null;
  setDot('work',`${meta.agent||'agent'} working`);
  renderJobs(appState || {jobs:[]});
  const details = document.createElement('details'); details.className='progress';
  const summary = document.createElement('summary'); summary.textContent='Agent output';
  const live = document.createElement('span'); live.className='live'; summary.appendChild(live);
  const box = document.createElement('pre'); box.className='out'; box.textContent='Waiting for output…';
  details.append(summary,box);
  const turn = say(`${meta.agent||'The agent'} is on it. You can leave this window — the job will keep running.`);
  turn.querySelector('.bub').appendChild(details);
  const tick = async () => {
    if (sequence !== watchSequence) return;
    try {
      const result = await api(`/api/log?id=${encodeURIComponent(id)}&pos=${seen}`);
      failures = 0;
      if (result.error) throw new Error(result.error);
      if (result.text) {
        seen = result.pos;
        box.textContent = (box.textContent==='Waiting for output…'?'':box.textContent) + result.text;
        box.textContent = box.textContent.slice(-8000); box.scrollTop = box.scrollHeight;
      }
      if (result.board) {
        if (!board) {
          const wrap = document.createElement('details'); wrap.className='progress'; wrap.open = true;
          const cap = document.createElement('summary'); cap.textContent='Swarm board';
          board = document.createElement('pre'); board.className='out';
          wrap.append(cap,board); turn.querySelector('.bub').appendChild(wrap);
        }
        board.textContent = result.board;
      }
      if (result.phase && result.phase !== lastPhase) {
        lastPhase = result.phase;
        if (result.phase === 'capped') {
          setDot('cap','waiting out a cap');
          const message = say(result.resets_at ? 'The usage window is full. Nothing needs you yet.' : (result.note || "The agent is capped. I'll wait for the window and resume."));
          if (result.resets_at) { message.querySelector('.bub').appendChild(nap(result.agent||'The agent',result.resets_at)); scroll(); }
        }
        if (result.phase === 'resumed') {
          setDot('work',`${result.agent||'agent'} working`); say('The window reset. Picking up exactly where it stopped.');
        }
      }
      if (result.state !== 'running') {
        current = null; live.remove(); summary.firstChild.textContent='Agent output · finished';
        setDot('ready','ready');
        say(result.exit===0 ? (result.summary||'Done. The job finished successfully.')
          : `That job stopped and needs you. ${result.summary||''}`.trim());
        refreshState(); return;
      }
      setTimeout(tick,2000);
    } catch (error) {
      failures += 1;
      setDot('idle',failures > 2 ? 'connection lost' : 'reconnecting');
      if (failures === 3) say('The window lost contact with the local service. The background job is still safe; I’ll keep trying.');
      setTimeout(tick,Math.min(10000,2000*failures));
    }
  };
  tick();
}

async function refreshState() {
  try { renderState(await api('/api/state')); }
  catch (_) { /* the active job poll owns connection feedback */ }
}

$('#send').onclick = send;
$('#msg').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
$('#agent').addEventListener('change', () => {
  localStorage.setItem('pengy-agent',$('#agent').value);
  if (appState) renderAgents(appState);
});
$('#dir').addEventListener('change',()=>localStorage.setItem('pengy-dir',$('#dir').value));
function setLeash(off) {
  offLeash = off;
  const b = $('#leash');
  b.classList.toggle('off', off);
  b.setAttribute('aria-pressed', String(off));
  b.textContent = off ? 'Leash off' : 'Leash on';
  b.title = off
    ? 'Leash off: the agent runs commands without asking. Click to put it back on.'
    : 'Leash on: the agent edits files but keeps its normal safety checks. Click to take it off.';
}
$('#leash').onclick = () => {
  if (offLeash) return setLeash(false);       // putting it back on needs no ceremony
  const sheet = $('#leash-sheet');
  sheet.returnValue = 'cancel';
  sheet.showModal();
  sheet.addEventListener('close', () => { if (sheet.returnValue === 'go') setLeash(true); }, {once:true});
};
document.querySelectorAll('.suggestion').forEach(button => button.onclick = () => {
  $('#msg').value = button.dataset.prompt; $('#msg').focus();
});
$('#panel-toggle').onclick = () => {
  const open = document.body.classList.toggle('panel-open');
  $('#panel-toggle').setAttribute('aria-expanded',String(open));
};
boot();
setInterval(refreshState,15000);
</script></body></html>"""


def _job_phase(text: str) -> tuple[str | None, str | None]:
    """What the log says is happening right now, in Pengy's voice."""
    phase, note = None, None
    for line in text.splitlines():
        if "capped. Resuming at" in line:
            phase, note = "capped", line.split("pengy ", 1)[-1].strip()
        elif "window reset" in line:
            phase = "resumed"
    return phase, note


def _ui_state() -> dict:
    led = read_ledger()
    agents = []
    for name in AGENTS:
        entry = led.get(name, {})
        capped = entry.get("state") == "capped" and not quota_ok(name)
        until = None
        if capped and entry.get("resetsAt"):
            until = datetime.fromisoformat(entry["resetsAt"]).astimezone().strftime("%H:%M")
        agents.append({"name": name, "installed": bool(agent_bin(name)), "capped": capped, "until": until})
    jobs = sorted((m for m in (job_meta(p.stem) for p in jobs_dir().glob("*.json")) if m.get("id")),
                  key=lambda m: m.get("started", ""), reverse=True)[:10]
    for j in jobs:
        if j.get("state") == "running" and not _alive(j.get("pid")):
            j["state"] = "gone"
    return {"cwd": os.getcwd(), "agents": agents, "jobs": jobs}


def _tail_bytes(path: Path, limit: int = 65536) -> str:
    """Last `limit` bytes of a file. Pengy's own status lines are always near
    the end, and re-reading a 20MB overnight log every two seconds is not free."""
    try:
        size = path.stat().st_size
        with path.open("r", errors="replace") as fh:
            if size > limit:
                fh.seek(size - limit)
            return fh.read()
    except OSError:
        return ""


def _ui_log(jid: str, pos: int) -> dict:
    meta = job_meta(jid)
    path = jobs_dir() / f"{jid}.log"
    text = ""
    try:
        with path.open("r", errors="replace") as fh:
            fh.seek(pos)
            text = fh.read()
            pos = fh.tell()
    except OSError:
        pass
    whole = _tail_bytes(path)
    phase, note = _job_phase(whole)
    state = meta.get("state", "running")
    if state == "running" and not _alive(meta.get("pid")):
        state = "gone"
    summary = ""
    for line in reversed(whole.splitlines()):
        if "finished in" in line or "needs you" in line or "did not say when" in line:
            summary = line.split("pengy ", 1)[-1].strip()
            break
    # The countdown is driven by the ledger's parsed reset time, not by the
    # log line — a detached job only writes that line every 15 minutes.
    agent = meta.get("agent")
    entry = read_ledger().get(agent, {}) if agent else {}
    resets_at = entry.get("resetsAt") if entry.get("state") == "capped" else None
    return {"text": text, "pos": pos, "state": state, "exit": meta.get("exit"),
            "phase": phase, "note": note, "summary": summary,
            "agent": agent, "resets_at": resets_at,
            # Only swarm jobs have a lane, so a plain job carries no board.
            "board": read_board(Path(meta["dir"])) if meta.get("lane") and meta.get("dir") else None}


def _ui_run(body: dict) -> dict:
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return {"error": "no prompt"}
    agent = body.get("agent") or (installed() or [None])[0]
    if agent not in AGENTS or not agent_bin(agent):
        return {"error": f"{agent!r} is not installed"}
    cwd = Path(body.get("dir") or ".").expanduser()
    if not cwd.is_dir():
        return {"error": f"no such folder: {cwd}"}
    args = argparse.Namespace(prompt=prompt, agent=agent, dir=str(cwd), max_waits=4,
                              fallback=body.get("fallback") or None, job_id=None,
                              off_leash=bool(body.get("off_leash")))
    return {"id": detach(args, agent, cwd.resolve()), "agent": agent}


def _handler(token: str, page: bytes):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        last_seen = time.time()

        def log_message(self, *a):  # the terminal is the thing we are replacing
            pass

        def _send(self, code, body: bytes, ctype="application/json", cache=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _ok(self, obj):
            Handler.last_seen = time.time()
            self._send(200, json.dumps(obj).encode())

        def _authed(self, query) -> bool:
            # This server can start an agent in any folder, so it is a real
            # trust boundary. Loopback alone is not enough: any page in the
            # browser can POST to localhost. A per-run token it cannot guess,
            # plus a Host check against DNS rebinding, is what gates it.
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                return False
            given = self.headers.get("X-Pengy-Key") or query.get("k", [""])[0]
            return hmac.compare_digest(given, token)

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            # Pengy's face, served once and cached, rather than the same base64
            # inlined into every turn. Unauthenticated on purpose: the browser
            # asks for /favicon.ico by itself with no token, and a 403 there is
            # a red line in the console for a picture that gates nothing. The
            # token still guards everything that reads state or starts work.
            if url.path in ("/pengy.png", "/favicon.ico"):
                return self._send(200, icon_png(), "image/png", cache=True)
            if not self._authed(query):
                return self._send(403, b"forbidden", "text/plain")
            if url.path == "/":
                return self._send(200, page, "text/html; charset=utf-8")
            if url.path == "/api/state":
                return self._ok(_ui_state())
            if url.path == "/api/log":
                jid = query.get("id", [""])[0]
                if not re.fullmatch(r"[0-9a-zA-Z-]{1,40}", jid):
                    return self._ok({"error": "bad id"})
                return self._ok(_ui_log(jid, int(query.get("pos", ["0"])[0] or 0)))
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            url = urlparse(self.path)
            if not self._authed(parse_qs(url.query)):
                return self._send(403, b"forbidden", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._ok({"error": "bad request"})
            if url.path == "/api/run":
                try:
                    return self._ok(_ui_run(body))
                except Exception as exc:
                    return self._ok({"error": str(exc)})
            self._send(404, b"not found", "text/plain")

    return Handler


def do_chat(args: argparse.Namespace) -> int:
    token = uuid.uuid4().hex
    page = CHAT_HTML.replace("__SVG__", icon_img()).encode()
    handler = _handler(token, page)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{server.server_port}/?k={token}"
    chat_url_file().write_text(url)  # the widget reuses this rather than starting another
    say(f"Pengy is at {url}")
    say(f"{DIM}closes itself when you close the tab; jobs keep running{OFF}")

    def reap():
        # Launched from the menu icon there is no terminal to Ctrl-C, so exit
        # once the page has stopped its heartbeat. Detached jobs are unaffected.
        while time.time() - handler.last_seen < args.idle:
            time.sleep(5)
        server.shutdown()

    threading.Thread(target=reap, daemon=True).start()
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    chat_url_file().unlink(missing_ok=True)
    say("bye.")
    return 0


DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name={name}
Comment=Runs your AI coding agents while you are away
Exec={exe} {cmd}
Icon={icon}
Terminal=false
Categories=Development;
"""


def do_desktop(args: argparse.Namespace) -> int:
    """Put a Pengy icon in the app menu so it is a double-click, not a command."""
    if sys.platform == "darwin" or os.name == "nt":
        say("desktop launchers are Linux-only for now — run `pengy chat`.")
        return 1
    icon_dir = Path.home() / ".local/share/icons"
    apps_dir = Path.home() / ".local/share/applications"
    icon_dir.mkdir(parents=True, exist_ok=True)
    apps_dir.mkdir(parents=True, exist_ok=True)
    icon = icon_dir / "pengy.png"
    icon.write_bytes(icon_png())
    (icon_dir / "pengy.svg").unlink(missing_ok=True)  # the drawn one, before 0.6
    exe = shutil.which("pengy") or f"{sys.executable} {os.path.abspath(__file__)}"
    for name, label, cmd in (("pengy", "Pengy", "chat"), ("pengy-widget", "Pengy widget", "widget")):
        entry = apps_dir / f"{name}.desktop"
        entry.write_text(DESKTOP_ENTRY.format(exe=exe, icon=icon, name=label, cmd=cmd))
        entry.chmod(0o755)
    subprocess.run(["update-desktop-database", str(apps_dir)], check=False, capture_output=True)
    say(f"added Pengy and Pengy widget to {apps_dir}")
    if getattr(args, "autostart", False):
        _autostart(True)
    say("search your applications for Pengy. `pengy widget` floats her over the desktop.")
    return 0


# -------------------------------------------------------------- widget --

# A small always-on-top chip that sits over the desktop. Tk cannot get a
# per-pixel-transparent window on X11, so this is a deliberate panel rather
# than a floating cut-out — it reads as intentional instead of broken. The
# artwork itself still has a transparent background; Tk composites it onto
# the panel, so it is the same face as the site and the window.

WIDGET_W, WIDGET_H = 196, 72


def chat_url_file() -> Path:
    return state_dir() / "chat.url"


def live_chat_url() -> str | None:
    """The URL of a `pengy chat` that is actually still answering."""
    try:
        url = chat_url_file().read_text().strip()
    except OSError:
        return None
    try:
        import socket
        from urllib.parse import urlparse as _p

        parsed = _p(url)
        with socket.create_connection(("127.0.0.1", parsed.port), timeout=0.4):
            return url
    except Exception:
        return None


def _widget_state() -> tuple[str, str]:
    """(mood, one short line) for the chip. Cheap enough to poll every 3s."""
    jobs = [m for m in (job_meta(p.stem) for p in jobs_dir().glob("*.json")) if m.get("id")]
    live = [j for j in jobs if j.get("state") == "running" and _alive(j.get("pid"))]
    led = read_ledger()

    def until(agent: str) -> str:
        entry = led.get(agent, {})
        stamp = entry.get("resetsAt")
        if not stamp:
            return "unknown"
        try:
            left = (datetime.fromisoformat(stamp) - _now()).total_seconds()
        except ValueError:
            return "unknown"
        if left <= 0:
            return "any moment"
        return f"{int(left // 3600)}h {int(left % 3600 // 60):02d}m"

    for job in live:
        agent = job.get("agent", "?")
        if not quota_ok(agent):
            return "asleep", f"{agent} capped · {until(agent)}"
    if live:
        return "working", f"{live[0].get('agent', '?')} · working"
    capped = [a for a in led if not quota_ok(a)]
    if capped:
        return "idle", f"{capped[0]} capped · {until(capped[0])}"
    return "idle", "idle · tap to open"


def do_widget(args: argparse.Namespace) -> int:
    try:
        import tkinter as tk
    except ImportError:
        say("the floating widget needs Tk, which this Python does not have.")
        say("  Zorin / Ubuntu / Debian:  sudo apt install python3-tk")
        say("  Fedora:                   sudo dnf install python3-tkinter")
        say("Everything else works without it — `pengy chat` opens the window in a browser.")
        return 1

    BG, LINE, INK, DIM = "#141117", "#3A3040", "#F6F1F4", "#9C919B"
    PINK, GOLD, DARK = "#FF3DBE", "#F5C542", "#14040E"

    root = tk.Tk()
    root.title("Pengy")
    root.overrideredirect(True)
    root.wm_attributes("-topmost", True)
    try:
        root.wm_attributes("-type", "dock")  # keeps it visible across workspaces
    except tk.TclError:
        pass
    root.wm_attributes("-alpha", 0.96)

    pos = state_dir() / "widget.pos"
    try:
        x, y = (int(v) for v in pos.read_text().split(","))
    except (OSError, ValueError):
        x, y = root.winfo_screenwidth() - WIDGET_W - 40, 60
    root.geometry(f"{WIDGET_W}x{WIDGET_H}+{x}+{y}")

    c = tk.Canvas(root, width=WIDGET_W, height=WIDGET_H, bg=BG,
                  highlightthickness=1, highlightbackground=LINE)
    c.pack(fill="both", expand=True)

    # Tk 8.6 reads a base64 PNG directly, so the real artwork costs no asset
    # file and no dependency. root keeps the reference alive past this scope.
    try:
        root.peng = tk.PhotoImage(data=PENGY_ICON_SMALL)
    except tk.TclError:  # Tk older than 8.6 cannot read PNG
        root.destroy()
        say(f"the floating widget needs Tk 8.6+ (this is {tk.TkVersion}).")
        say("`pengy chat` opens the same window in a browser instead.")
        return 1
    c.create_image(36, WIDGET_H // 2, image=root.peng, tags="face")
    zs = [c.create_text(58 + i * 8, 20 - i * 6, text="z", fill=GOLD,
                        font=("TkDefaultFont", 7 + i * 2)) for i in range(3)]
    label = c.create_text(72, 27, text="", anchor="w", fill=INK, font=("TkDefaultFont", 9, "bold"))
    sub = c.create_text(72, 44, text="pengy", anchor="w", fill=DIM, font=("TkDefaultFont", 8))

    def show(items, on):
        for i in items:
            c.itemconfigure(i, state="normal" if on else "hidden")

    state = {"mood": "", "tick": 0, "bob": 0}

    def paint():
        mood, text = _widget_state()
        state["mood"] = mood
        head, _, tail = text.partition(" · ")
        c.itemconfigure(label, text=head)
        c.itemconfigure(sub, text=tail or "pengy")
        show(zs, mood == "asleep")
        root.after(3000, paint)

    def animate():
        state["tick"] += 1
        t = state["tick"]
        if state["mood"] == "asleep":
            for i, z in enumerate(zs):
                c.itemconfigure(z, state="normal" if (t + i * 3) % 9 < 6 else "hidden")
        want = -2 if (state["mood"] == "working" and t % 2) else 0
        if want != state["bob"]:
            c.move("face", 0, want - state["bob"])
            state["bob"] = want
        root.after(420, animate)

    # --- drag to move, click to open ---
    drag = {"x": 0, "y": 0, "moved": False}

    def press(e):
        drag.update(x=e.x_root, y=e.y_root, moved=False)

    def motion(e):
        dx, dy = e.x_root - drag["x"], e.y_root - drag["y"]
        if abs(dx) > 3 or abs(dy) > 3:
            drag["moved"] = True
        root.geometry(f"+{root.winfo_x() + dx}+{root.winfo_y() + dy}")
        drag.update(x=e.x_root, y=e.y_root)

    def release(_e):
        if drag["moved"]:
            pos.write_text(f"{root.winfo_x()},{root.winfo_y()}")
        else:
            open_chat()

    def open_chat():
        url = live_chat_url()
        if url:
            webbrowser.open(url)
            return
        spawn = {"creationflags": 0x00000008} if os.name == "nt" else {"start_new_session": True}
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "chat"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **spawn)

    menu = tk.Menu(root, tearoff=0, bg=BG, fg=INK, activebackground=PINK, activeforeground=DARK)
    menu.add_command(label="Open Pengy", command=open_chat)
    menu.add_command(label="Start at login", command=lambda: _autostart(True))
    menu.add_command(label="Don't start at login", command=lambda: _autostart(False))
    menu.add_separator()
    menu.add_command(label="Quit", command=root.destroy)

    c.bind("<Button-1>", press)
    c.bind("<B1-Motion>", motion)
    c.bind("<ButtonRelease-1>", release)
    c.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    paint()
    animate()
    root.mainloop()
    return 0


AUTOSTART_ENTRY = """[Desktop Entry]
Type=Application
Name=Pengy widget
Comment=The floating Pengy that watches your agents
Exec={exe} widget
Icon={icon}
Terminal=false
X-GNOME-Autostart-enabled=true
"""


def _autostart(on: bool) -> bool:
    d = Path.home() / ".config/autostart"
    d.mkdir(parents=True, exist_ok=True)
    entry = d / "pengy-widget.desktop"
    if not on:
        entry.unlink(missing_ok=True)
        say("the widget will no longer start at login.")
        return True
    icon = Path.home() / ".local/share/icons/pengy.png"
    if not icon.exists():
        icon.parent.mkdir(parents=True, exist_ok=True)
        icon.write_bytes(icon_png())
    exe = shutil.which("pengy") or f"{sys.executable} {os.path.abspath(__file__)}"
    entry.write_text(AUTOSTART_ENTRY.format(exe=exe, icon=icon))
    say(f"the widget will start at login ({entry})")
    return True


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
        epilog="Just type `pengy` to open the window. `pengy widget` floats her over "
               "the desktop; `pengy desktop` puts her in your applications menu.",
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

    c = subs.add_parser("chat", help="open the Pengy window — a chat, not a terminal")
    c.add_argument("--port", type=int, default=0, help="fixed port (default: pick a free one)")
    c.add_argument("--no-open", action="store_true", help="print the URL instead of opening a browser")
    c.add_argument("--idle", type=int, default=180, help="quit after this many seconds with the tab closed")
    c.set_defaults(func=do_chat)

    subs.add_parser("widget", help="the floating Pengy that sits over your desktop").set_defaults(func=do_widget)

    dk = subs.add_parser("desktop", help="add Pengy to your applications menu")
    dk.add_argument("--autostart", action="store_true", help="also float the widget from login")
    dk.set_defaults(func=do_desktop)
    s = subs.add_parser("swarm", help="put every installed agent on one goal at the same time")
    s.add_argument("prompt", help="the goal — every agent gets this one")
    s.add_argument("--agents", help="comma-separated (default: every installed agent)")
    s.add_argument("-C", "--dir", default=".", help="working directory (default: .)")
    s.add_argument("--off-leash", action="store_true", help="every agent gets its own bypass mode")
    s.add_argument("--max-waits", type=int, default=4, help="stop an agent after this many caps (default: 4)")
    s.add_argument("-y", "--yes", action="store_true", help="skip the off-leash confirmation")
    s.set_defaults(func=do_swarm)

    b = subs.add_parser("board", help="what the swarm is telling itself, every lane merged")
    b.add_argument("-C", "--dir", default=".", help="working directory (default: .)")
    b.add_argument("-f", "--follow", action="store_true", help="stay on screen and refresh")
    b.set_defaults(func=do_board)

    subs.add_parser("agents", help="which agents are installed, and their quota state").set_defaults(func=do_agents)
    subs.add_parser("jobs", help="background jobs, running and finished").set_defaults(func=do_jobs)
    subs.add_parser("mcp", help="run as an MCP server so other agents can use Pengy").set_defaults(func=do_mcp)
    subs.add_parser("doctor", help="check the install").set_defaults(func=do_doctor)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        # Bare `pengy` opens the window. What someone wants immediately after
        # installing is to see the thing, not a usage block — and a usage block
        # is what they got. `pengy --help` still lists every command.
        args = p.parse_args(["chat"])
    try:
        return args.func(args)
    except KeyboardInterrupt:
        say("stopped.")
        return 130


# ------------------------------------------------------------- artwork --

# Pengy herself, transparent-background PNG, base64. 256px for the window and
# the app menu, 56px for the desktop widget. Kept last so the code above reads
# without scrolling past her, and before __main__ so both are defined by then.

PENGY_ICON = (
    "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"
    "RK5CYII="
)

PENGY_ICON_SMALL = (
    "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"
)

if __name__ == "__main__":
    sys.exit(main())
