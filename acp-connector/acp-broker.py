#!/usr/bin/python3
"""Host-side ACP broker between Zed (Flatpak) and an agent in rootless Podman.

Runs on the host as a systemd user service, one instance per profile. It
accepts connections on a dedicated Unix socket inside Zed's Flatpak runtime
directory. For each connection it starts a fresh container of the profile's
fixed image and ACP adapter (`podman run --rm`), with the git root mounted
read-only and the connection's project mounted read-write, and relays ACP
(newline-delimited JSON-RPC 2.0) through an allowlist policy:

* The profile is fixed by the host unit. The project is the connecting
  process's working directory (Zed starts agents in the project root) and
  must be a real directory inside the git root. Client input never chooses
  the command, image, other mounts or environment.
* Agent -> Zed: only `session/update` notifications of known kinds and
  `session/request_permission` requests are forwarded. fs/*, terminal/*,
  mcp/*, elicitation/*, extension (`_`-prefixed) and unknown methods are
  answered with an error by the broker and never reach Zed.
* Zed -> agent: known methods only. Client capabilities are replaced with a
  fixed "no fs, no terminal" set, forwarded MCP servers are removed, and
  session cwd must lie inside the connection's project.
* `_meta` extension data is removed in both directions, and the agent's
  advertised capabilities and auth methods are rewritten to a safe subset.
* Every forwarded message is re-encoded from the parsed object, so Zed and
  the agent see exactly what the policy inspected.

Message contents are never logged, only method names and policy decisions.
"""

import argparse
import asyncio
import json
import os
import posixpath
import shlex
import signal
import socket
import stat
import struct
import sys

PODMAN = ("/usr/bin/podman", "--storage-opt", "overlay.mount_program=/usr/bin/fuse-overlayfs")
# Fixed per-profile image, ACP adapter and state volume. Nothing here comes
# from the client; the project path is taken from the connecting process's
# working directory and validated (see resolve_project).
PROFILES = {
    "codex": {"image": "localhost/codex-agent:local", "entry": "codex-acp", "label": "Codex",
              "volume": "codex-agent-state", "state": "/home/node/.codex",
              "instructions": "/home/node/.codex/AGENTS.md"},
    "claude": {"image": "localhost/claude-agent:local", "entry": "claude-agent-acp", "label": "Claude",
               "volume": "claude-agent-state", "state": "/home/node/.claude",
               "instructions": "/home/node/.claude/CLAUDE.md"},
}
# Writable home shared by both agents: toolchains they install, build caches.
HOME_VOLUME = "agent-home"
HOME_DIR = "/home/node"
# Mount targets pre-created in the images (their root filesystem is read-only).
GIT_CONFIG_TARGET = "/opt/agent-git/gitconfig"   # GIT_CONFIG_GLOBAL in the images
SSH_AGENT_TARGET = "/opt/agent-git/ssh-agent.sock"
KNOWN_HOSTS_TARGET = "/opt/agent-git/known_hosts"
# Notes shared by all agents across projects (read-write). The git repository
# around it stays on the host, so the agents can't touch its hooks or config.
MEMORY_TARGET = "/home/node/memory"
# Git's SSH (pushes) trusts only the host keys from the host, never the
# agent-writable ~/.ssh/known_hosts, and never adds keys on first use.
GIT_SSH_COMMAND = f"ssh -o UserKnownHostsFile={KNOWN_HOSTS_TARGET} -o StrictHostKeyChecking=yes"

MAX_LINE = 64 * 1024 * 1024
MAX_PENDING = 256
MAX_SESSIONS = 4
BUSY_TIMEOUT = 10
AGENT_EXIT_GRACE = 5
AGENT_TERM_GRACE = 3

METHOD_NOT_FOUND = -32601
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
BROKER_ERROR = -32000

CLIENT_REQUESTS = frozenset({
    "initialize", "authenticate", "logout",
    "session/new", "session/load", "session/resume", "session/fork",
    "session/list", "session/prompt", "session/set_mode", "session/set_model",
    "session/set_config_option", "session/close", "session/delete",
})
CLIENT_NOTIFICATIONS = frozenset({"session/cancel", "$/cancel_request"})
SESSION_OPENERS = frozenset({"session/new", "session/load", "session/resume", "session/fork"})

AGENT_REQUESTS = frozenset({"session/request_permission"})
AGENT_NOTIFICATIONS = frozenset({"session/update"})

