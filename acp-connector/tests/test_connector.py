"""End-to-end tests: fake Zed -> real client -> socket -> broker -> agent.

Run inside or outside the Flatpak: python3 -m unittest -v tests/test_connector.py
Needs no Podman and no host escape; the agent is a hostile mock (or, for the
interop tests, the real codex-acp / claude-agent-acp with throwaway state).
"""

import importlib.util
import json
import os
import queue
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.join(HERE, "..", "acp-client.py")
HARNESS = os.path.join(HERE, "run_broker.py")
CODEX_ACP_JS = next((p for p in (
    # Inside the codex image, else Zed's copy.
    "/opt/agent/node_modules/@agentclientprotocol/codex-acp/dist/index.js",
    os.path.expanduser("~/.var/app/dev.zed.Zed/data/zed/external_agents/registry/npx/codex-acp/"
                       "node_modules/@agentclientprotocol/codex-acp/dist/index.js")) if os.path.exists(p)), "")
# The tree installed from claude-container/package-lock.json (npm ci), the
# claude image's own copy, else Zed's copy.
CLAUDE_ACP_BIN = next((p for p in (
    os.environ.get("CLAUDE_AGENT_ACP", ""),
    "/tmp/claude-agent-tree/node_modules/.bin/claude-agent-acp",
    "/opt/agent/node_modules/.bin/claude-agent-acp",
    os.path.expanduser("~/.var/app/dev.zed.Zed/data/zed/external_agents/registry/npx/claude-acp/"
                       "node_modules/.bin/claude-agent-acp")) if p and os.path.exists(p)), None)
FORBIDDEN_PREFIXES = ("fs/", "terminal/", "mcp/", "elicitation/", "_")


def find_meta(obj):
    if isinstance(obj, dict):
        return "_meta" in obj or any(find_meta(v) for v in obj.values())
    if isinstance(obj, list):
        return any(find_meta(v) for v in obj)
    return False


def wait_for(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def pid_alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[1][0] != "Z"
    except FileNotFoundError:
        return False


class FakeZed:
    """Plays Zed: launches the real client and speaks ACP over its stdio."""

    def __init__(self, sock, args=(), cwd=None):
        env = dict(os.environ, ACP_CONNECTOR_SOCKET=sock)
        # Zed starts agent servers in the project root; the broker relies on it.
        self.proc = subprocess.Popen([sys.executable, "-I", CLIENT, *args], env=env, cwd=cwd,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.received = []
        self.responses = queue.Queue()
        self.next_id = 100
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            msg = json.loads(line)
            self.received.append(msg)
            if msg.get("method") == "session/request_permission":
                self.send({"jsonrpc": "2.0", "id": msg["id"],
                           "result": {"outcome": {"outcome": "selected", "optionId": "allow"}, "_meta": {"z": 1}}})
            elif "method" not in msg:
                self.responses.put(msg)

    def send(self, msg):
        self.raw(json.dumps(msg).encode())

    def raw(self, data):
        with self.lock:
            self.proc.stdin.write(data + b"\n")
            self.proc.stdin.flush()

    def request(self, method, params, timeout=15):
        self.next_id += 1
        mid = self.next_id
        self.send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        end = time.time() + timeout
        while True:
            msg = self.responses.get(timeout=max(0.01, end - time.time()))
            if msg.get("id") == mid:
                return msg

    def notify(self, method, params):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def initialize(self):
        return self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": True,
                                   "elicitation": {"url": {}, "form": {}},
                                   "_meta": {"terminal_output": True, "terminal-auth": True}},
            "clientInfo": {"name": "zed", "version": "1.20.2", "_meta": {"x": 1}}})

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


