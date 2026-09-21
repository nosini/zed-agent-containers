#!/usr/bin/python3
"""Host smoke test for an installed connector: host-smoke-test.py codex|claude [PROJECT]

Run from a HOST terminal after install.sh, rebuilding the images and enabling
the <agent>-acp-connector.socket units. PROJECT defaults to this repository.
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
    sock = f"/run/user/{os.getuid()}/app/dev.zed.Zed/{agent}-acp.sock"

    check("socket unit active", sh("systemctl", "--user", "-q", "is-active", f"{agent}-acp-connector.socket").returncode == 0)
    check("socket is private (0600)", os.path.exists(sock) and oct(os.stat(sock).st_mode & 0o777) == "0o600")

    before = set(containers(agent))  # containers of Zed threads that are already open
    c = Client(agent, project)
    init = c.rpc("initialize", {"protocolVersion": 1, "clientCapabilities": {
        "fs": {"readTextFile": True, "writeTextFile": True}, "terminal": True, "_meta": {"terminal_output": True}}})
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
        expected = {f"{GIT}:false", f"{project}:true", f"{state}:true", "/home/node:true"}
        optional = {"/opt/agent-git/gitconfig:false", "/opt/agent-git/ssh-agent.sock:true",
                    f"{HOME}/Android/Sdk:false", f"{state}/{'AGENTS.md' if agent == 'codex' else 'CLAUDE.md'}:false"}
        extra = mounts - expected - optional
        check("mounts: ~/Git ro, project rw, agent home, state (+ optional ro extras)",
              expected <= mounts and not extra, str(sorted(mounts)))

        def inside(cmd):
            return sh(*PODMAN, "exec", name, "sh", "-c", cmd)
        probe = os.path.join(project, ".acp-rw-probe")
        check("project is writable", inside(f"touch '{probe}' && rm '{probe}'").returncode == 0)
        check("rest of ~/Git is read-only", inside(f"touch '{GIT}/.acp-ro-probe'").returncode != 0)
        check("agent home is writable", inside("touch ~/.acp-probe && rm ~/.acp-probe").returncode == 0)
        tools = inside("gcc --version && java -version && python3 -m venv --help && npm --version")
        check("build tools present (gcc, java, venv, npm)", tools.returncode == 0, tools.stderr[-200:])
        check("host home not visible", inside(f"! ls -d {HOME}/.ssh {HOME}/.config /run/user/*/bus 2>/dev/null").returncode == 0)
        ident = inside(f"git -C '{project}' config user.name").stdout.strip()
        check("git identity configured", bool(ident), "no user.name; see ~/.config/acp-connector/gitconfig")
        agent_sock = inside('test -S "$SSH_AUTH_SOCK" && echo yes').stdout.strip()
        print(f"info SSH agent for signing: {'available' if agent_sock == 'yes' else 'not forwarded'}")
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
