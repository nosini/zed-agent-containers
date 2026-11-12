---
name: delegate-writing
description: Hand code or document writing to a cheaper model (DeepSeek on OpenCode Go) with opencode-delegate, then review the result. Use when an implementation task is big enough that writing a spec and reviewing the diff costs less than writing it yourself. Skip it for small fixes.
---

# Delegating writing to OpenCode Go

You orchestrate and review; `opencode-delegate` hands the writing to a
cheaper model (default `opencode-go/deepseek-v4-pro`) running in this
container. The user pays for your tokens at a much higher rate than the
worker's, so the point is to spend fewer of yours.

## Decide first: is it cheaper than doing it yourself?

Delegate only when writing the spec, reading the report and reviewing the
diff would cost you clearly fewer tokens than making the change yourself.
Estimate roughly, don't deliberate:

- **Do it yourself** when you already know the edit and it's small (a few
  lines to a few dozen, one or two files), or when explaining it would take
  about as many words as writing it. Typos, one-line fixes, renames, config
  tweaks, small follow-ups to a review: just do them.
- **Do it yourself** when the work is mostly figuring out *what* to change
  (debugging, design, security-sensitive code) and the writing is the easy
  part, or when a wrong result would cost more to review than to write.
- **Delegate** when the output is large compared with its description:
  new modules or features, many similar edits, tests to fill out, ports,
  boilerplate, long documentation, from a spec you can state briefly.

If you're unsure and the change is under roughly a hundred lines, do it
yourself. The user can always ask for delegation explicitly, or tell you
not to.

## Workflow

1. **Read only what you need** to write the spec. Don't read files on the
   worker's behalf; it can read them itself.
2. **Make a worktree** so the worker's changes stay separate from the
   user's and show up as a clean diff:
   `git worktree add -b delegate/<name> ~/<repo>/worktrees/<name>`.
   For non-repository work, use a copy under `~/<repo>/tmp`.
3. **Write the spec** to `~/<repo>/tmp/opencode-delegate/<name>.md`. Keep
   it short and concrete: goal, files or areas involved, constraints
   (style, APIs to use or avoid, what not to touch), acceptance criteria,
   and the exact checks to run. Point to files and examples instead of
   pasting code. The worker sees the repository's `AGENTS.md`/project
   instructions and the container notes, not this conversation.
4. **Run it** from the worktree, in the background for anything sizeable:
   `cd <worktree> && opencode-delegate ~/<repo>/tmp/opencode-delegate/<name>.md`.
   It prints the session ID, the worker's short report, cost and the git
   summary; the full transcript is in the log it names. Read the log only
   when something's unclear.
5. **Review** the diff (`git -C <worktree> diff`) like a pull request from
   a junior contributor: correctness, scope creep, style, tests. Run the
   checks yourself; don't trust the report's test results.
6. **Iterate cheaply.** Real problems spread over several places: write a
   short follow-up and send it to the same session with
   `opencode-delegate -s <session> <followup.md>`. A few small fixes: make
   them yourself, as that's cheaper than another round. After two rounds
   that don't converge, finish it yourself.
7. **Bring it back**: merge or cherry-pick the result into the user's
   branch (don't commit unless the user asked for commits), then remove the
   worktree and its branch. Tell the user what was delegated and what you
   changed in review.

## Models and login

- `opencode-delegate models` lists the OpenCode Go models. Use
  `-m opencode-go/deepseek-v4.1-flash` for simple, mechanical work. Only
  `opencode-go/` models are allowed.
- If the run fails with an authentication or usage-limit error, tell the
  user. Logging in happens from a host terminal, as described in the
  claude-container README; don't try to log in yourself.
