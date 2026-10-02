# ZCode container for rootless Podman

The third Zed agent uses the same per-project Podman boundary as Claude Code
and Codex. Its image pins `zcode-app-cli` 3.14.4-29 and
`zcode-acp-server` 0.52.0. The former is a community-packaged build of
Z.ai's ZCode runtime; the latter bridges its app-server protocol to ACP.
Neither package is an official Z.ai CLI release. See the
[ZCode source](https://github.com/zai-org/ZCode) and the
[CLI package](https://github.com/kingsword09/zcode-cli).

Build from a host terminal:

```sh
cd ~/Git/zed-agent-containers/zcode-container
podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs \
  build -t localhost/zcode-agent:local .
```

The `zcode-agent-state` volume is mounted at `~/.zcode` in all three agent
containers. Configure GLM once there, then ZCode can run in Zed or as a
headless worker invoked by Claude or Codex. On Linux, the CLI supports a
Z.ai/BigModel Coding Plan API key or a custom provider API key. Its OAuth
login flow is currently documented for macOS only. The CLI's provider
configuration lives at `~/.zcode/v2/provider_config.json` and its general
settings at `~/.zcode/cli/setting.json`.

For interactive setup, run from a host terminal after building the image:

```sh
podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs run --rm -it \
  --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
  -v zcode-agent-state:/home/node/.zcode \
  localhost/zcode-agent:local zcode
```

Use the same user mapping as the broker so setup can access the shared state.

Choose the Coding Plan API-key or custom provider option in the setup wizard.
The key is stored in the volume, never in this repository. Test the setup
inside a project with `zcode --prompt 'Reply with OK' --json` before using
the Zed agent. The host smoke test checks the adapter without credentials
or a model request.

For worker tasks, ask Claude or Codex to use ZCode/GLM on a self-contained
subtask. They can run `zcode --prompt '<task>' --json` from the project root
and inspect its answer and changes. This launches a separate ZCode agent;
it does not change their own model or turn a Codex or Claude native subagent
into GLM.

Z.ai also exposes an Anthropic-compatible GLM endpoint, so a separately
configured Claude Code process can use GLM. Setting that endpoint on the
existing Claude ACP container would route its main agent there as well. The
ZCode worker route keeps Claude's current model and login intact. See
[Z.ai's endpoint guide](https://zcode.z.ai/en/docs/configuration).