class BrokerTest(unittest.TestCase):
    mode = "mock"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acp-test-")
        self.git = os.path.join(self.tmp, "Git")
        self.root = os.path.join(self.git, "project")
        os.makedirs(os.path.join(self.root, "src"))
        os.makedirs(os.path.join(self.git, "other"))
        self.sock = os.path.join(self.tmp, "run", "codex-acp.sock")
        self.mock_log = os.path.join(self.tmp, "mock.log")
        self.broker_log = open(os.path.join(self.tmp, "broker.log"), "w+")
        self.zeds = []

    def start_broker(self, *extra, env=None):
        env = env or dict(os.environ, MOCK_LOG=self.mock_log, MOCK_ROOT=self.root)
        if self.mode == "activate":
            os.makedirs(os.path.dirname(self.sock), exist_ok=True)
        started = self.broker_text().count("listening;")
        self.broker = subprocess.Popen([sys.executable, HARNESS, self.mode, self.sock, self.git, *extra],
                                       env=env, stderr=self.broker_log)
        self.assertTrue(wait_for(lambda: self.broker_text().count("listening;") > started),
                        "broker did not listen")

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
            proc.wait(20)

    def tearDown(self):
        for z in self.zeds:
            z.close()
        if getattr(self, "broker", None) and self.broker.poll() is None:
            self.broker.send_signal(signal.SIGTERM)
            self.broker.wait(20)
        self.broker_log.close()
        shutil.rmtree(self.tmp)

    def zed(self, *args, cwd=None):
        z = FakeZed(self.sock, args, cwd or self.root)
        self.zeds.append(z)
        return z

    def mock_entries(self, kind=None):
        if not os.path.exists(self.mock_log):
            return []
        with open(self.mock_log) as f:
            entries = [json.loads(l) for l in f]
        return [e for e in entries if kind is None or e["kind"] == kind]

    def agent_saw(self, method):
        return [e["obj"] for e in self.mock_entries("in") if e["obj"].get("method") == method]

    def broker_text(self):
        self.broker_log.flush()
        with open(self.broker_log.name) as f:
            return f.read()


