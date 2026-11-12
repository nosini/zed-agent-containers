#!/usr/bin/python3 -I
"""Hand a writing task to OpenCode (OpenCode Go) and summarize the result.

Claude orchestrates and reviews; a cheaper model on OpenCode Go writes. Run
in the directory the worker should change, normally a git worktree:

    opencode-delegate SPEC_FILE                 new session
    opencode-delegate -s SESSION SPEC_FILE      follow-up in the same session
    opencode-delegate login | status | models   OpenCode Go login (host terminal)

The worker's full JSON transcript goes to a log file; stdout gets only the
session ID, the worker's final report, cost and the git summary, so the
orchestrator doesn't pay to read the whole transcript.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys

DEFAULT_MODEL = "opencode-go/deepseek-v4-pro"
PROVIDER = "opencode-go"
REPORT_LIMIT = 6000
# OpenCode's data directory (login, sessions) in this agent's state volume,
# not in the shared home, so each Claude account keeps its own Go login.
DATA_HOME = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", "/home/node/.claude"), "opencode-data")


def opencode_env():
    env = dict(os.environ, XDG_DATA_HOME=DATA_HOME, OPENCODE_DISABLE_AUTOUPDATE="1")
    # Keep Claude's own session variables out of the worker's tools.
    for key in [k for k in env if k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))]:
        del env[key]
    return env


def git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout.rstrip() if r.returncode == 0 else ""


def log_path(title):
    # ~/PROJECT/tmp/opencode-delegate, named after the main repository even
    # when running in one of its worktrees.
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    project = os.path.basename(os.path.dirname(common)) if common else os.path.basename(os.getcwd())
    directory = os.path.join(os.path.expanduser("~"), project, "tmp", "opencode-delegate")
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in title)[:40]
    return os.path.join(directory, f"{stamp}-{safe}.jsonl")


def summarize(path):
    session, texts, errors, cost, tokens = None, [], [], 0.0, {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            session = event.get("sessionID") or session
            part = event.get("part") or {}
            if part.get("type") == "text" and part.get("text"):
                texts.append(part["text"])
            if event.get("type") == "error":
                error = event.get("error") or {}
                errors.append(error.get("message") or json.dumps(error)[:500])
            if isinstance(part.get("cost"), (int, float)):
                cost += part["cost"]
            for key, value in (part.get("tokens") or {}).items():
                if isinstance(value, (int, float)):
                    tokens[key] = tokens.get(key, 0) + value
    return session, texts, errors, cost, tokens


def delegate(args):
    if not args.model.startswith(PROVIDER + "/"):
        sys.exit(f"opencode-delegate: only {PROVIDER}/ models (got {args.model})")
    with open(args.spec, encoding="utf-8") as f:
        spec = f.read()
    if not spec.strip():
        sys.exit("opencode-delegate: the spec is empty")
    title = args.title or os.path.splitext(os.path.basename(args.spec))[0]
    log = log_path(title)
    cmd = ["opencode", "run", "--standalone", "--auto", "--format", "json", "--model", args.model]
    cmd += ["--session", args.session] if args.session else ["--title", title]
    cmd.append(spec)
    with open(log, "w") as out, open(log + ".stderr", "w") as err:
        try:
            rc = subprocess.run(cmd, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                env=opencode_env(), timeout=args.timeout * 60).returncode
        except subprocess.TimeoutExpired:
            rc = f"timeout after {args.timeout} min"
    session, texts, errors, cost, tokens = summarize(log)
    print(f"session: {session}  model: {args.model}  exit: {rc}")
    print(f"log: {log}")
    if cost or tokens:
        print("cost: $%.4f  tokens: %s" % (cost, ", ".join(f"{k} {v}" for k, v in tokens.items())))
    for message in errors:
        print(f"error: {message}")
    if texts:
        report = texts[-1]
        if len(report) > REPORT_LIMIT:
            report = report[:REPORT_LIMIT] + f"\n[... cut; full text in {log}]"
        print("--- worker report\n" + report)
    else:
        print("--- no report from the worker; check the log")
    print("--- git status\n" + (git("status", "--short") or "(clean or not a repository)"))
    stat = git("diff", "--stat")
    if stat:
        print("--- git diff --stat\n" + stat)
    return 0 if rc == 0 and not errors else 1


def main():
    if sys.argv[1:2] in (["login"], ["status"], ["models"]):
        os.makedirs(DATA_HOME, mode=0o700, exist_ok=True)
        if sys.argv[1] == "models":
            out = subprocess.run(["opencode", "models", "--standalone"], capture_output=True, text=True,
                                 env=opencode_env()).stdout
            print("\n".join(line for line in out.splitlines() if line.startswith(PROVIDER + "/")))
            return 0
        cmd = ["auth", "login", PROVIDER] if sys.argv[1] == "login" else ["auth", "list"]
        os.execvpe("opencode", ["opencode", *cmd, "--standalone"], opencode_env())
    ap = argparse.ArgumentParser(description="Hand a writing task to OpenCode Go in the current directory.")
    ap.add_argument("spec", help="file with the task for the worker")
    ap.add_argument("-m", "--model", default=os.environ.get("OPENCODE_DELEGATE_MODEL", DEFAULT_MODEL),
                    help=f"{PROVIDER}/ model (default {DEFAULT_MODEL})")
    ap.add_argument("-s", "--session", help="continue this session (from an earlier run's output)")
    ap.add_argument("--title", help="session title and log name (default: the spec's file name)")
    ap.add_argument("--timeout", type=int, default=45, help="minutes before the worker is stopped (default 45)")
    return delegate(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
