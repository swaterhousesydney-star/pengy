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

__version__ = "0.4.0"

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

    if not args.prompt.strip():
        say("nothing to do — give me a job.")
        return 2

    cwd = Path(args.dir).expanduser().resolve()
    if not cwd.is_dir():
        say(f"no such directory: {cwd}")
        return 2

    # Checked now rather than at 3am, when the cap arrives and the fallback
    # turns out to be a typo.
    if args.fallback:
        if args.fallback not in AGENTS:
            say(f"unknown fallback '{args.fallback}'. Known: {', '.join(AGENTS)}")
            return 2
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

PENGY_SVG = """<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" class="peng">
<g class="wings">
<ellipse cx="9" cy="40" rx="5" ry="11" fill="#FF3DBE" transform="rotate(15 9 40)"/>
<ellipse cx="55" cy="40" rx="5" ry="11" fill="#FF3DBE" transform="rotate(-15 55 40)"/>
</g>
<ellipse cx="32" cy="38" rx="22" ry="24" fill="#FF3DBE"/>
<ellipse cx="32" cy="42" rx="14" ry="18" fill="#FFD9F1"/>
<circle cx="32" cy="22" r="18" fill="#FF3DBE"/>
<g class="eyes">
<ellipse cx="25" cy="21" rx="6.5" ry="7" fill="#fff"/>
<ellipse cx="39" cy="21" rx="6.5" ry="7" fill="#fff"/>
<circle cx="26" cy="22" r="3.2" fill="#14040E"/>
<circle cx="38" cy="22" r="3.2" fill="#14040E"/>
</g>
<g class="lids">
<path d="M19.5 20 q5.5 5.5 11 0" stroke="#14040E" stroke-width="2.1" fill="none" stroke-linecap="round"/>
<path d="M33.5 20 q5.5 5.5 11 0" stroke="#14040E" stroke-width="2.1" fill="none" stroke-linecap="round"/>
</g>
<path d="M27 31 q5 4 10 0" stroke="#14040E" stroke-width="2.2" fill="none" stroke-linecap="round"/>
<path d="M32 27 l-5 3 h10 z" fill="#F5C542"/>
</svg>"""

