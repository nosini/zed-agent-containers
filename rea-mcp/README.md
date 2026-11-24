# REA MCP proxy

All agent images register [REA](https://github.com/morluto/rea) as the
`rea` MCP server. REA and Ghidra run on the VM (see
[the Codex README](../codex-container/README.md) and
[`../acp-connector/agent-vm.md`](../acp-connector/agent-vm.md)), and
`rea-mcp` in the image connects to them over SSH.

Agents start every configured MCP server when a session opens. Starting
`rea mcp` over SSH directly would boot the VM as soon as a thread is
opened and keep it awake for as long as the thread stays open, since an
open relayed SSH connection prevents the idle shutdown. `rea-mcp` avoids
that:

- It answers the MCP handshake and the tool and prompt lists itself, from
  a saved listing, without connecting.
- The first request that needs REA (a tool call, a prompt, a completion)
  opens the SSH connection and starts `/usr/local/bin/rea mcp` on the VM,
  which boots the VM if it's off. Requests then pass straight through.
- After 30 minutes without requests the connection is closed, so the VM
  can shut down; the next call reconnects. Closing it also closes REA's
  open analysis sessions (`open_binary`), so a later call may need to
  reopen the binary. `REA_MCP_IDLE_MINUTES` in the MCP server's
  environment changes the period; `0` keeps the connection until the
  thread ends.

## The listing

The image contains `rea-listing.json.gz`, the handshake, tools and prompts
of the REA version installed on the VM. Each time `rea-mcp` connects, it
compares REA's lists with the ones it served. If REA has changed, it saves
the new listing to `~/.cache/rea-mcp/listing.json`, uses it from then on,
and tells the agent that the tool list changed.

After upgrading REA on the VM, refresh the snapshot in this directory,
then rebuild the images:

```sh
rea-mcp/rea-mcp.py --snapshot rea-mcp/rea-listing.json.gz
```

This connects with the same SSH command the proxy uses
(`ssh -T -F /home/node/.ssh/agent-vm.conf agent-vm /usr/local/bin/rea mcp`);
a different command can follow `--`, for example
`-- node .../rea-agents/scripts/rea.mjs mcp` for a local copy of the same
REA version. The file is written reproducibly, so an unchanged REA gives
an unchanged file.

## Tests

```sh
python3 -I rea-mcp/tests/test_rea_mcp.py
```

They use a stand-in REA (`tests/mock_rea.py`), not SSH.
