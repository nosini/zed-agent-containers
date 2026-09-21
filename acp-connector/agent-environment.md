# Your environment

You run inside a rootless Podman container started for this project, not
directly on the user's machine.

- The project you were started in is read-write. The rest of the user's
  `~/Git` is mounted read-only at the same path. Nothing else of the
  user's home exists here.
- You are uid 1000 (`node`) without sudo; you cannot install system
  packages. Installed: Node 22 + npm, Python 3.11 (`venv`, `pip`), gcc/make
  (`build-essential`, `pkg-config`, `libssl-dev`), OpenJDK 17, git, curl,
  ripgrep, zip/unzip/xz.
- Your home, `/home/node`, is writable and persists between sessions. It's
  shared with the other agent. Install extra toolchains there when a
  project needs them; their bin directories are already on `PATH`:
  - Rust: `rustup` (`~/.cargo`)
  - Other Python versions and tools: `uv` (`~/.local/bin`)
  - Go: the official tarball extracted to `~/.local/go`
  - `npm install -g` installs into `~/.local`
- Android: `$ANDROID_HOME` is the user's SDK, read-only. If a build needs a
  missing SDK component, ask the user to install it on the host. Gradle
  uses your own `~/.gradle`.
- `/tmp` is in RAM; put large build output in the project or your home.
- You cannot run containers (no Podman/Docker).
- Git commits are signed through the user's SSH agent, and the user
  approves each signature. Don't push unless asked.
