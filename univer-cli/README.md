# Univer Workspace CLI in the agent images

All agent images include `uvw`, the [Univer Workspace CLI](https://github.com/dream-num/univer-workspace/tree/main/apps/cli)
(npm package `univer-workspace-cli`; `univer-workspace-cli` is a second name
for the same command). With it, Codex, Claude and OpenCode can work on a Univer
Workspace, such as your own deployment: find files in your Spaces, edit
Sheets, Docs, Slides, Bases and Boards in a reviewable Worktree (a pull
request), render them and export Office files. The agent uses its usual
model subscription; Univer needs no API key, only a Workspace login.

## Setting it up

Rebuild the images and reconnect the agents as described in the
[main README](../README.md). Then, in a Zed thread, ask the agent to connect
to your Workspace and give it the full origin, for example
`https://workspace.example.com`. The agent runs:

```sh
uvw config set workspace.origin https://workspace.example.com
uvw auth login --json
```

and gives you a link and a code. Open the link in your browser, sign in
the way you normally do, check that the code matches and approve. Then tell
the agent, which finishes with `uvw auth login --complete`. No password
or browser cookie passes through the agent.

Each agent has its own login: do this once for each agent you use it with.
You can also log in from a host terminal, in two steps:

```sh
P="podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"
$P run --rm -it --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
  -v codex-agent-state:/home/node/.codex localhost/codex-agent:local \
  sh -c 'uvw config set workspace.origin https://workspace.example.com && uvw auth login'
# approve in the browser, then:
$P run --rm -it --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
  -v codex-agent-state:/home/node/.codex localhost/codex-agent:local uvw auth login --complete
```

For Claude, use `claude-agent-state`, `/home/node/.claude` and
`localhost/claude-agent:local`; for OpenCode, `opencode-agent-state`,
`/home/node/.local/share/opencode` and `localhost/opencode-agent:local`.

## Using it

Ask for the result you want, for example "add a summary sheet to the Q3
budget in my personal Space". The agents load the `univer-workspace-cli`
Skill, then the detailed guidance for that kind of document from the CLI
itself (`uvw skills get core`, `uvw skills get sheet`, …), so the commands
they use always match the installed version. Changes go into a new
Worktree; the agent hands you its review link, and merging is up to you.
Blob uploads and moving or renaming files in a Space change the Space
directly, without a review step.

Screenshots, PDF printing and Slide layout checks render with Chromium,
which is in the images, with the Liberation fonts (metric-compatible with
Arial, Times New Roman and Courier New). Text in fonts the image doesn't
have, such as Chinese, Japanese or Korean, renders with fallback fonts.

## Where the login is kept

`UNIVER_HOME` points into the agent's state volume:
`/home/node/.codex/univer-workspace` in `codex-agent-state`,
`/home/node/.claude/univer-workspace` in `claude-agent-state` and
`/home/node/.local/share/opencode/univer-workspace` in
`opencode-agent-state`. That holds
the configured origin and the Workspace session (`session.json`, readable
only by you). It survives image rebuilds and new containers.

- Every thread of that agent can use the login, in every project, including
  the "+ Teams" variant of the agent. The other agents can't.
- It is a sign-in to your Workspace: whatever runs in that agent's
  containers can read and change what your account can. `uvw auth logout`
  in a thread ends the session.
- Nothing else changes: the agents still have all of `~/Git` read-write and
  an open network. This is not a separate sandbox for Univer work.

The CLI starts a helper process for editing, with its socket in the
container's own `/tmp`. Each container has its own, and it ends with the
container.

## Updating

`uvw` is pinned in `package.json` here, with its dependencies and their
hashes in `package-lock.json`. Its native parts (formula engine, Office
exchange, Typst) are prebuilt packages, so it installs without running
package scripts. Don't install it with `npm install -g` in the agents'
home: `~/.local/bin` comes first on `PATH`, so that copy would replace the
pinned one in every container until you delete it.

`../update.sh` moves the pin to the newest release that is at least three
days old, together with the agents. To pin a version by hand:

```sh
cd ~/Git/zed-agent-containers/univer-cli
npm pkg set dependencies.univer-workspace-cli=VERSION
npm install --package-lock-only --ignore-scripts --no-audit --no-fund
```

`SKILL.md` is `skills/univer-workspace-cli/SKILL.md` from the Univer
Workspace repository, at the release tag of the pinned CLI (now `v0.6.0`).
When the pin moves, replace it with the file from the new tag. The images
put it where each agent reads skills shared by all users
(`/etc/codex/skills`, `/etc/claude-code/.claude/skills` and
`/etc/opencode/skills`), so it never
touches the skills in your state volumes.

## Known issues

- In 0.6.0, `uvw blob download` fails when the Workspace sits behind a proxy
  that compresses responses and drops the `ETag` header, and `uvw blob
  replace` then has no version to give. Univer fixed this after 0.6.0.
