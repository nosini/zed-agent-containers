#!/usr/bin/python3
"""Host smoke test for an installed connector:
host-smoke-test.py codex|claude|codex-teams|claude-teams [PROJECT]

Run from a HOST terminal after install.sh, rebuilding the images and enabling
the <agent>-acp-connector.socket units. PROJECT defaults to this repository.
The Teams profiles also check the relay reaches Teams; with Teams not running
they report SKIP for those checks.
Needs no credentials and sends no model requests. It connects like Zed does
(the client started in the project directory), inspects the per-connection
container while it runs, and checks it is removed afterwards.
"""

import json
import os
import subprocess
import sys
import time

AGENTS = {"codex": ("@agentclientprotocol/codex-acp", "/home/node/.codex"),
          "claude": ("@agentclientprotocol/claude-agent-acp", "/home/node/.claude")}
AGENTS.update({f"{a}-teams": AGENTS[a] for a in list(AGENTS)})
MCP_INIT = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "host-smoke-test", "version": "1"}}})
PODMAN = ["podman", "--storage-opt", "overlay.mount_program=/usr/bin/fuse-overlayfs"]
HOME = os.path.expanduser("~")
GIT = os.path.join(HOME, "Git")
CLIENT = os.path.join(HOME, ".var/app/dev.zed.Zed/data/acp-connector/acp-client.py")
failures = 0


def check(name, ok, detail=""):
    global failures
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    failures += not ok


def sh(*argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


class Client:
    def __init__(self, agent, cwd):
        self.p = subprocess.Popen([sys.executable, "-I", CLIENT], cwd=cwd, env=dict(os.environ, ACP_CONNECTOR=agent),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.n = 0

    def rpc(self, method, params):
        self.n += 1
        self.p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}) + "\n").encode())
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                return {"error": {"message": "connection closed: " + self.p.stderr.read().decode()[-300:]}}
            msg = json.loads(line)
            if msg.get("id") == self.n and "method" not in msg:
                return msg

    def close(self):
        self.p.stdin.close()
        self.p.wait(30)


def containers(agent):
    out = sh(*PODMAN, "ps", "--all", "--format", "{{.Names}}", f"--filter=label=acp-connector.profile={agent}")
    return out.stdout.split()