CHAT_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pengy</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0B0A0D;--raise:#141117;--line:#262029;--ink:#F6F1F4;--muted:#9C919B;
--dim:#6B626C;--pink:#FF3DBE;--soft:#FF8AD9;--gold:#F5C542;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
body{background:var(--bg);color:var(--ink);font:16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
height:100vh;display:flex;flex-direction:column;-webkit-font-smoothing:antialiased}
header{display:flex;align-items:center;gap:.75rem;padding:.85rem 1.1rem;border-bottom:1px solid var(--line);
background:rgba(11,10,13,.9);backdrop-filter:blur(12px);flex:none}
header svg{width:34px;height:34px;flex:none}
.name{font-weight:830;letter-spacing:-.03em;font-size:1.1rem}
.status{margin-left:auto;font-family:var(--mono);font-size:.72rem;letter-spacing:.07em;text-transform:uppercase;color:var(--dim)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--gold);margin-right:.45rem;vertical-align:middle}
.dot.idle{background:var(--dim)}.dot.work{background:var(--pink)}.dot.cap{background:var(--gold)}
#log{flex:1;overflow-y:auto;padding:1.5rem 1.1rem;display:flex;flex-direction:column;gap:1rem}
.wrapin{width:100%;max-width:820px;margin-inline:auto}
.turn{display:flex;gap:.7rem;max-width:min(680px,100%);align-items:flex-start}
.turn.me{align-self:flex-end;flex-direction:row-reverse}
.av{width:30px;height:30px;flex:none;border-radius:9px;overflow:hidden}
.turn.me .av{background:var(--line);display:grid;place-items:center;color:var(--muted);font-size:.7rem;font-weight:700}
.bub{background:var(--raise);border:1px solid var(--line);border-radius:16px;padding:.7rem 1rem;white-space:pre-wrap;word-break:break-word}
.turn.me .bub{background:linear-gradient(160deg,rgba(255,61,190,.16),transparent),var(--raise);border-color:rgba(255,61,190,.35)}
.bub .quiet{color:var(--muted)}
.opts{display:flex;flex-wrap:wrap;gap:.45rem;margin-top:.7rem}
.opt{background:transparent;border:1px solid var(--line);color:var(--muted);border-radius:999px;
padding:.35rem .85rem;font:inherit;font-size:.86rem;cursor:pointer}
.opt:hover{border-color:var(--pink);color:var(--ink)}
.opt.on{border-color:var(--pink);color:var(--soft);background:rgba(255,61,190,.1)}
pre.out{font-family:var(--mono);font-size:.76rem;color:var(--muted);background:#07060A;border:1px solid var(--line);
border-radius:10px;padding:.7rem .8rem;margin-top:.7rem;max-height:230px;overflow:auto;white-space:pre}
footer{flex:none;border-top:1px solid var(--line);padding:.85rem 1.1rem;background:var(--raise)}
.bar{display:flex;flex-wrap:wrap;gap:.45rem;align-items:center;margin-bottom:.6rem}
.bar label{font-family:var(--mono);font-size:.68rem;letter-spacing:.07em;text-transform:uppercase;color:var(--dim)}
select,input[type=text]{background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:8px;
padding:.35rem .6rem;font:inherit;font-size:.85rem}
input[type=text]{flex:1;min-width:150px;max-width:480px;font-family:var(--mono);font-size:.78rem}
.compose{display:flex;gap:.6rem;align-items:flex-end}
textarea{flex:1;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:14px;
padding:.7rem .9rem;font:inherit;resize:none;min-height:52px;max-height:180px}
textarea:focus,select:focus,input:focus{outline:none;border-color:var(--pink)}
button.send{background:var(--pink);color:#14040E;border:0;border-radius:999px;padding:.75rem 1.4rem;
font:inherit;font-weight:750;cursor:pointer;flex:none}
button.send:hover{background:var(--soft)}
button.send:disabled{opacity:.45;cursor:not-allowed}
.hint{color:var(--dim);font-size:.76rem;margin-top:.5rem}
.peng{overflow:visible}
.peng .lids{opacity:0}
[data-mood=idle] .peng .lids{animation:blinkOn 6s infinite}
[data-mood=idle] .peng .eyes{animation:blinkOff 6s infinite}
@keyframes blinkOn{0%,95%,100%{opacity:0}96.5%,98.5%{opacity:1}}
@keyframes blinkOff{0%,95%,100%{opacity:1}96.5%,98.5%{opacity:0}}
[data-mood=working] .peng{animation:bob 1.5s ease-in-out infinite;transform-origin:50% 90%}
@keyframes bob{0%,100%{transform:translateY(0) rotate(0)}50%{transform:translateY(-2px) rotate(-2.5deg)}}
[data-mood=sleeping] .peng .eyes{opacity:0}
[data-mood=sleeping] .peng .lids{opacity:1}
[data-mood=sleeping] .peng{animation:breathe 3.6s ease-in-out infinite;transform-origin:50% 95%}
@keyframes breathe{0%,100%{transform:scale(1)}50%{transform:scale(1.05)}}
.nap{position:relative;display:flex;align-items:center;gap:1.3rem;margin-top:.9rem;padding:1.2rem 1.4rem;
border:1px solid rgba(245,197,66,.32);background:linear-gradient(150deg,rgba(245,197,66,.09),transparent 70%),#0E0C12;border-radius:16px}
.nap .peng{width:70px;height:70px;flex:none}
.napt{font-size:1.02rem;font-weight:620}
.napclock{font-family:var(--mono);font-size:1.9rem;letter-spacing:-.02em;color:var(--gold);line-height:1.15;margin:.15rem 0}
.naps{color:var(--muted);font-size:.87rem}
.zzz{position:absolute;left:78px;top:2px;font-family:var(--mono);font-size:1rem;color:var(--gold)}
.zzz span{position:absolute;opacity:0;animation:rise 3s linear infinite}
.zzz span:nth-child(2){animation-delay:1s;left:9px}
.zzz span:nth-child(3){animation-delay:2s;left:18px}
@keyframes rise{0%{opacity:0;transform:translateY(6px) scale(.7)}
25%{opacity:.95}100%{opacity:0;transform:translateY(-26px) scale(1.25)}}
@media (prefers-reduced-motion:reduce){.peng,.zzz span{animation:none!important}}
</style></head><body>
<header>__SVG__<div class="name">Pengy</div><div class="status"><span class="dot idle" id="dot"></span><span id="st">idle</span></div></header>
<div id="log"></div>
<footer>
  <div class="bar">
    <label>agent</label><select id="agent"></select>
    <label>folder</label><input type="text" id="dir">
    <button class="opt" id="leash" title="Off the leash passes the agent its own bypass flag">on a leash</button>
  </div>
  <div class="compose">
    <textarea id="msg" placeholder="Tell Pengy what to do, then close the laptop…" rows="2"></textarea>
    <button class="send" id="send">Send</button>
  </div>
  <div class="hint">Enter sends · Shift+Enter for a new line</div>
</footer>
<script>
const KEY = new URLSearchParams(location.search).get('k') || '';
const $ = s => document.querySelector(s);
const api = (p, o={}) => fetch(p, {...o, headers:{'X-Pengy-Key':KEY,'Content-Type':'application/json',...(o.headers||{})}}).then(r=>r.json());
const AV = `__SVG__`;
let offLeash = false, current = null, seen = 0, lastState = '';

function say(text, opts) {
  const t = document.createElement('div'); t.className = 'turn';
  t.innerHTML = `<div class="av">${AV}</div><div class="bub"></div>`;
  t.querySelector('.bub').append(...render(text));
  if (opts) {
    const row = document.createElement('div'); row.className = 'opts';
    opts.forEach(o => { const b = document.createElement('button'); b.className='opt'; b.textContent=o.label;
      b.onclick = () => { row.remove(); o.run(); }; row.appendChild(b); });
    t.querySelector('.bub').appendChild(row);
  }
  $('#log').appendChild(t); scroll(); return t;
}
function render(text){ const f=document.createDocumentFragment(); f.append(document.createTextNode(text)); return [f]; }
function me(text) {
  const t = document.createElement('div'); t.className = 'turn me';
  t.innerHTML = `<div class="av">you</div><div class="bub"></div>`;
  t.querySelector('.bub').textContent = text; $('#log').appendChild(t); scroll();
}
function scroll(){ $('#log').scrollTop = $('#log').scrollHeight; }
function setDot(cls, label){ $('#dot').className = 'dot ' + cls; $('#st').textContent = label;
  document.body.dataset.mood = cls === 'work' ? 'working' : cls === 'cap' ? 'sleeping' : 'idle'; }
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
  const s = await api('/api/state');
  $('#dir').value = s.cwd;
  $('#agent').innerHTML = s.agents.filter(a=>a.installed)
    .map(a=>`<option value="${a.name}">${a.name}</option>`).join('') || '<option>none found</option>';
  const ready = s.agents.filter(a=>a.installed);
  const capped = ready.filter(a=>a.capped);
  if (!ready.length) { say("No agent CLIs on this machine yet. Install Claude Code, Codex, Gemini, OpenCode, Kimi, Antigravity or Droid, then reopen me."); return; }
  let hi = `Ready. ${ready.length} agent${ready.length>1?'s':''} here: ${ready.map(a=>a.name).join(', ')}.`;
  if (capped.length) hi += `\n${capped.map(a=>`${a.name} is capped until ${a.until||'an unknown time'}`).join('; ')}.`;
  hi += `\n\nGive me a job and go and do something else. If the agent hits its cap I'll wait for the window and pick it back up.`;
  say(hi);
  if (s.jobs.some(j=>j.state==='running')) {
    const j = s.jobs.find(j=>j.state==='running');
    say(`One from earlier is still going — ${j.agent}, "${j.prompt}".`, [{label:'Watch it', run:()=>watch(j.id)}]);
  }
}

