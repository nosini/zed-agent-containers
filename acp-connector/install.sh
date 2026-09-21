#!/bin/sh
# Install the ACP connectors (Codex and Claude Code). Run from a HOST terminal
# (not in Zed). Copies (never symlinks) host-side code out of ~/Git.
# Re-run after changing any of these files. Never overwrites your edits to
# ~/.config/acp-connector/{gitconfig,broker.env,agent-environment.md}.
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
for agent in codex claude; do
    install -m 644 "$src/$agent-acp-connector.socket" "$src/$agent-acp-connector.service" "$units/"
    install -m 644 "$src/../$agent-container/$agent-agent-state.volume" "$quadlets/"
done
install -m 644 "$src/agent-home.volume" "$quadlets/"

# Environment notes the agents read (AGENTS.md / CLAUDE.md); yours to edit.
if [ ! -e "$settings/agent-environment.md" ]; then
    install -m 644 "$src/agent-environment.md" "$settings/"
    echo "Created $settings/agent-environment.md"
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
if [ -d "$HOME/Android/Sdk" ]; then add_opt --android-sdk "$HOME/Android/Sdk"; fi
echo "Broker options: $(cat "$settings/broker.env")"

systemctl --user daemon-reload
for agent in codex claude; do
    systemctl --user try-restart "$agent-acp-connector.service"
done
echo "Installed. If not done yet:"
echo "  systemctl --user enable --now codex-acp-connector.socket claude-acp-connector.socket"
echo "Rebuild both images after Containerfile changes (see the READMEs)."
