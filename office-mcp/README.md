# Word and Excel MCP servers

All agent images include two MCP servers. Claude, Codex and OpenCode
register them as `docx-mcp` and `excel`:

- [docx-mcp](https://github.com/SecurityRonin/docx-mcp) (`docx-mcp-server`
  on PyPI) creates, reads and edits `.docx` files. It can make tracked
  changes, comments, footnotes and tables, and it can convert Markdown to
  a Word document.
- [excel-mcp-server](https://github.com/haris-musa/excel-mcp-server) reads
  and writes `.xlsx`/`.xlsm` workbooks. It handles formulas (with a
  built-in calculator), charts, PivotTables and formatting.

They run inside the agent's container over stdio, so they see the same
files as the agent: the open project read-write and the rest of `~/Git`
read-only. The Excel server doesn't get `--allow-dir`: the container
already limits what it can reach, and the open project's path changes
for each connection. Writing VBA macros stays off (no `--allow-vba-write`).

## Limitations

- docx-mcp's `scrub_pii` tool doesn't work: it reports that `spacy` is
  missing. The image leaves out Presidio and spaCy, which only that
  experimental tool uses. They take hundreds of MB, and the tool would
  then try to download a 560 MB language model at runtime.
- docx-mcp's PDF export needs LibreOffice, which the image doesn't have.
- `save_document` leaves a `.bak` copy of the previous file next to it.
- For Claude, docx-mcp installs its companion skill into
  `~/.claude/skills/docx-mcp` (the Claude state volume) when it starts.
  Codex runs the server module directly, which skips that step.
- docx-mcp offers 219 tools and the Excel server 42, about 220,000
  characters of tool definitions. Claude Code and Codex (0.163) both load
  MCP tools on demand through tool search, so they don't all go into every
  request. Neither does OpenCode (2.2.6): with both servers, its first
  request was about 8,000 input tokens. To cut them down anyway, add `--tools` with a list of areas to
  the Excel server's arguments (see its README), or list the docx tools you
  don't want under `disabled_tools` in Codex's server entry.

## Files

`docx.txt` and `excel.txt` pin each server and all its dependencies by
version and SHA-256. The images install each one into its own venv under
`/opt/office-mcp` with `pip --require-hashes --no-deps`, using wheels only.
They need separate venvs because docx-mcp uses the mcp 1.x API and the
Excel server needs mcp 2.x. The image builds read this directory as the
`office-mcp` build context (`--build-context office-mcp=../office-mcp`).

`../update.sh` moves both servers to their newest release that's at least
3 days old, like the npm packages. It then runs `lock.py` in the current
Codex image, which resolves the dependencies for the image's Python 3.11
with only releases from before the same cutoff. `lock.py` resolves with the
pip pinned in `pip.txt`, since Debian's pip can't filter by upload date, and
it updates that pin too.