async function send() {
  const text = $('#msg').value.trim(); if (!text) return;
  if (current) { say("Something's already running. Let that finish first."); return; }
  me(text); $('#msg').value = '';
  const body = { prompt:text, agent:$('#agent').value, dir:$('#dir').value, off_leash:offLeash };
  const r = await api('/api/run', {method:'POST', body:JSON.stringify(body)});
  if (r.error) { say('That did not start: ' + r.error); return; }
  say(`${r.agent} is on it${offLeash?', off the leash':''}. I'll tell you when something changes.`);
  watch(r.id);
}

function watch(id) {
  current = id; seen = 0; lastState = ''; setDot('work','working');
  const box = document.createElement('pre'); box.className='out'; box.textContent='';
  const t = say('Working.'); t.querySelector('.bub').appendChild(box);
  const tick = async () => {
    const r = await api(`/api/log?id=${encodeURIComponent(id)}&pos=${seen}`);
    if (r.text) { seen = r.pos; box.textContent = (box.textContent + r.text).slice(-6000); box.scrollTop = box.scrollHeight; }
    if (r.phase && r.phase !== lastState) {
      lastState = r.phase;
      if (r.phase === 'capped') {
        setDot('cap','asleep, waiting out a cap');
        const t = say(r.resets_at ? '' : (r.note || "Capped. I'll wait for the window and resume."));
        if (r.resets_at) { t.querySelector('.bub').appendChild(nap(r.agent || 'the agent', r.resets_at)); scroll(); }
      }
      if (r.phase === 'resumed') { setDot('work','working'); say('Window reset. Picking it back up.'); }
    }
    if (r.state !== 'running') {
      current = null; setDot('idle','idle');
      say(r.exit === 0 ? (r.summary || 'Done.') : `That one stopped and needs you. ${r.summary||''}`.trim());
      return;
    }
    setTimeout(tick, 2000);
  };
  tick();
}

