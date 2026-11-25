#!/usr/bin/python3
"""Start Antigravity CLI's ACP server for the connector.

Antigravity CLI reads its MCP servers and Skills only from ~/.gemini, which
is the state volume, so the image's servers and the Univer Workspace Skill
are written there on every start (rebuilds update them). Other servers and
Skills there are left alone. Then it runs `agy acp` with every tool allowed
without asking, because Podman provides the isolation, and telemetry off.
`--setup-only` stops after the setup, for tests.
"""

import json
import os
import shutil
import sys
import tempfile

HOME = os.path.expanduser("~")
CONFIG = os.path.join(HOME, ".gemini", "config")
MCP_CONFIG = os.path.join(CONFIG, "mcp_config.json")
SKILLS = os.path.join(CONFIG, "skills")
IMAGE_SKILLS = "/opt/agent/skills"
SERVERS = {
    "rea": {"command": "/usr/local/bin/rea-mcp", "args": []},
    # Running the module skips docx-mcp's install of a Claude Code skill.
    "docx-mcp": {"command": "/opt/office-mcp/docx/bin/python3", "args": ["-m", "docx_mcp.server"]},
    "excel": {"command": "/opt/office-mcp/excel/bin/excel-mcp-server", "args": ["stdio"]},
}
ACP = ["/usr/local/bin/agy", "acp", "--dangerously-skip-permissions", "--disable-telemetry"]


def write_json(path, data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".mcp_config.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def setup():
    os.makedirs(SKILLS, exist_ok=True)
    try:
        with open(MCP_CONFIG) as f:
            config = json.load(f)
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = config["mcpServers"] = {}
    changed = False
    for name, spec in SERVERS.items():
        old = servers.get(name)
        # Keep a server the user disabled (`agy mcp disable`) disabled.
        new = {**spec, "disabled": bool(isinstance(old, dict) and old.get("disabled"))}
        if old != new:
            servers[name] = new
            changed = True
    if changed:
        write_json(MCP_CONFIG, config)
    for name in os.listdir(IMAGE_SKILLS):
        target = os.path.join(SKILLS, name)
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(os.path.join(IMAGE_SKILLS, name), target)


def main():
    try:
        setup()
    except OSError as exc:
        # The agent still works without them; say why in the container log.
        print(f"start-agent: could not set up MCP servers and Skills: {exc}", file=sys.stderr)
    if sys.argv[1:] == ["--setup-only"]:
        return
    os.execv(ACP[0], ACP + sys.argv[1:])


if __name__ == "__main__":
    main()