SESSION_UPDATES = frozenset({
    "user_message_chunk", "agent_message_chunk", "agent_thought_chunk",
    "tool_call", "tool_call_update", "plan", "available_commands_update",
    "current_mode_update", "config_option_update", "session_info_update",
    "usage_update",
})
CONTENT_BLOCKS = frozenset({"text", "image", "audio", "resource_link", "resource"})

# The only capabilities the agent is told Zed has.
CLIENT_CAPABILITIES = {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}


class PolicyError(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def _reject_constant(name):
    raise ValueError(f"non-standard JSON constant {name}")


def decode(line):
    """Parse one NDJSON line; None if it is not a JSON object."""
    try:
        msg = json.loads(line, parse_constant=_reject_constant)
    except (ValueError, RecursionError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, dict) else None


def encode(msg):
    return (json.dumps(msg, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def valid_id(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def id_key(value):
    return json.dumps(value)


def classify(msg):
    """Return 'request', 'notification' or 'response', or None if malformed."""
    if msg.get("jsonrpc") != "2.0":
        return None
    has_id = "id" in msg
    if "method" in msg:
        if not isinstance(msg["method"], str) or "result" in msg or "error" in msg:
            return None
        if "params" in msg and not isinstance(msg["params"], dict):
            return None
        if has_id:
            return "request" if valid_id(msg["id"]) else None
        return "notification"
    if has_id and valid_id(msg["id"]) and (("result" in msg) != ("error" in msg)):
        return "response"
    return None


def strip_meta(value):
    """Deep copy without `_meta` keys at any depth."""
    if isinstance(value, dict):
        return {k: strip_meta(v) for k, v in value.items() if k != "_meta"}
    if isinstance(value, list):
        return [strip_meta(v) for v in value]
    return value


def error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def pick_strings(obj, keys):
    if not isinstance(obj, dict):
        return {}
    return {k: obj[k] for k in keys if isinstance(obj.get(k), str)}


class Policy:
    """Per-connection ACP filter. Returns messages to send in each direction."""

    def __init__(self, project, git_root, tag, label=None):
        self.project = project      # session cwd must be inside this
        self.git_root = git_root    # locations/diffs shown to Zed must be inside this
        self.tag = tag
        # Zed shows every custom agent's threads with the same icon, so the
        # thread titles it displays carry the agent's name instead.
        self.title_prefix = f"[{label}] " if label else ""
        self.client_pending = {}   # id key -> method, client requests awaiting the agent
        self.agent_pending = set()  # id keys of agent requests forwarded to Zed

    def note(self, text):
        log(f"[{self.tag}] {text}")

    @staticmethod
    def within(path, root):
        if not isinstance(path, str) or "\0" in path or not path.startswith("/"):
            return False
        path = posixpath.normpath(path)
        return path == root or path.startswith(root + "/")

    # ---- Zed -> agent -------------------------------------------------

    def from_client(self, msg):
        """Returns (to_agent, to_client)."""
        kind = classify(msg)
        if kind is None:
            self.note("dropped malformed message from client")
            mid = msg.get("id")
            return [], ([error(mid, INVALID_REQUEST, "Invalid JSON-RPC message")] if valid_id(mid) else [])

        if kind == "response":
            key = id_key(msg["id"])
            if key not in self.agent_pending:
                self.note("dropped client response with unknown id")
                return [], []
            self.agent_pending.discard(key)
            out = {"jsonrpc": "2.0", "id": msg["id"]}
            if "result" in msg:
                out["result"] = strip_meta(msg["result"])
            else:
                out["error"] = self.clean_error(msg["error"])
            return [out], []

        method = msg["method"]
        if kind == "notification":
            if method not in CLIENT_NOTIFICATIONS:
                self.note(f"dropped client notification {method!r}")
                return [], []
            return [{"jsonrpc": "2.0", "method": method, "params": strip_meta(msg.get("params", {}))}], []

        mid = msg["id"]
        if method not in CLIENT_REQUESTS:
            self.note(f"rejected client request {method!r}")
            return [], [error(mid, METHOD_NOT_FOUND, f"Method not supported by read-only connector: {method}")]
        key = id_key(mid)
        if key in self.client_pending or len(self.client_pending) >= MAX_PENDING:
            return [], [error(mid, INVALID_REQUEST, "Duplicate or too many pending requests")]
        try:
            params = self.client_params(method, msg.get("params", {}))
        except PolicyError as exc:
            self.note(f"rejected client request {method!r}: {exc}")
            return [], [error(mid, INVALID_PARAMS, str(exc))]
        self.client_pending[key] = method
        return [{"jsonrpc": "2.0", "id": mid, "method": method, "params": params}], []

    def client_params(self, method, params):
        params = strip_meta(params)
        if method == "initialize":
            out = {"protocolVersion": params.get("protocolVersion"),
                   "clientCapabilities": json.loads(json.dumps(CLIENT_CAPABILITIES))}
            info = pick_strings(params.get("clientInfo"), ("name", "title", "version"))
            if info:
                out["clientInfo"] = info
            return out
        if method == "authenticate":
            if not isinstance(params.get("methodId"), str):
                raise PolicyError("methodId must be a string")
            return {"methodId": params["methodId"]}
        if method in SESSION_OPENERS:
            if not self.within(params.get("cwd"), self.project):
                raise PolicyError(f"cwd must be inside {self.project}, the project this agent was started for")
            params["cwd"] = posixpath.normpath(params["cwd"])
            servers = params.get("mcpServers")
            if isinstance(servers, list) and servers:
                self.note(f"removed {len(servers)} forwarded MCP server(s) from {method}")
            params["mcpServers"] = []
            if params.pop("additionalDirectories", None):
                self.note(f"removed additionalDirectories from {method}")
        return params

    # ---- agent -> Zed -------------------------------------------------

    def from_agent(self, msg):
        """Returns (to_client, to_agent)."""
        kind = classify(msg)
        if kind is None:
            self.note("dropped malformed message from agent")
            mid = msg.get("id")
            if "method" in msg and valid_id(mid):
                return [], [error(mid, INVALID_REQUEST, "Invalid JSON-RPC message")]
            return [], []

        if kind == "response":
            method = self.client_pending.pop(id_key(msg["id"]), None)
            if method is None:
                self.note("dropped agent response with unknown id")
                return [], []
            out = {"jsonrpc": "2.0", "id": msg["id"]}
            if "error" in msg:
                out["error"] = self.clean_error(msg["error"])
            else:
                out["result"] = self.agent_result(method, msg["result"])
            return [out], []

        method = msg["method"]
        params = msg.get("params", {})
        if kind == "notification":
            if method not in AGENT_NOTIFICATIONS:
                self.note(f"blocked agent notification {method!r}")
                return [], []
            params = self.session_update(params)
            return ([{"jsonrpc": "2.0", "method": method, "params": params}] if params else []), []

        mid = msg["id"]
        if method not in AGENT_REQUESTS:
            self.note(f"blocked agent request {method!r}")
            return [], [error(mid, METHOD_NOT_FOUND, f"Blocked by read-only connector policy: {method}")]
        key = id_key(mid)
        if key in self.agent_pending or len(self.agent_pending) >= MAX_PENDING:
            return [], [error(mid, INVALID_REQUEST, "Duplicate or too many pending requests")]
        try:
            params = self.permission_request(params)
        except PolicyError as exc:
            self.note(f"blocked agent request {method!r}: {exc}")
            return [], [error(mid, INVALID_PARAMS, str(exc))]
        self.agent_pending.add(key)
        return [{"jsonrpc": "2.0", "id": mid, "method": method, "params": params}], []

    def label_title(self, title):
        if not self.title_prefix or not isinstance(title, str) or title.startswith(self.title_prefix):
            return title
        return self.title_prefix + title

    def agent_result(self, method, result):
        result = strip_meta(result)
        if method == "session/list" and isinstance(result, dict) and isinstance(result.get("sessions"), list):
            for s in result["sessions"]:
                if isinstance(s, dict) and "title" in s:
                    s["title"] = self.label_title(s["title"])
        if method != "initialize" or not isinstance(result, dict):
            return result
        caps = result.get("agentCapabilities")
        caps = caps if isinstance(caps, dict) else {}
        prompt = caps.get("promptCapabilities")
        prompt = prompt if isinstance(prompt, dict) else {}
        sess = caps.get("sessionCapabilities")
        sess = sess if isinstance(sess, dict) else {}
        auth = caps.get("auth")
        out_caps = {
            "loadSession": caps.get("loadSession") is True,
            "promptCapabilities": {k: prompt[k] is True for k in ("image", "audio", "embeddedContext") if k in prompt},
            "mcpCapabilities": {"http": False, "sse": False},
            "sessionCapabilities": {k: {} for k in ("list", "resume", "fork", "close", "delete") if k in sess},
        }
        if isinstance(auth, dict) and "logout" in auth:
            out_caps["auth"] = {"logout": {}}
        methods = []
        for m in result.get("authMethods") or []:
            # Only agent-handled auth. "terminal" and "env_var" methods make Zed
            # run commands or restart the client with extra input.
            if isinstance(m, dict) and isinstance(m.get("id"), str) and m.get("type", "agent") == "agent":
                methods.append(pick_strings(m, ("id", "name", "description")))
            elif isinstance(m, dict):
                self.note(f"removed auth method {m.get('id')!r}")
        out = {"protocolVersion": result.get("protocolVersion"),
               "agentCapabilities": out_caps, "authMethods": methods}
        info = pick_strings(result.get("agentInfo"), ("name", "title", "version"))
        if info:
            out["agentInfo"] = info
        return out

    def session_update(self, params):
        params = strip_meta(params)
        update = params.get("update")
        kind = update.get("sessionUpdate") if isinstance(update, dict) else None
        if kind not in SESSION_UPDATES:
            self.note(f"dropped session/update kind {kind!r}")
            return None
        if kind in ("tool_call", "tool_call_update"):
            self.tool_call(update)
        elif kind == "session_info_update" and "title" in update:
            update["title"] = self.label_title(update["title"])
        elif "content" in update and not self.content_block_ok(update["content"]):
            self.note(f"dropped {kind} with unsupported content")
            return None
        return params

    def content_block_ok(self, block):
        return isinstance(block, dict) and block.get("type") in CONTENT_BLOCKS

    def tool_call(self, tc):
        """Filter a ToolCall/ToolCallUpdate in place."""
        if "locations" in tc:
            locs = tc["locations"] if isinstance(tc["locations"], list) else []
            kept = [dict(l, path=posixpath.normpath(l["path"])) for l in locs
                    if isinstance(l, dict) and self.within(l.get("path"), self.git_root)]
            if len(kept) != len(locs):
                self.note(f"removed {len(locs) - len(kept)} tool location(s) outside project")
            tc["locations"] = kept
        if "content" in tc:
            items = tc["content"] if isinstance(tc["content"], list) else []
            kept = []
            for item in items:
                kind = item.get("type") if isinstance(item, dict) else None
                if kind == "content" and self.content_block_ok(item.get("content")):
                    kept.append(item)
                elif kind == "diff" and self.within(item.get("path"), self.git_root):
                    kept.append(dict(item, path=posixpath.normpath(item["path"])))
                else:
                    # "terminal" content refers to editor terminals, which are never created.
                    self.note(f"removed tool content item of type {kind!r}")
            tc["content"] = kept

    def permission_request(self, params):
        params = strip_meta(params)
        tool = params.get("toolCall")
        options = params.get("options")
        if not isinstance(tool, dict) or not isinstance(options, list) or not all(
                isinstance(o, dict) and isinstance(o.get("optionId"), str) for o in options):
            raise PolicyError("malformed permission request")
        self.tool_call(tool)
        return params

    @staticmethod
    def clean_error(err):
        if not isinstance(err, dict):
            return {"code": BROKER_ERROR, "message": "Error"}
        code = err.get("code")
        out = {"code": code if isinstance(code, int) and not isinstance(code, bool) else BROKER_ERROR,
               "message": err.get("message") if isinstance(err.get("message"), str) else "Error"}
        if "data" in err:
            out["data"] = strip_meta(err["data"])
        return out


def mount_safe(path):
    """Absolute, normalized, and free of characters that break --mount syntax."""
    return (isinstance(path, str) and path.startswith("/") and posixpath.normpath(path) == path
            and not any(c in path for c in ",=") and all(0x20 <= ord(c) != 0x7f for c in path))


def resolve_project(pid, git_root):
    """The project a client was started in: its working directory, which must
    be a real directory strictly inside git_root. Zed starts agent servers in
    the project root. Returns (project, None) or (None, reason)."""
    link = f"/proc/{pid}/cwd"
    try:
        cwd = os.readlink(link)
        peer = os.stat(link)
    except OSError as exc:
        return None, f"cannot read the client's working directory ({exc.strerror})"
    if not mount_safe(cwd):
        return None, f"unsupported project path {cwd!r}"
    if not cwd.startswith(git_root + "/"):
        return None, f"open a project inside {git_root} to use this agent (got {cwd})"
    try:
        real = os.path.realpath(cwd, strict=True)
        host = os.stat(real)
    except OSError:
        return None, f"project path {cwd} does not exist on the host"
    # Same path, no symlinks, and the very directory the client is in (this
    # also catches a sandbox that shows a different tree under the same path).
    if real != cwd or (host.st_dev, host.st_ino) != (peer.st_dev, peer.st_ino) or not stat.S_ISDIR(host.st_mode):
        return None, f"project path {cwd} does not match the host directory"
    return cwd, None


def podman_argv(profile, name, git_root, project, gitconfig=None, ssh_agent=None,
                android_sdk=None, instructions=None, known_hosts=None, memory=None):
    """The one command a connection may run: a fresh container with ~/Git
    read-only, the connection's project read-write, the shared agent home
    and the profile's state. Optional host resources are mounted read-only,
    except the SSH agent socket and the shared memory."""
    p = PROFILES[profile]
    argv = [*PODMAN, "run", "--rm", "--interactive", "--detach-keys=", f"--name={name}",
            f"--label=acp-connector.profile={profile}", "--pull=never", "--log-driver=none",
            "--userns=keep-id:uid=1000,gid=1000", "--user=1000:1000",
            "--read-only", "--read-only-tmpfs=true", "--cap-drop=all",
            "--security-opt=no-new-privileges", "--security-opt=label=disable", "--pids-limit=4096",
            f"--mount=type=volume,src={HOME_VOLUME},dst={HOME_DIR}",
            f"--mount=type=volume,src={p['volume']},dst={p['state']}",
            f"--mount=type=bind,src={git_root},dst={git_root},ro=true",
            f"--mount=type=bind,src={project},dst={project}",
            f"--workdir={project}"]
    if gitconfig:
        argv.append(f"--mount=type=bind,src={gitconfig},dst={GIT_CONFIG_TARGET},ro=true")
    if ssh_agent:
        argv += [f"--mount=type=bind,src={ssh_agent},dst={SSH_AGENT_TARGET}", f"--env=SSH_AUTH_SOCK={SSH_AGENT_TARGET}"]
    if android_sdk:
        # Same path as on the host, so projects' local.properties sdk.dir works.
        argv += [f"--mount=type=bind,src={android_sdk},dst={android_sdk},ro=true",
                 f"--env=ANDROID_HOME={android_sdk}", f"--env=ANDROID_SDK_ROOT={android_sdk}"]
    if instructions:
        argv.append(f"--mount=type=bind,src={instructions},dst={p['instructions']},ro=true")
    if known_hosts:
        argv += [f"--mount=type=bind,src={known_hosts},dst={KNOWN_HOSTS_TARGET},ro=true",
                 f"--env=GIT_SSH_COMMAND={GIT_SSH_COMMAND}"]
    if memory:
        argv.append(f"--mount=type=bind,src={memory},dst={MEMORY_TARGET}")
    return argv + [p["image"], p["entry"]]


def podman_launcher(profile, git_root, gitconfig=None, ssh_agent=None, android_sdk=None, instructions=None,
                    known_hosts=None, memory=None):
    def launch(project, name):
        agent = ssh_agent if ssh_agent and os.path.exists(ssh_agent) else None
        if ssh_agent and not agent:
            log(f"SSH agent socket {ssh_agent} missing; starting without commit signing")
        return (podman_argv(profile, name, git_root, project, gitconfig, agent, android_sdk, instructions,
                            known_hosts, memory),
                [*PODMAN, "rm", "--force", "--ignore", name])
    return launch


class Broker:
    def __init__(self, launcher, git_root, name):
        self.hint = f"see journalctl --user -u {name}-acp-connector"
        self.launcher = launcher
        self.git_root = git_root
        self.name = name
        self.active = 0
        self.counter = 0
        self.tasks = set()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.counter += 1
        tag = f"conn{self.counter}"
        try:
            pid = self.peer_pid(writer, tag)
            if pid is None:
                return
            project, reason = resolve_project(pid, self.git_root)
            if project is None:
                await self.refuse(reader, writer, tag, reason[0].upper() + reason[1:])
                return
            if self.active >= MAX_SESSIONS:
                await self.refuse(reader, writer, tag, f"Connector busy: at most {MAX_SESSIONS} concurrent sessions")
                return
            self.active += 1
            try:
                await self.session(reader, writer, tag, project)
            finally:
                self.active -= 1
        except Exception as exc:  # never let one connection take down the broker
            log(f"[{tag}] error: {exc!r}")
        finally:
            writer.close()
            self.tasks.discard(task)

    def peer_pid(self, writer, tag):
        sock = writer.get_extra_info("socket")
        try:
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            pid, uid, _ = struct.unpack("3i", creds)
        except OSError as exc:
            log(f"[{tag}] refused: cannot read peer credentials ({exc})")
            return None
        if uid != os.getuid():
            log(f"[{tag}] refused: peer uid {uid}")
            return None
        log(f"[{tag}] connected (peer pid {pid})")
        return pid

    async def refuse(self, reader, writer, tag, message):
        log(f"[{tag}] refused: {message}")
        try:
            line = await asyncio.wait_for(reader.readline(), BUSY_TIMEOUT)
            msg = decode(line)
            if msg and valid_id(msg.get("id")) and "method" in msg:
                writer.write(encode(error(msg["id"], BROKER_ERROR, message)))
                await writer.drain()
        except (asyncio.TimeoutError, ValueError, ConnectionError):
            pass

    async def session(self, reader, writer, tag, project):
        policy = Policy(project, self.git_root, tag, PROFILES.get(self.name, {}).get("label"))
        argv, cleanup = self.launcher(project, f"{self.name}-agent-{os.getpid()}-{self.counter}")
        log(f"[{tag}] project {project}; agent: {shlex.join(argv)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=MAX_LINE)
        except OSError as exc:
            log(f"[{tag}] cannot start agent: {exc}")
            await self.refuse(reader, writer, tag, f"Container agent could not be started; {self.hint}")
            return
        log(f"[{tag}] agent started (pid {proc.pid})")

        async def send(stream, msgs):
            for m in msgs:
                stream.write(encode(m))
            if msgs:
                await stream.drain()

        async def client_to_agent():
            while True:
                line = await reader.readline()
                if not line:
                    return "client closed"
                msg = decode(line)
                if msg is None:
                    policy.note("dropped unparseable line from client")
                    continue
                to_agent, to_client = policy.from_client(msg)
                await send(proc.stdin, to_agent)
                await send(writer, to_client)

        async def agent_to_client():
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return "agent exited"
                msg = decode(line)
                if msg is None:
                    policy.note("dropped unparseable line from agent")
                    continue
                to_client, to_agent = policy.from_agent(msg)
                await send(writer, to_client)
                await send(proc.stdin, to_agent)

        async def agent_stderr():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                log(f"[{tag}] agent: {line[:2000].decode('utf-8', 'replace').rstrip()}")

        stderr_task = asyncio.create_task(agent_stderr())
        pumps = [asyncio.create_task(client_to_agent()), asyncio.create_task(agent_to_client())]
        try:
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception()
                log(f"[{tag}] closing: {exc!r}" if exc else f"[{tag}] closing: {t.result()}")
            # Tell Zed about requests that will never be answered.
            if policy.client_pending and not writer.is_closing():
                try:
                    await send(writer, [error(json.loads(k), BROKER_ERROR,
                                              f"Container agent exited; {self.hint}")
                                        for k in policy.client_pending])
                except (ConnectionError, OSError):
                    pass
        finally:
            for t in pumps:
                t.cancel()
            await self.stop_agent(proc, tag, cleanup)
            stderr_task.cancel()

    async def stop_agent(self, proc, tag, cleanup):
        signalled = False
        if proc.returncode is None:
            try:
                proc.stdin.close()
            except (ConnectionError, OSError):
                pass
            for sig, grace in ((None, AGENT_EXIT_GRACE), (signal.SIGTERM, AGENT_TERM_GRACE), (signal.SIGKILL, None)):
                if sig is not None:
                    signalled = True
                    try:
                        proc.send_signal(sig)
                    except ProcessLookupError:
                        break
                try:
                    await asyncio.wait_for(proc.wait(), grace)
                    break
                except asyncio.TimeoutError:
                    continue
        log(f"[{tag}] agent ended (status {proc.returncode})")
        if cleanup and signalled:
            # The agent ignored EOF: make sure its container is gone too.
            await run_quiet(cleanup)


def activated_socket():
    """The listening socket passed by systemd socket activation, if any."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        return None
    for var in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(var, None)
    sock = socket.socket(fileno=3)
    if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM:
        raise SystemExit("activated fd is not a Unix stream socket")
    return sock


def bind_socket(path):
    """Manual mode: create the listening socket (0600) ourselves."""
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    try:
        if stat.S_ISSOCK(os.lstat(path).st_mode):
            os.unlink(path)
        else:
            raise SystemExit(f"{path} exists and is not a socket")
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    sock.listen(16)
    return sock


async def run_quiet(argv, timeout=30):
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return out.decode("utf-8", "replace")
    except (OSError, asyncio.TimeoutError) as exc:
        log(f"{argv[0]}: {exc!r}")
        return ""


async def remove_stale(profile):
    """Remove containers left behind by an earlier broker (e.g. after a crash)."""
    ids = (await run_quiet([*PODMAN, "ps", "--all", "--quiet",
                            f"--filter=label=acp-connector.profile={profile}"])).split()
    ids = [i for i in ids if i.isalnum()]
    if ids:
        log(f"removing {len(ids)} stale container(s)")
        await run_quiet([*PODMAN, "rm", "--force", "--ignore", *ids])


async def serve(sock, launcher, git_root, owns_path=True, name="agent", before=None):
    broker = Broker(launcher, git_root, name)
    extra = {}
    if sys.version_info >= (3, 13):
        # We unlink the path ourselves below, the same way on every Python.
        extra["cleanup_socket"] = False
    # With socket activation systemd owns the path; only manual mode removes it.
    path = sock.getsockname() if owns_path else None
    if before:
        await before
    server = await asyncio.start_unix_server(broker.handle, sock=sock, limit=MAX_LINE, **extra)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log(f"listening; profile: {name}; git root: {git_root}")
    async with server:
        await stop.wait()
        server.close()
        for t in list(broker.tasks):
            t.cancel()
        await asyncio.gather(*broker.tasks, return_exceptions=True)
    if path:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    log("stopped")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", required=True, choices=sorted(PROFILES),
                    help="which fixed image and ACP adapter to run")
    ap.add_argument("--git-root", required=True,
                    help="mounted read-only; projects inside it are mounted read-write per connection")
    ap.add_argument("--git-config", help="gitconfig for the agents (identity, signing), mounted read-only")
    ap.add_argument("--ssh-agent-socket", help="SSH agent socket forwarded for commit signing")
    ap.add_argument("--android-sdk", help="Android SDK directory, mounted read-only at the same path")
    ap.add_argument("--instructions", help="environment notes for the agents (AGENTS.md/CLAUDE.md), read-only")
    ap.add_argument("--known-hosts", help="SSH host keys git may push to, mounted read-only")
    ap.add_argument("--memory", help="notes directory shared by all agents, mounted read-write at ~/memory")
    ap.add_argument("--socket", help="listen here instead of using systemd socket activation")
    args = ap.parse_args()
    git_root = os.path.realpath(args.git_root)
    for opt, path in (("--git-root", git_root), ("--git-config", args.git_config),
                      ("--ssh-agent-socket", args.ssh_agent_socket), ("--android-sdk", args.android_sdk),
                      ("--instructions", args.instructions), ("--known-hosts", args.known_hosts),
                      ("--memory", args.memory)):
        if path is not None and not mount_safe(path):
            ap.error(f"{opt} must be an absolute, normalized path without ',' or '='")
    if not os.path.isdir(git_root):
        ap.error("--git-root must be an existing directory")
    for opt, path in (("--git-config", args.git_config), ("--instructions", args.instructions),
                      ("--known-hosts", args.known_hosts)):
        if path and not os.path.isfile(path):
            ap.error(f"{opt} must be an existing file")
    for opt, path in (("--android-sdk", args.android_sdk), ("--memory", args.memory)):
        if path and not os.path.isdir(path):
            ap.error(f"{opt} must be an existing directory")
    sock = activated_socket()
    owns_path = sock is None
    if owns_path:
        if not args.socket:
            ap.error("no socket-activation fd; use the systemd socket unit or pass --socket")
        sock = bind_socket(args.socket)
    launcher = podman_launcher(args.profile, git_root, args.git_config, args.ssh_agent_socket,
                               args.android_sdk, args.instructions, args.known_hosts, args.memory)
    asyncio.run(serve(sock, launcher, git_root, owns_path, args.profile, remove_stale(args.profile)))


if __name__ == "__main__":
    main()
