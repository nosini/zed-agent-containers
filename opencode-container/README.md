# OpenCode container for rootless Podman

A locally built image for [OpenCode](https://opencode.ai), the agent for
models other than Claude: your ChatGPT subscription when you're out of Codex
usage, Mistral, OpenRouter, Gemini and other API-key providers, and local
models. Claude stays with Claude Code (`../claude-container`): Anthropic
doesn't allow Claude Pro/Max logins in other harnesses, and OpenCode only
offers an Anthropic API key. The connector in `../acp-connector/` starts one
container per Zed connection, which all OpenCode threads on a project share,
with all of `~/Git` read-write. See that README for setup, the ACP policy,
tests and limitations.

## Image

- Official `docker.io/library/node:22-bookworm-slim`, pinned by manifest
  digest, with the same Debian packages, Word and Excel MCP servers and
  Univer Workspace CLI as the Codex image (shared layers; see
  `../codex-container/README.md`).
- `@opencode/cli` 2.2.6 (OpenCode 2), with integrity hashes in
  `package-lock.json`. Its install script normally replaces the launcher
  with the binary for your platform; scripts are disabled, so the build
  links the locked `@opencode/cli-linux-x64` binary in its place and removes
  the other Linux variants. The image is amd64-only, like the others.
- `opencode-acp` runs `opencode acp`, OpenCode's own ACP server. It starts a
  private `opencode serve --stdio` child, not the shared background service
  the interactive CLI uses.
- `/etc/opencode/opencode.json`, OpenCode's machine-wide config, from
  `opencode.json` here. In the image, so rebuilds update it, and it takes
  precedence over the user and project configs:
  - every tool is allowed without asking (`permissions`), because Podman
    provides the isolation;
  - updates are off (`update`, and `OPENCODE_DISABLE_AUTOUPDATE`): pins
    move with `../update.sh`;
  - session sharing to opencode.ai is off (`share`);
  - the connector's environment notes are read from the state volume
    (`instructions`; OpenCode would otherwise look in `~/.config/opencode`,
    which is in the shared home);
  - the `docx-mcp`, `excel` and `rea` MCP servers, as in the other images.
- The Univer Workspace CLI's Skill in `/etc/opencode/skills`.

## State and login

The `opencode-agent-state` volume (`opencode-agent-state.volume`,
installed by the connector) is mounted at `~/.local/share/opencode`,
OpenCode's data directory. It holds your logins and API keys (in
`opencode.db`), sessions, logs and the Univer Workspace login
(`UNIVER_HOME`). Rebuilding doesn't touch it. The rest of `~/.config`,
`~/.cache` and `~/.local` is the home shared with Codex and Claude, so
keep secrets out of `~/.config/opencode`.

Log in with a one-off container using the broker's user mapping, so it can
read and update the private state files in the existing volume. Connect
each provider you want; you can repeat this any time:

```sh
P="podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"
run() { $P run --rm -it --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
  -v opencode-agent-state:/home/node/.local/share/opencode localhost/opencode-agent:local "$@"; }
run opencode auth login openai --method chatgpt-headless --standalone  # ChatGPT Plus/Pro (device code)
run opencode auth login mistral --standalone                           # Mistral API key
run opencode auth login --standalone                                   # pick any other provider
run opencode auth list --standalone
```

Without a login OpenCode still works with its free "OpenCode Zen" models
(`opencode/...`), which send your prompts to OpenCode's own service. Pick
the model per thread in Zed's model selector. To set a default, put
`"model": "<provider>/<model>"` (from `run opencode models --standalone`)
in `~/.config/opencode/opencode.json`, in the shared home.

Per-project `opencode.json` and `AGENTS.md` files in a repository still
apply, as with the other agents; `/etc/opencode/opencode.json` takes
precedence over them.
