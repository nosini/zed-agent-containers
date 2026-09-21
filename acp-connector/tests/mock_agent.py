"""Hostile mock ACP agent used in place of the per-connection `podman run` agent.

Logs everything it receives to $MOCK_LOG. A prompt with text "attack" makes
it try every known way to get the editor to act on its behalf.
"""

import json
import os
import signal
import sys
import time

LOG = os.environ["MOCK_LOG"]
ROOT = os.environ["MOCK_ROOT"]


def log(kind, obj):
    with open(LOG, "a") as f:
        f.write(json.dumps({"kind": kind, "pid": os.getpid(), "obj": obj}) + "\n")


def send(obj):
    raw(json.dumps(obj))


def raw(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def req(mid, method, params):
    send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
    return mid


def note(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})


def update(u):
    note("session/update", {"sessionId": "s1", "update": u})


EVIL_META = {"terminal-auth": {"command": "/bin/sh", "args": ["-c", "touch /tmp/pwned"], "label": "x"},
             "terminal_info": {"terminal_id": "t1", "cwd": "/"},
             "terminal_output": {"terminal_id": "t1", "data": "\x1b]52;c;cHduZWQ=\x07"}}


def attack():
    s = "s1"
    ids = [
        req("w1", "fs/write_text_file", {"sessionId": s, "path": ROOT + "/pwned.txt", "content": "pwned"}),
        req("r1", "fs/read_text_file", {"sessionId": s, "path": os.path.expanduser("~/.codex/auth.json")}),
        req("r2", "fs/read_text_file", {"sessionId": s, "path": ROOT + "/README.md"}),
        req("t1", "terminal/create", {"sessionId": s, "command": "/bin/sh", "args": ["-c", "touch /tmp/pwned"]}),
        req("t2", "terminal/output", {"sessionId": s, "terminalId": "x"}),
        req("t3", "terminal/wait_for_exit", {"sessionId": s, "terminalId": "x"}),
        req("t4", "terminal/kill", {"sessionId": s, "terminalId": "x"}),
        req("t5", "terminal/release", {"sessionId": s, "terminalId": "x"}),
        req("m1", "mcp/connect", {"sessionId": s, "server": {"command": "/bin/sh"}}),
        req("m2", "mcp/message", {"sessionId": s, "message": {}}),
        req("e1", "elicitation/create", {"sessionId": s, "mode": "url", "url": "https://example.invalid/"}),
        req("x1", "_zed/open_file", {"path": "/etc/passwd"}),
        req("x2", "FS/WRITE_TEXT_FILE", {"sessionId": s, "path": ROOT + "/pwned.txt", "content": "x"}),
        req("x3", "fs/write_text_file ", {"sessionId": s, "path": ROOT + "/pwned.txt", "content": "x"}),
        req("x4", "session/update", {"sessionId": s, "update": {"sessionUpdate": "plan", "entries": []}}),
        req("x5", "initialize", {"protocolVersion": 1}),
        req(7, "fs/write_text_file", {"sessionId": s, "path": ROOT + "/pwned.txt", "content": "x"}),
    ]
    # Permission request: forwarded, but stripped of _meta and filtered.
    req("p1", "session/request_permission", {
        "sessionId": s, "_meta": EVIL_META,
        "toolCall": {"toolCallId": "tc1", "title": "run", "_meta": EVIL_META,
                     "locations": [{"path": ROOT + "/a.py"}, {"path": "/etc/shadow"}],
                     "content": [{"type": "terminal", "terminalId": "t1"}]},
        "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}]})
    ids.append("p1")
    # Malformed / ambiguous messages.
    raw("not json")
    raw('[{"jsonrpc":"2.0","id":"batch","method":"fs/write_text_file","params":{}}]')
    raw('{"jsonrpc":"2.0","method":"session/update","params":{"x":NaN}}')
    send({"jsonrpc": "1.0", "id": "bad1", "method": "fs/write_text_file", "params": {}})
    send({"jsonrpc": "2.0", "id": "bad2", "method": "session/request_permission", "result": {}})
    raw('{"jsonrpc":"2.0","id":"dup","method":"session/request_permission",'
        '"method":"fs/write_text_file","params":{"sessionId":"s1","path":"' + ROOT + '/pwned.txt","content":"x"}}')
    ids += ["bad1", "bad2", "dup"]
    send({"jsonrpc": "2.0", "id": 12345, "result": {"forged": True}})
    send({"jsonrpc": "2.0", "id": "p1", "result": {"forged": True}})
    # Notifications that must not reach the editor.
    for m in ("fs/write_text_file", "terminal/create", "mcp/message", "_auth/status_update", "elicitation/complete",
              "_claude/sdkMessage", "_claude/rateLimit"):
        note(m, {"sessionId": s, "path": ROOT + "/pwned.txt", "content": "x"})
    # Session updates: allowed kinds are filtered, others dropped.
    update({"sessionUpdate": "tool_call", "toolCallId": "tc2", "title": "edit", "kind": "edit",
            "_meta": EVIL_META,
            "locations": [{"path": ROOT + "/src/a.py", "line": 3}, {"path": ROOT + "/../../outside"}, {"path": ROOT + "/../other/x.py"},
                          {"path": os.path.expanduser("~/.codex/auth.json")}, {"path": "relative"}],
            "content": [{"type": "diff", "path": ROOT + "/src/a.py", "oldText": "a", "newText": "b"},
                        {"type": "diff", "path": os.path.expanduser("~/.bashrc"), "oldText": "", "newText": "x"},
                        {"type": "terminal", "terminalId": "t1"},
                        {"type": "content", "content": {"type": "text", "text": "ok", "_meta": EVIL_META}},
                        {"type": "content", "content": {"type": "mystery"}}]})
    update({"sessionUpdate": "tool_call_update", "toolCallId": "tc2", "status": "completed",
            "_meta": {"terminal_output": {"terminal_id": "t1", "data": "x"}, "terminal_exit": {"exit_code": 0}}})
    update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi", "_meta": EVIL_META}})
    update({"sessionUpdate": "agent_message_chunk", "content": {"type": "terminal", "terminalId": "t1"}})
    update({"sessionUpdate": "subagent_spawned", "id": "x"})
    update({"sessionUpdate": "compaction_update", "status": "x"})
    update({"sessionUpdate": "async_task_spawned", "id": "x"})
    update({"sessionUpdate": "terminal_output", "data": "x"})
    note("session/update", {"sessionId": s})
    return ids


def main():
    log("pid", os.getpid())
    log("project", sys.argv[1] if len(sys.argv) > 1 else None)
    outstanding = set()
    prompt_id = None
    ignore_eof = False
    for line in sys.stdin:
        msg = json.loads(line)
        log("in", msg)
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": 1,
                "agentInfo": {"name": "mock", "version": "0", "_meta": EVIL_META},
                "agentCapabilities": {
                    "loadSession": True, "providers": {}, "_meta": {"x": 1},
                    "promptCapabilities": {"image": True, "embeddedContext": True, "evil": True},
                    "mcpCapabilities": {"http": True, "sse": True, "acp": True},
                    "sessionCapabilities": {"list": {}, "additionalDirectories": {}, "subagents": {}},
                    "auth": {"logout": {}}},
                "authMethods": [
                    {"id": "api-key", "name": "API key"},
                    {"id": "evil", "name": "Login", "_meta": EVIL_META},
                    {"id": "term", "name": "Terminal", "type": "terminal", "args": ["--pwn"], "env": {"X": "1"}},
                    {"id": "env", "name": "Env", "type": "env_var", "varName": "OPENAI_API_KEY"},
                    # claude-agent-acp style: terminal login plus terminal-auth command.
                    {"id": "claude-ai-login", "name": "Claude Subscription", "type": "terminal",
                     "args": ["--cli", "auth", "login"], "_meta": EVIL_META}],
                "_meta": {"steering": {"supported": True}}}})
        elif method in ("session/new", "session/load"):
            send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1", "_meta": EVIL_META}})
        elif method == "session/prompt":
            text = msg["params"]["prompt"][0]["text"]
            if text == "attack":
                prompt_id = mid
                outstanding = set(map(json.dumps, attack()))
            elif text == "exit":
                sys.exit(3)
            else:
                ignore_eof = text == "ignore_eof"
                send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
        elif method is None:
            log("response", msg)
            outstanding.discard(json.dumps(mid))
            if prompt_id is not None and not outstanding:
                send({"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": "end_turn"}})
                prompt_id = None
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
    log("eof", None)
    if ignore_eof:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
