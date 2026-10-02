#!/bin/sh
# Install the ACP connectors (Codex, Claude Code and ZCode). Run from a HOST terminal
# (not in Zed). Copies (never symlinks) host-side code out of ~/Git.
# Re-run after changing any of these files. Never overwrites your edits to
# ~/.config/acp-connector/{gitconfig,broker.env,agent-environment.md}; the
# last is updated from the repo only while you haven't edited it.
set -eu

if [ -e /.flatpak-info ]; then
    echo "Run this in a host terminal, not inside a Flatpak." >&2
    exit 1
fi

src=$(cd "$(dirname "$0")" && pwd)
config=${XDG_CONFIG_HOME:-$HOME/.config}
libexec=$HOME/.local/libexec/acp-connector
units=$config/systemd/user
quadlets=$config/containers/systemd
settings=$config/acp-connector
client=$HOME/.var/app/dev.zed.Zed/data/acp-connector

# Earlier versions ran one long-lived container per agent (Quadlet .container).
for agent in codex claude; do
    if [ -e "$quadlets/$agent-agent.container" ]; then
        systemctl --user stop "$agent-agent.service" 2>/dev/null || true
        rm -f "$quadlets/$agent-agent.container"
    fi
done
rm -f "$HOME/.local/libexec/codex-acp-connector/codex-acp-broker.py" \
      "$HOME/.var/app/dev.zed.Zed/data/codex-acp-connector/codex-acp-client.py"
rmdir "$HOME/.local/libexec/codex-acp-connector" \
      "$HOME/.var/app/dev.zed.Zed/data/codex-acp-connector" 2>/dev/null || true

install -d -m 700 "$libexec" "$client" "$settings"
install -m 555 "$src/acp-broker.py" "$libexec/"
install -m 755 "$src/acp-client.py" "$client/"
install -d -m 755 "$units" "$quadlets"
for agent in codex claude zcode; do
    install -m 644 "$src/$agent-acp-connector.socket" "$src/$agent-acp-connector.service" "$units/"
    install -m 644 "$src/../$agent-container/$agent-agent-state.volume" "$quadlets/"
done
install -m 644 "$src/agent-home.volume" "$quadlets/"

# Environment notes the agents read (AGENTS.md / CLAUDE.md); yours to edit.
# Replaced by the repo version only while you haven't edited it, that is
# while it is a version from this repository's git history.
notes=$settings/agent-environment.md
if [ ! -e "$notes" ]; then
    install -m 644 "$src/agent-environment.md" "$settings/"
    echo "Created $notes"
elif ! cmp -s "$src/agent-environment.md" "$notes"; then
    blob=$(git hash-object "$notes" 2>/dev/null || true)
    if [ -n "$blob" ] && git -C "$src" log --format= --raw --no-abbrev -- agent-environment.md 2>/dev/null |
            grep -q " $blob "; then
        install -m 644 "$src/agent-environment.md" "$settings/"
        echo "Updated $notes"
    else
        echo "Not updating $notes: it has your edits. Compare: diff $notes $src/agent-environment.md"
    fi
fi

# Git identity and signing for commits made by the agents, from your host
# config. The signing key is embedded as key:: so no ~/.ssh path is mounted.
if [ ! -e "$settings/gitconfig" ]; then
    {
        echo "# Git config inside the agent containers (mounted read-only)."
        printf '[user]\n\tname = %s\n\temail = %s\n' \
            "$(git config --global user.name || echo agent)" "$(git config --global user.email || echo agent@localhost)"
        key=$(git config --global user.signingkey || true)
        case $key in "~/"*) key=$HOME/${key#"~/"} ;; esac
        if [ "$(git config --global gpg.format || true)" = ssh ] && [ -n "$key" ]; then
            if [ -f "$key" ]; then key="key::$(cat "$key")"; fi
            printf '\tsigningkey = %s\n[gpg]\n\tformat = ssh\n' "$key"
            printf '[commit]\n\tgpgsign = %s\n[tag]\n\tgpgsign = %s\n' \
                "$(git config --global commit.gpgsign || echo false)" "$(git config --global tag.gpgsign || echo false)"
        fi
    } > "$settings/gitconfig"
    chmod 644 "$settings/gitconfig"
    echo "Created $settings/gitconfig"
fi