$('#send').onclick = send;
$('#msg').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
$('#leash').onclick = () => {
  offLeash = !offLeash;
  $('#leash').classList.toggle('on', offLeash);
  $('#leash').textContent = offLeash ? 'off the leash' : 'on a leash';
  if (offLeash) say("Off the leash means the agent gets its own bypass mode — it can run commands and install things. I am not a sandbox and cannot stop it. Use a folder that's in git.");
};
boot();
setInterval(() => api('/api/state').catch(()=>{}), 20000);
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
            "agent": agent, "resets_at": resets_at}


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

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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
    page = CHAT_HTML.replace("__SVG__", PENGY_SVG).encode()
    handler = _handler(token, page)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{server.server_port}/?k={token}"
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
    say("bye.")
    return 0


DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=Pengy
Comment=Runs your AI coding agents while you are away
Exec={exe} chat
Icon={icon}
Terminal=false
Categories=Development;
"""


def do_desktop(_args: argparse.Namespace) -> int:
    """Put a Pengy icon in the app menu so it is a double-click, not a command."""
    if sys.platform == "darwin" or os.name == "nt":
        say("desktop launchers are Linux-only for now — run `pengy chat`.")
        return 1
    icon_dir = Path.home() / ".local/share/icons"
    apps_dir = Path.home() / ".local/share/applications"
    icon_dir.mkdir(parents=True, exist_ok=True)
    apps_dir.mkdir(parents=True, exist_ok=True)
    icon = icon_dir / "pengy.svg"
    icon.write_text(PENGY_SVG)
    exe = shutil.which("pengy") or f"{sys.executable} {os.path.abspath(__file__)}"
    entry = apps_dir / "pengy.desktop"
    entry.write_text(DESKTOP_ENTRY.format(exe=exe, icon=icon))
    entry.chmod(0o755)
    subprocess.run(["update-desktop-database", str(apps_dir)], check=False, capture_output=True)
    say(f"added {entry}")
    say("Pengy is now in your applications menu. Search for it and pin it.")
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

    c = subs.add_parser("chat", help="open the Pengy window — a chat, not a terminal")
    c.add_argument("--port", type=int, default=0, help="fixed port (default: pick a free one)")
    c.add_argument("--no-open", action="store_true", help="print the URL instead of opening a browser")
    c.add_argument("--idle", type=int, default=180, help="quit after this many seconds with the tab closed")
    c.set_defaults(func=do_chat)

    subs.add_parser("desktop", help="add Pengy to your applications menu").set_defaults(func=do_desktop)
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