def main():
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in AGENTS:
        sys.exit(__doc__)
    agent = sys.argv[1]
    repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    project = os.path.realpath(sys.argv[2] if len(sys.argv) == 3 else repo)
    adapter, state = AGENTS[agent]
    teams = agent.endswith("-teams")
    sock = f"/run/user/{os.getuid()}/app/dev.zed.Zed/{agent}-acp.sock"

    check("socket unit active", sh("systemctl", "--user", "-q", "is-active", f"{agent}-acp-connector.socket").returncode == 0)
    check("socket is private (0600)", os.path.exists(sock) and oct(os.stat(sock).st_mode & 0o777) == "0o600")

    before = set(containers(agent))  # containers of Zed threads that are already open
    c = Client(agent, project)
    init = c.rpc("initialize", {"protocolVersion": 1, "clientCapabilities": {
        "fs": {"readTextFile": True, "writeTextFile": True}, "terminal": True, "_meta": {"terminal_output": True}}})
    message = init.get("error", {}).get("message", "")
    if teams and "Teams MCP socket" in message:
        print(f"SKIP Teams profile checks: {message}")
        c.close()
        print(f"failures: {failures}")
        sys.exit(min(failures, 1))
    info = init.get("result", {}).get("agentInfo", {})
    check(f"handshake reaches {adapter}", info.get("name") == adapter, json.dumps(init)[:300])
    check("no _meta reaches the editor", "_meta" not in json.dumps(init))
    check("session outside the project refused",
          c.rpc("session/new", {"cwd": GIT, "mcpServers": []}).get("error", {}).get("code") == -32602)
    check("extension method rejected", c.rpc("_zed/anything", {}).get("error", {}).get("code") == -32601)

    names = [n for n in containers(agent) if n not in before]
    check("a new container for this connection", len(names) == 1, str(names))
    if names:
        name = names[0]
        fmt = "{{range .Mounts}}{{.Destination}}:{{.RW}}{{println}}{{end}}"
        mounts = set(sh(*PODMAN, "inspect", name, "--format", fmt).stdout.split())
        expected = {f"{GIT}:true", f"{state}:true", "/home/node:true"}
        optional = {"/opt/agent-git/gitconfig:false", "/opt/agent-git/ssh-agent.sock:true",
                    "/opt/agent-vm:false", "/opt/agent-vm-client.py:false",
                    "/opt/agent-git/known_hosts:false", "/home/node/memory:true",
                    f"{HOME}/Android/Sdk:false",
                    f"{state}/{'AGENTS.md' if agent.startswith('codex') else 'CLAUDE.md'}:false"}
        if teams:
            expected |= {"/run/teams-office.sock:true", "/run/teams-mcp-relay.py:false"}
        extra = mounts - expected - optional
        check("mounts: ~/Git rw, agent home, state (+ optional extras)",
              expected <= mounts and not extra, str(sorted(mounts)))

        def inside(cmd):
            return sh(*PODMAN, "exec", name, "sh", "-c", cmd)
        probe = os.path.join(project, ".acp-rw-probe")
        check("project is writable", inside(f"touch '{probe}' && rm '{probe}'").returncode == 0)
        probe = os.path.join(GIT, ".acp-rw-probe")
        check("rest of ~/Git is writable", inside(f"touch '{probe}' && rm '{probe}'").returncode == 0)
        check("agent home is writable", inside("touch ~/.acp-probe && rm ~/.acp-probe").returncode == 0)
        if "/home/node/memory:true" in mounts:
            check("shared memory is writable, its git repository not visible",
                  inside("touch ~/memory/.acp-probe && rm ~/memory/.acp-probe && ! test -e ~/memory/.git")
                  .returncode == 0)
        if "/opt/agent-vm:false" in mounts:
            check("VM activation resources are paired and present",
                  "/opt/agent-vm-client.py:false" in mounts and
                  inside("test -S /opt/agent-vm/ssh.sock && test -S /opt/agent-vm/control.sock "
                         "&& test -r /opt/agent-vm-client.py").returncode == 0)
        if teams:
            # The relay the injected teams-office server runs, through the
            # broker's forwarder, to Teams; stdin stays open until it replies.
            relay = inside(f"(printf '%s\\n' '{MCP_INIT}'; sleep 10) | timeout 15 python3 -I /run/teams-mcp-relay.py "
                           "/run/teams-office.sock | head -n 1")
            try:
                result = json.loads(relay.stdout).get("result")
            except (ValueError, AttributeError):
                result = None
            check("Teams MCP answers through the relay", isinstance(result, dict) and "serverInfo" in result,
                  (relay.stdout or relay.stderr)[-300:])
        tools = inside("gcc --version && java -version && python3 -m venv --help && npm --version")
        check("build tools present (gcc, java, venv, npm)", tools.returncode == 0, tools.stderr[-200:])
        office = inside("/opt/office-mcp/docx/bin/python3 -I -c 'import docx_mcp.server' "
                        "&& /opt/office-mcp/excel/bin/excel-mcp-server --help >/dev/null")
        check("Word and Excel MCP servers load", office.returncode == 0, office.stderr[-200:])
        # Offline: no Workspace request, no login. Its settings and login
        # belong in the agent's state volume, not the shared home.
        skill = ("/etc/codex/skills" if agent.startswith("codex") else "/etc/claude-code/.claude/skills") + \
            "/univer-workspace-cli/SKILL.md"
        uvw = inside(f'test "$UNIVER_HOME" = {state}/univer-workspace && test -r {skill} '
                     "&& bash -lc 'uvw --version' && univer-workspace-cli --version "
                     "&& uvw skills get core >/dev/null")
        check("Univer Workspace CLI runs, state in the agent's volume", uvw.returncode == 0,
              (uvw.stderr or uvw.stdout)[-200:])
        browser = inside("uvw screenshot setup --json")
        check("Chromium for Workspace rendering launches", '"resolved"' in browser.stdout,
              (browser.stdout or browser.stderr)[-200:])
        check("host home not visible", inside(f"! ls -d {HOME}/.ssh {HOME}/.config /run/user/*/bus 2>/dev/null").returncode == 0)
        ident = inside(f"git -C '{project}' config user.name").stdout.strip()
        check("git identity configured", bool(ident), "no user.name; see ~/.config/acp-connector/gitconfig")
        agent_sock = inside('test -S "$SSH_AUTH_SOCK" && echo yes').stdout.strip()
        print(f"info SSH agent for signing: {'available' if agent_sock == 'yes' else 'not forwarded'}")
        if "/opt/agent-git/known_hosts:false" in mounts:
            push = inside('case "$GIT_SSH_COMMAND" in *UserKnownHostsFile=/opt/agent-git/known_hosts*) '
                          'grep -c . /opt/agent-git/known_hosts || true;; *) exit 1;; esac')
            check("git's SSH uses the read-only host keys", push.returncode == 0, "GIT_SSH_COMMAND not set")
            push_urls = inside(f"git -C '{project}' config --get-regexp 'url\\..*\\.pushinsteadof'").stdout.split()
            print(f"info push host keys: {push.stdout.strip() or 0}; push rules: {push_urls[1::2] or 'none'}")
    c.close()

    refused = Client(agent, GIT).rpc("initialize", {"protocolVersion": 1})
    check("~/Git itself refused as a project", "open a project inside" in refused.get("error", {}).get("message", "").lower())

    deadline = time.time() + 20
    while set(names) & set(containers(agent)) and time.time() < deadline:
        time.sleep(1)
    check("container removed after disconnect", not set(names) & set(containers(agent)))
    print(f"failures: {failures}")
    sys.exit(min(failures, 1))


if __name__ == "__main__":
    main()
