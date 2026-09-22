# zed-agent-containers

Run Codex and Claude Code for Zed (Flatpak) in rootless Podman containers.
Each agent gets its own container for the project it's working on (Zed
runs all of an agent's threads on a project over one connection, so they
share that container):

- the project it was opened on is read-write;
- the rest of `~/Git` and the Android SDK are read-only;
- nothing else from your home is visible;
- the agent can't make Zed read, write or run anything on its behalf.

The host escape feature in Zed is not needed.

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

To update the agents, run `./update.sh` from a host terminal. It moves the
pinned npm packages to their newest releases that are at least 3 days old
(`--min-age=N` to change that, `0` for the latest), so a compromised
release has time to be caught before you pin it. It also moves the Node
base image digest to the latest and rebuilds both images without cache,
which refreshes the Debian packages; those aren't delayed, since they
carry security fixes. Nothing updates automatically, so run it now and
then, and whenever you want a new Claude model: the model list comes from
the pinned Claude Code version, and it arrives 3 days after its release
unless you pass `--min-age=0`. `./update.sh --check` only compares versions,
and `--no-build` only bumps the pins. It commits nothing; review
`git diff` and commit the new pins.

`acp-connector/install.sh` copies what runs on the host out of this repository, so
moving or editing the repo doesn't affect a running setup until you
reinstall. Tests that need no Podman:
`cd acp-connector && python3 -m unittest -v tests/test_connector.py`.
