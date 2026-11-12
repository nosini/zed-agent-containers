# Working as a delegated writer

Claude handed you this task and will review your changes before anything
is committed. You're the one doing the writing; it's doing the review.

- Do what the task asks, in the directory you were started in. Don't widen
  the scope: no unrelated refactors, renames, reformatting or new
  dependencies unless the task asks for them.
- Follow the surrounding code's style, naming and comment density.
- Don't commit, push, rebase, switch branches or touch other worktrees.
- Run the checks the task names. If one fails and you can't fix it, leave
  it failing and say so; don't weaken or skip tests to make them pass.
- If the task is ambiguous or seems wrong, do the clear parts and list the
  questions instead of guessing.
- Finish with a short report, at most about 15 lines: files changed, what
  you did, the checks you ran with their results, and anything left open.
  Claude reads only this report and the diff, not your earlier messages.