# Pushes: your host's url.<ssh>.pushInsteadOf rules (fetch over https, push
# over SSH), added to the agents' gitconfig if missing.
git config --global --get-regexp '^url\..*\.pushinsteadof$' 2>/dev/null | while read -r key value; do
    base=${key#url.}; base=${base%.pushinsteadof}
    if ! git config --file "$settings/gitconfig" --get-all "url.$base.pushInsteadOf" 2>/dev/null |
            grep -qxF -- "$value"; then
        git config --file "$settings/gitconfig" --add "url.$base.pushInsteadOf" "$value"
        echo "Added push rule to gitconfig: $value -> $base"
    fi
done

# Host keys for the SSH hosts those rules push to, taken from your
# ~/.ssh/known_hosts. Regenerated on every install and mounted read-only, so
# the agents can't add or change them.
push_hosts() {
    git config --file "$settings/gitconfig" --get-regexp '^url\..*\.pushinsteadof$' 2>/dev/null |
    while read -r key value; do
        base=${key#url.}; base=${base%.pushinsteadof}
        case $base in
            ssh://*)
                rest=${base#ssh://}; rest=${rest#*@}; rest=${rest%%/*}
                case $rest in *:*) echo "[${rest%%:*}]:${rest#*:}" ;; *) echo "$rest" ;; esac ;;
            *://*) ;;  # not SSH
            *:*) rest=${base#*@}; echo "${rest%%:*}" ;;  # git@host:path
        esac
    done | sort -u
}
{
    echo "# Generated by acp-connector/install.sh from ~/.ssh/known_hosts; mounted read-only."
    for host in $(push_hosts); do
        keys=$(ssh-keygen -F "$host" 2>/dev/null | grep -v '^#' || true)
        if [ -n "$keys" ]; then
            echo "$keys"
        else
            echo "Warning: no host key for $host in ~/.ssh/known_hosts; connect to it once over SSH" \
                 "from the host (e.g. a git fetch or push), then rerun install.sh" >&2
        fi
    done
} > "$settings/known_hosts.new"
chmod 644 "$settings/known_hosts.new"
mv "$settings/known_hosts.new" "$settings/known_hosts"
echo "Push host keys: $(grep -vc '^#' "$settings/known_hosts" || true) (in $settings/known_hosts)"

# Broker options: the gitconfig above, and an SSH agent for commit signing.
if [ ! -e "$settings/broker.env" ]; then
    opts="--git-config $settings/gitconfig"
    # Prefer a socket an agent is actually listening on (ssh-add -l exits 2
    # when it cannot connect); fall back to the first existing socket file.
    live="" first=""
    for s in "$HOME/.bitwarden-ssh-agent.sock" \
             "$HOME/.var/app/com.bitwarden.desktop/data/.bitwarden-ssh-agent.sock" \
             "${SSH_AUTH_SOCK:-}"; do
        [ -n "$s" ] && [ -S "$s" ] || continue
        [ -n "$first" ] || first=$s
        rc=0; SSH_AUTH_SOCK=$s ssh-add -l >/dev/null 2>&1 || rc=$?
        if [ "$rc" -ne 2 ]; then live=$s; break; fi
    done
    if [ -n "${live:-$first}" ]; then opts="$opts --ssh-agent-socket ${live:-$first}"; fi
    [ -n "$live" ] || [ -z "$first" ] || echo "Warning: no SSH agent answering at $first (Bitwarden not running?)"
    printf 'ACP_BROKER_OPTS="%s"\n' "$opts" > "$settings/broker.env"
    echo "Created $settings/broker.env"
fi
# Options added in later versions; appended only if missing.
add_opt() {
    if ! grep -q -- "$1 " "$settings/broker.env"; then
        sed -i "s|\"\$| $1 $2\"|" "$settings/broker.env"
        echo "Added $1 $2 to broker.env"
    fi
}
add_opt --instructions "$settings/agent-environment.md"
add_opt --known-hosts "$settings/known_hosts"
# Memory shared by all agents. Only notes/ is mounted (read-write, at
# ~/memory); the git repository around it stays on the host, so the agents
# can't change its hooks or config. Skipped once broker.env has --memory, so
# you can point that elsewhere.
if ! grep -q -- "--memory " "$settings/broker.env"; then
    memory=${XDG_DATA_HOME:-$HOME/.local/share}/agent-memory
    if [ ! -e "$memory" ]; then
        install -d -m 700 "$memory" "$memory/notes"
        git init -q "$memory"
        printf '.obsidian/\n' > "$memory/.gitignore"
        printf '# Shared memory\n\n' > "$memory/notes/INDEX.md"
        echo "Created $memory (git repository; the agents see only notes/)"
    fi
    add_opt --memory "$memory/notes"
fi
if [ -d "$HOME/Android/Sdk" ]; then add_opt --android-sdk "$HOME/Android/Sdk"; fi
echo "Broker options: $(cat "$settings/broker.env")"

systemctl --user daemon-reload
for agent in codex claude zcode; do
    systemctl --user try-restart "$agent-acp-connector.service"
done
echo "Installed. If not done yet:"
echo "  systemctl --user enable --now codex-acp-connector.socket claude-acp-connector.socket zcode-acp-connector.socket"
echo "Rebuild the images after Containerfile changes (see the READMEs)."