class PolicyTests(BrokerTest):
    def setUp(self):
        super().setUp()
        self.start_broker()
        self.z = self.zed()
        self.init = self.z.initialize()
        self.new = self.z.request("session/new", {
            "cwd": self.root, "additionalDirectories": ["/home"],
            "mcpServers": [{"name": "fs", "command": "/bin/sh", "args": ["-c", "id"], "env": []},
                           {"type": "http", "name": "web", "url": "http://127.0.0.1:1", "headers": []}]})

    def test_socket_is_private(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.sock).st_mode), 0o600)

    def test_initialize_capabilities_rewritten_both_ways(self):
        seen = self.agent_saw("initialize")[0]["params"]
        self.assertEqual(seen["clientCapabilities"],
                         {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False})
        self.assertFalse(find_meta(seen))
        result = self.init["result"]
        self.assertFalse(find_meta(result))
        # terminal/env_var methods removed; "evil" survives only as a plain agent method.
        self.assertEqual(result["authMethods"], [{"id": "api-key", "name": "API key"},
                                                 {"id": "evil", "name": "Login"}])
        caps = result["agentCapabilities"]
        self.assertEqual(caps["mcpCapabilities"], {"http": False, "sse": False})
        self.assertNotIn("providers", caps)
        self.assertEqual(caps["sessionCapabilities"], {"list": {}})
        self.assertEqual(caps["promptCapabilities"], {"image": True, "embeddedContext": True})

    def test_mcp_servers_and_extra_dirs_removed(self):
        self.assertIn("result", self.new)
        seen = self.agent_saw("session/new")[0]["params"]
        self.assertEqual(seen["mcpServers"], [])
        self.assertNotIn("additionalDirectories", seen)
        self.assertFalse(find_meta(self.new))

    def test_cwd_outside_project_rejected(self):
        for cwd in ("/", self.root + "/../other", os.path.dirname(self.root), "project", self.root + "x"):
            resp = self.z.request("session/new", {"cwd": cwd, "mcpServers": []})
            self.assertEqual(resp["error"]["code"], -32602, cwd)
        self.assertEqual(self.z.request("session/new", {"cwd": self.root + "/src", "mcpServers": []})
                         ["result"]["sessionId"], "s1")
        self.assertEqual(len(self.agent_saw("session/new")), 2)

    def test_client_side_unknown_methods_blocked(self):
        for m in ("_zed/anything", "nes/start", "providers/set", "mcp/message", "document/didOpen", "fs/read_text_file"):
            self.assertEqual(self.z.request(m, {})["error"]["code"], -32601, m)
        self.z.notify("document/didOpen", {"uri": "file:///etc/passwd", "text": "secret"})
        self.z.notify("_zed/ping", {})
        self.z.send({"jsonrpc": "2.0", "id": "never-asked", "result": {"outcome": "x"}})
        self.z.raw(b"garbage")
        self.z.request("session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "hi"}]})
        seen = [e["obj"] for e in self.mock_entries("in")]
        self.assertEqual({m.get("method") for m in seen}, {"initialize", "session/new", "session/prompt"})

    def test_agent_bypass_attempts(self):
        resp = self.z.request("session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "attack"}]})
        self.assertEqual(resp["result"], {"stopReason": "end_turn"})
        received = self.z.received

        # Nothing but filtered session updates and one permission request reached "Zed".
        methods = [m["method"] for m in received if "method" in m]
        self.assertEqual(set(methods), {"session/update", "session/request_permission"})
        self.assertEqual(methods.count("session/request_permission"), 1)
        self.assertFalse(any(m.startswith(FORBIDDEN_PREFIXES) for m in methods))
        self.assertFalse(find_meta(received), "_meta leaked to editor")
        responses = [m for m in received if "method" not in m]
        self.assertFalse(any(r.get("id") in (12345, "p1") for r in responses), "forged response forwarded")

        # Tool calls: only in-project locations/diffs, no terminal content.
        updates = [m["params"]["update"] for m in received if m.get("method") == "session/update"]
        kinds = [u["sessionUpdate"] for u in updates]
        self.assertEqual(kinds, ["tool_call", "tool_call_update", "agent_message_chunk"])
        tc = updates[0]
        # Locations anywhere in the git root are shown (normalized); nothing outside it.
        self.assertEqual(tc["locations"], [{"path": self.root + "/src/a.py", "line": 3},
                                           {"path": self.git + "/other/x.py"}])
        self.assertEqual([c["type"] for c in tc["content"]], ["diff", "content"])
        self.assertEqual(tc["content"][0]["path"], self.root + "/src/a.py")
        perm = next(m for m in received if m.get("method") == "session/request_permission")
        self.assertEqual(perm["params"]["toolCall"]["locations"], [{"path": self.root + "/a.py"}])
        self.assertEqual(perm["params"]["toolCall"]["content"], [])

        # The agent got a policy error for every blocked request, and the permission answer.
        answers = {json.dumps(e["obj"]["id"]): e["obj"] for e in self.mock_entries("response")}
        blocked = ["w1", "r1", "r2", "t1", "t2", "t3", "t4", "t5", "m1", "m2", "e1", "x1", "x2", "x3", "x4", "x5",
                   7, "dup"]
        for mid in blocked:
            self.assertEqual(answers[json.dumps(mid)]["error"]["code"], -32601, mid)
        for mid in ("bad1", "bad2"):
            self.assertEqual(answers[json.dumps(mid)]["error"]["code"], -32600, mid)
        self.assertEqual(answers['"p1"']["result"], {"outcome": {"outcome": "selected", "optionId": "allow"}})
        self.assertFalse(os.path.exists(os.path.join(self.root, "pwned.txt")))
        self.assertIn("blocked agent request 'fs/write_text_file'", self.broker_text())


class ThreadTitleTests(unittest.TestCase):
    """Zed shows all custom agents' threads with one icon, so titles get a prefix."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("broker", os.path.join(HERE, "..", "acp-broker.py"))
        cls.broker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.broker)

    def policy(self, label="Claude"):
        p = self.broker.Policy("/g/p", "/g", "t", label)
        p.client_pending[self.broker.id_key(1)] = "session/list"
        return p

    def info_update(self, p, title):
        msg = {"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s", "update": {"sessionUpdate": "session_info_update", "title": title}}}
        to_client, _ = p.from_agent(msg)
        return to_client[0]["params"]["update"]["title"]

    def test_session_info_update_is_prefixed_once(self):
        p = self.policy()
        self.assertEqual(self.info_update(p, "Fix the build"), "[Claude] Fix the build")
        self.assertEqual(self.info_update(p, "[Claude] Fix the build"), "[Claude] Fix the build")
        self.assertIsNone(self.info_update(p, None))
        self.assertEqual(self.info_update(self.policy("Codex"), "Fix the build"), "[Codex] Fix the build")

    def test_session_list_titles_are_prefixed(self):
        p = self.policy()
        result = {"sessions": [{"sessionId": "a", "cwd": "/g/p", "title": "One", "_meta": {"x": 1}},
                               {"sessionId": "b", "cwd": "/g/p", "title": None},
                               {"sessionId": "c", "cwd": "/g/p"}, "junk"]}
        to_client, _ = p.from_agent({"jsonrpc": "2.0", "id": 1, "result": result})
        self.assertEqual(to_client[0]["result"]["sessions"], [
            {"sessionId": "a", "cwd": "/g/p", "title": "[Claude] One"},
            {"sessionId": "b", "cwd": "/g/p", "title": None},
            {"sessionId": "c", "cwd": "/g/p"}, "junk"])

    def test_no_label_leaves_titles_alone(self):
        self.assertEqual(self.info_update(self.policy(None), "Fix the build"), "Fix the build")

    def test_production_profiles_have_labels(self):
        self.assertEqual({k: v["label"] for k, v in self.broker.PROFILES.items()},
                         {"codex": "Codex", "claude": "Claude"})


class LifecycleTests(BrokerTest):
    def test_disconnect_stops_agent(self):
        self.start_broker()
        z = self.zed()
        z.initialize()
        pid = self.mock_entries("pid")[0]["obj"]
        self.assertTrue(pid_alive(pid))
        z.close()
        self.assertEqual(z.proc.returncode, 0)
        self.assertTrue(wait_for(lambda: not pid_alive(pid)), "agent survived client disconnect")

    def test_agent_ignoring_eof_and_sigterm_is_killed(self):
        self.start_broker()
        z = self.zed()
        z.initialize()
        z.request("session/new", {"cwd": self.root, "mcpServers": []})
        z.request("session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "ignore_eof"}]})
        pid = self.mock_entries("pid")[0]["obj"]
        start = time.time()
        z.close()
        self.assertTrue(wait_for(lambda: not pid_alive(pid), 15), "agent not killed")
        self.assertGreater(time.time() - start, 4)

    def test_agent_crash_reports_pending_request(self):
        self.start_broker()
        z = self.zed()
        z.initialize()
        resp = z.request("session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "exit"}]})
        self.assertIn("agent exited", resp["error"]["message"])
        z.proc.wait(10)
        self.assertEqual(z.proc.returncode, 0)

    def test_session_limit(self):
        self.start_broker()
        for _ in range(4):  # MAX_SESSIONS
            self.zed().initialize()
        resp = self.zed().initialize()
        self.assertIn("busy", resp["error"]["message"])
        self.zeds[0].close()
        time.sleep(0.5)
        self.assertIn("result", self.zed().initialize())

    def test_oversized_message_closes_only_that_connection(self):
        self.start_broker("1048576")
        z = self.zed()
        z.initialize()
        z.raw(b'{"jsonrpc":"2.0","id":1,"method":"session/prompt","params":{"x":"' + b"A" * 2_000_000 + b'"}}')
        z.proc.wait(15)
        self.assertIn("result", self.zed().initialize())

    def test_stale_socket_replaced_and_regular_file_refused(self):
        self.start_broker()
        self.broker.kill()
        self.broker.wait()
        self.assertTrue(os.path.exists(self.sock))
        self.start_broker()
        self.assertIn("result", self.zed().initialize())
        self.broker.terminate()
        self.broker.wait(20)
        self.assertFalse(os.path.exists(self.sock), "manual mode should remove its socket")
        open(self.sock, "w").close()
        proc = subprocess.run([sys.executable, HARNESS, "mock", self.sock, self.git],
                              capture_output=True, timeout=20)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"not a socket", proc.stderr)

    def test_client_rejects_arguments_and_reports_missing_socket(self):
        p = subprocess.run([sys.executable, "-I", CLIENT, "--login"], capture_output=True)
        self.assertEqual(p.returncode, 2)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ACP_CONNECTOR")}
        p = subprocess.run([sys.executable, "-I", CLIENT], env=env, capture_output=True, input=b"")
        self.assertEqual(p.returncode, 2)
        self.assertIn(b"set ACP_CONNECTOR", p.stderr)
        p = subprocess.run([sys.executable, "-I", CLIENT], env=dict(env, ACP_CONNECTOR="../x"),
                           capture_output=True, input=b"")
        self.assertEqual(p.returncode, 2)
        env.update(ACP_CONNECTOR="claude", ACP_CONNECTOR_SOCKET=self.sock)
        p = subprocess.run([sys.executable, "-I", CLIENT], env=env, capture_output=True, input=b"")
        self.assertEqual(p.returncode, 1)
        self.assertIn(b"systemctl --user enable --now claude-acp-connector.socket", p.stderr)


class ProjectTests(BrokerTest):
    """The project comes from the client's working directory and is validated."""

    def setUp(self):
        super().setUp()
        self.start_broker()

    def project_seen(self):
        return [e["obj"] for e in self.mock_entries("project")]

    def test_project_is_client_cwd(self):
        z = self.zed()
        self.assertIn("result", z.initialize())
        self.assertEqual(self.project_seen(), [self.root])
        other = os.path.join(self.git, "other")
        self.assertIn("result", self.zed(cwd=other).initialize())
        self.assertEqual(self.project_seen(), [self.root, other])
        # The session must stay in the connection's own project.
        self.assertEqual(z.request("session/new", {"cwd": other, "mcpServers": []})["error"]["code"], -32602)

    def test_symlink_inside_git_resolves_to_real_project(self):
        link = os.path.join(self.git, "link")
        os.symlink(self.root, link)
        self.assertIn("result", self.zed(cwd=link).initialize())
        self.assertEqual(self.project_seen(), [self.root])

    def test_refused_projects(self):
        outside = os.path.join(self.tmp, "outside")
        comma = os.path.join(self.git, "a,b")
        for d in (outside, comma):
            os.makedirs(d)
        escape = os.path.join(self.git, "escape")
        os.symlink(outside, escape)
        for cwd, why in ((self.git, "open a project inside"), (outside, "open a project inside"),
                         (escape, "open a project inside"), (comma, "unsupported project path"),
                         ("/", "open a project inside")):
            with self.subTest(cwd=cwd):
                resp = self.zed(cwd=cwd).initialize()
                self.assertIn(why, resp["error"]["message"].lower())
        self.assertEqual(self.project_seen(), [], "no agent may start for a refused project")


class ActivationTest(BrokerTest):
    """Production entry point with systemd-style activation and the fixed Podman argv."""
    mode = "activate"

    def test_socket_activation_and_unavailable_container(self):
        gitconfig = os.path.join(self.tmp, "gitconfig")
        open(gitconfig, "w").close()
        agent_sock = os.path.join(self.tmp, "agent.sock")
        sdk = os.path.join(self.tmp, "Android", "Sdk")
        os.makedirs(sdk)
        notes = os.path.join(self.tmp, "agent-environment.md")
        open(notes, "w").close()
        known_hosts = os.path.join(self.tmp, "known_hosts")
        open(known_hosts, "w").close()
        with socket.socket(socket.AF_UNIX) as s:
            s.bind(agent_sock)
        for profile, image, entry, state, notes_dst in (
                ("codex", "codex-agent", "codex-acp", "/home/node/.codex", "/home/node/.codex/AGENTS.md"),
                ("claude", "claude-agent", "claude-agent-acp", "/home/node/.claude", "/home/node/.claude/CLAUDE.md")):
            with self.subTest(profile=profile):
                if os.path.exists(self.sock):
                    os.unlink(self.sock)
                self.start_broker(profile, "--git-config", gitconfig, "--ssh-agent-socket", agent_sock,
                                  "--android-sdk", sdk, "--instructions", notes, "--known-hosts", known_hosts)
                self.addCleanup(self.stop, self.broker)
                resp = self.zed().initialize()
                line = next(l for l in self.broker_text().splitlines() if "agent: /usr/bin/podman" in l)
                argv = shlex.split(line.split("agent: ", 1)[1])
                self.assertEqual(argv[:6], ["/usr/bin/podman", "--storage-opt",
                                            "overlay.mount_program=/usr/bin/fuse-overlayfs", "run", "--rm",
                                            "--interactive"])
                for flag in ("--read-only", "--read-only-tmpfs=true", "--cap-drop=all", "--pull=never",
                             "--security-opt=no-new-privileges", "--userns=keep-id:uid=1000,gid=1000",
                             "--log-driver=none", f"--label=acp-connector.profile={profile}",
                             f"--workdir={self.root}", f"--env=SSH_AUTH_SOCK=/opt/agent-git/ssh-agent.sock",
                             "--pids-limit=4096", f"--env=ANDROID_HOME={sdk}",
                             "--env=GIT_SSH_COMMAND=ssh -o UserKnownHostsFile=/opt/agent-git/known_hosts"
                             " -o StrictHostKeyChecking=yes"):
                    self.assertIn(flag, argv)
                mounts = sorted(a for a in argv if a.startswith("--mount="))
                self.assertEqual(mounts, sorted([
                    "--mount=type=volume,src=agent-home,dst=/home/node",
                    f"--mount=type=volume,src={image}-state,dst={state}",
                    f"--mount=type=bind,src={sdk},dst={sdk},ro=true",
                    f"--mount=type=bind,src={notes},dst={notes_dst},ro=true",
                    f"--mount=type=bind,src={self.git},dst={self.git},ro=true",
                    f"--mount=type=bind,src={self.root},dst={self.root}",
                    f"--mount=type=bind,src={gitconfig},dst=/opt/agent-git/gitconfig,ro=true",
                    f"--mount=type=bind,src={known_hosts},dst=/opt/agent-git/known_hosts,ro=true",
                    f"--mount=type=bind,src={agent_sock},dst=/opt/agent-git/ssh-agent.sock"]))
                self.assertEqual(argv[-2:], [f"localhost/{image}:local", entry])
                self.broker.terminate()
                self.broker.wait(20)
                self.assertTrue(os.path.exists(self.sock), "broker removed the systemd-owned socket path")
                if not os.path.exists("/usr/bin/podman"):
                    self.assertIn("could not be started", resp["error"]["message"])
                    self.assertIn(f"journalctl --user -u {profile}-acp-connector", resp["error"]["message"])
                os.unlink(self.broker_log.name)
                self.broker_log.close()
                self.broker_log = open(os.path.join(self.tmp, "broker.log"), "w+")

    def test_profile_is_required_and_fixed(self):
        broker = os.path.join(HERE, "..", "acp-broker.py")
        for extra in ([], ["--profile", "other"], ["--profile", "/bin/sh"],
                      ["--profile", "codex", "--android-sdk", os.path.join(self.tmp, "missing")],
                      ["--profile", "codex", "--android-sdk", self.git + "/a,b"],
                      ["--profile", "codex", "--instructions", self.root],
                      ["--profile", "codex", "--known-hosts", os.path.join(self.tmp, "missing")]):
            p = subprocess.run([sys.executable, "-I", broker, "--git-root", self.git, "--socket", self.sock,
                                *extra], capture_output=True, timeout=20)
            self.assertEqual(p.returncode, 2, extra)
        self.assertFalse(os.path.exists(self.sock))


@unittest.skipUnless(os.path.exists(CODEX_ACP_JS) and shutil.which("node"), "codex-acp not available")
class RealCodexAcpTest(BrokerTest):
    """Interop with the real codex-acp 1.12.0, offline, with throwaway state."""
    mode = "real"

    def test_handshake_through_policy(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".codex"))
        env = {"PATH": os.path.dirname(shutil.which("node")) + ":/usr/bin:/bin", "HOME": home,
               "CODEX_HOME": os.path.join(home, ".codex"), "NO_BROWSER": "1", "LANG": "C.UTF-8"}
        self.start_broker("node", CODEX_ACP_JS, env=env)
        z = self.zed()
        init = z.initialize()
        self.assertEqual(init["result"]["agentInfo"]["name"], "@agentclientprotocol/codex-acp")
        self.assertEqual([m["id"] for m in init["result"]["authMethods"]], ["api-key"])
        self.assertFalse(find_meta(init))
        new = z.request("session/new", {"cwd": self.root, "mcpServers": [
            {"name": "x", "command": "/bin/sh", "args": [], "env": []}]}, timeout=60)
        # Without credentials codex-acp asks for authentication; with the
        # rewritten capabilities it must not have called back into the editor.
        self.assertTrue("result" in new or "error" in new)
        time.sleep(1)
        methods = {m.get("method") for m in z.received if "method" in m}
        self.assertTrue(methods <= {"session/update"}, methods)
        self.assertIn("removed 1 forwarded MCP server", self.broker_text())


@unittest.skipUnless(CLAUDE_ACP_BIN and shutil.which("node"), "claude-agent-acp not available")
class RealClaudeAgentAcpTest(BrokerTest):
    """Interop with the real claude-agent-acp 0.79.0, with throwaway state and a read-only HOME."""
    mode = "real"

    def test_handshake_through_policy(self):
        home = os.path.join(self.tmp, "home")
        state = os.path.join(self.tmp, "state")
        tmp = os.path.join(self.tmp, "agent-tmp")
        os.makedirs(home, mode=0o555)
        os.makedirs(tmp)
        os.makedirs(state)
        shutil.copy(os.path.join(HERE, "..", "..", "claude-container", "settings.json"), state)
        env = {"PATH": os.path.dirname(shutil.which("node")) + ":/usr/bin:/bin", "HOME": home,
               "CLAUDE_CONFIG_DIR": state, "TMPDIR": tmp, "NO_BROWSER": "1",
               "DISABLE_AUTOUPDATER": "1", "LANG": "C.UTF-8"}
        self.start_broker(CLAUDE_ACP_BIN, env=env)
        z = self.zed()
        init = z.initialize()
        self.assertEqual(init["result"]["agentInfo"]["name"], "@agentclientprotocol/claude-agent-acp")
        # Every login method it offers is terminal-auth, which the connector removes.
        self.assertEqual(init["result"]["authMethods"], [])
        self.assertFalse(find_meta(init))
        new = z.request("session/new", {"cwd": self.root, "mcpServers": [
            {"name": "x", "command": "/bin/sh", "args": [], "env": []}]}, timeout=60)
        self.assertEqual(new["result"]["modes"]["currentModeId"], "bypassPermissions")
        self.assertFalse(find_meta(new))
        time.sleep(1)
        methods = {m.get("method") for m in z.received if "method" in m}
        self.assertTrue(methods <= {"session/update"}, methods)
        log = self.broker_text()
        self.assertIn("removed 1 forwarded MCP server", log)
        self.assertIn("blocked agent notification '_auth/status_update'", log)
        self.assertEqual(os.listdir(home), [])


if __name__ == "__main__":
    unittest.main()
