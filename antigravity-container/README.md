# Antigravity CLI container for rootless Podman

A locally built image for [Antigravity CLI](https://antigravity.google)
(`agy`), Google's coding agent, signed in with a Google account, so the
account's plan applies, including a Google AI Pro or Ultra subscription.
It replaces Gemini CLI, whose Google-account login Google has ended. Gemini
API keys also work through OpenCode (`../opencode-container`). The connector
in `../acp-connector/` starts one container per Zed connection, which all
Antigravity threads on a project share, with all of `~/Git` read-write. See
that README for setup, the ACP policy, tests and limitations.

## Image

- Official `docker.io/library/node:22-bookworm-slim`, pinned by manifest
  digest, with the same Debian packages, Word and Excel MCP servers and
  Univer Workspace CLI as the Codex image (shared layers; see
  `../codex-container/README.md`).
- Antigravity CLI, a single native binary from Google's release bucket,
  pinned in `agy-release.env` by version, download URL and SHA-512. The
  build checks the download against that hash. There is no npm package:
  `../update.sh` reads the list of releases from Google's update server,
  takes the newest one whose download is at least 3 days old, checks it
  against the MD5 the bucket stores for it and records its SHA-512. The
  binary sits in the read-only image, so it can't update itself.
- `antigravity-acp` (`start-agent.py`) runs
  `agy acp --dangerously-skip-permissions --disable-telemetry`, Antigravity
  CLI's own ACP server, with every tool allowed without asking, because
  Podman provides the isolation, and telemetry off. Antigravity CLI reads
  MCP servers and Skills only from `~/.gemini`, the state volume, so before
  that the script writes into it, on every start:
  - the `docx-mcp`, `excel` and `rea` MCP servers, as in the other images,
    in `~/.gemini/config/mcp_config.json`. Other servers there are kept, and
    one of these three stays off once turned off with `agy mcp disable`;
  - the Univer Workspace CLI's Skill, in `~/.gemini/config/skills`.

## State and login

The `antigravity-agent-state` volume (`antigravity-agent-state.volume`,
installed by the connector) is mounted at `~/.gemini`, Antigravity CLI's
directory. It holds the login (in a file, as there is no system keyring in
the container), settings, conversations, MCP servers, Skills and the Univer
Workspace login (`UNIVER_HOME`). Rebuilding doesn't touch it. Secrets of
Antigravity plugins go to `~/.config/antigravity`, which is in the home
shared with the other agents.

The connector always starts this image with the hostname
`antigravity-agent`, and the login container below uses it too, in case a
saved login depends on the machine.

Zed can't run the login: over ACP, Antigravity CLI's Google login needs a
browser inside the container, so the connector offers Zed no login for
this agent. Log in once from a host terminal with a one-off
container using the broker's user mapping and hostname:

```sh
P="podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"
$P run --rm -it --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 --hostname=antigravity-agent \
  -v antigravity-agent-state:/home/node/.gemini localhost/antigravity-agent:local agy acp --login
```

1. Open the printed URL in a browser on the host and sign in with the
   Google account that has the subscription.
2. Paste the authorization code, or the full address of the page the
   browser ends up on, back into the terminal and press Enter. The command
   exits once the login is saved.

Then start a new Antigravity thread in Zed. Until a login exists, starting
a thread fails with a message pointing here. To switch accounts, log out in
an interactive session (`agy` in the same container command, then
`/logout`) and log in again.

Per-project `GEMINI.md` and `AGENTS.md` files in a repository still apply,
as with the other agents.
