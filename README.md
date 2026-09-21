# zed-agent-containers

Run Codex and Claude Code for Zed (Flatpak) in rootless Podman containers.
Each agent thread gets its own container:

- the project it was opened on is read-write;
- the rest of `~/Git` and the Android SDK are read-only;
- nothing else from your home is visible;
- the agent can't make Zed read, write or run anything on its behalf.

No Flatpak host escape is needed.

| Directory | Contents |
|---|---|
| `acp-connector/` | Host broker (ACP policy, per-project `podman run`), Zed-side client, systemd units, `install.sh`, tests. **Start here.** |
| `codex-container/` | Codex image: `@openai/codex` + `codex-acp`, pinned |
| `claude-container/` | Claude Code image: `claude-agent-acp` + native CLI, pinned |

Quick start, from a host terminal:

```sh
./install.sh
```

It builds both images, runs `acp-connector/install.sh`, enables the
socket units and runs the smoke tests. `--no-build` skips the image
builds and `--no-test` skips the tests. Then log in once per agent (see
the container READMEs). The steps it runs:

```sh
P="podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"
(cd codex-container && $P build -t localhost/codex-agent:local .)
(cd claude-container && $P build -t localhost/claude-agent:local .)
acp-connector/install.sh
systemctl --user enable --now codex-acp-connector.socket claude-acp-connector.socket
acp-connector/tests/host-smoke-test.py codex && acp-connector/tests/host-smoke-test.py claude
```

`acp-connector/install.sh` copies what runs on the host out of this repository, so
moving or editing the repo doesn't affect a running setup until you
reinstall. Tests that need no Podman:
`cd acp-connector && python3 -m unittest -v tests/test_connector.py`.
