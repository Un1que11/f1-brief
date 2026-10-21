#!/usr/bin/env bash
# One-command setup for f1-brief on macOS.
#
#   ./setup.sh             install or update everything
#   ./setup.sh status      show whether the hourly job and the bot are running
#   ./setup.sh uninstall   stop and remove both launchd agents (state.db and .env are kept)
#
# Safe to run again at any time (e.g. after `git pull`): it re-syncs and reloads.
# What it does: checks uv and Claude Code, installs the dependencies, sets up the Telegram bot
# in .env (finds your chat id for you), sends a test message, seeds the first stories, and
# installs two launchd agents: local.f1brief.run (hourly: digest, qualifying message, alerts)
# and local.f1brief.bot (the always-on bot for /stories and the Follow/Unfollow buttons).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOMAIN="gui/$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/f1brief"
JOBS=(run bot)

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
ask_yes() { # ask_yes "question" -> 0 for yes (default yes)
    local reply
    read -r -p "  $1 [Y/n] " reply || true
    [[ -z "$reply" || "$reply" =~ ^[Yy] ]]
}
label() { echo "local.f1brief.$1"; }
plist() { echo "$AGENTS/$(label "$1").plist"; }
env_get() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- || true; }
env_set() { # env_set KEY VALUE
    if grep -qE "^$1=" .env; then
        local tmp
        tmp="$(mktemp)"
        awk -v k="$1" -v v="$2" 'BEGIN{FS=OFS="="} $1==k{print k"="v; next} {print}' .env >"$tmp" && mv "$tmp" .env
    else
        printf '%s=%s\n' "$1" "$2" >>.env
    fi
    chmod 600 .env
}
agent_state() {
    if launchctl print "$DOMAIN/$(label "$1")" >/dev/null 2>&1; then
        if launchctl print "$DOMAIN/$(label "$1")" | grep -q "state = running"; then echo "running"; else echo "loaded (waiting for its next start)"; fi
    else
        echo "not installed"
    fi
}
stop_agents() {
    for job in "${JOBS[@]}"; do launchctl bootout "$DOMAIN/$(label "$job")" 2>/dev/null || true; done
}

[[ "$(uname -s)" == "Darwin" ]] || die "this script is for macOS (launchd)"
cd "$REPO"

case "${1:-}" in
    status)
        bold "f1-brief"
        echo "  hourly job ($(label run)): $(agent_state run)"
        echo "  bot ($(label bot)): $(agent_state bot)"
        echo "  logs: $LOGS/{run,bot}.log and .err.log"
        exit 0
        ;;
    uninstall)
        stop_agents
        for job in "${JOBS[@]}"; do rm -f "$(plist "$job")"; done
        ok "removed both agents; state.db, config.toml and .env are untouched"
        exit 0
        ;;
    "") ;;
    *) echo "usage: $0 [status|uninstall]" >&2; exit 2 ;;
esac

bold "1/7 Tools"
for dir in "$HOME/.local/bin" "$HOME/.cargo/bin" /opt/homebrew/bin /usr/local/bin "$HOME/.claude/local"; do
    [[ -d "$dir" && ":$PATH:" != *":$dir:"* ]] && PATH="$dir:$PATH"
done
export PATH
if ! command -v uv >/dev/null 2>&1; then
    if ask_yes "uv is not installed. Install it now?"; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    else
        die "uv is needed: https://docs.astral.sh/uv/"
    fi
fi
ok "$(uv --version)"
if command -v claude >/dev/null 2>&1; then
    ok "claude $(claude --version 2>/dev/null | head -1)"
else
    die "Claude Code (\`claude\`) is not on PATH; it writes the digest. Install it (https://docs.claude.com/en/docs/claude-code), run \`claude\` once to log in, then re-run ./setup.sh"
fi

bold "2/7 Dependencies"
uv sync --quiet
ok "uv sync"

# The bot listener and the chat-id lookup both read the bot's updates; only one may poll.
stop_agents

bold "3/7 Telegram bot (.env)"
if [[ ! -f .env ]]; then
    cp .env.example .env
    chmod 600 .env
    ok "created .env"
fi
token="$(env_get TELEGRAM_BOT_TOKEN)"
bot_name=""
while true; do
    if [[ -z "$token" ]]; then
        echo "  Create a bot: open @BotFather in Telegram → /newbot → copy the token."
        read -r -s -p "  Bot token: " token || true
        echo
        [[ -n "$token" ]] || die "a bot token is needed"
    fi
    me="$(curl -fsS "https://api.telegram.org/bot$token/getMe" 2>/dev/null || true)"
    bot_name="$(printf '%s' "$me" | uv run --quiet python -c 'import json,sys; d=json.load(sys.stdin); print(d["result"]["username"] if d.get("ok") else "")' 2>/dev/null || true)"
    if [[ -n "$bot_name" ]]; then
        env_set TELEGRAM_BOT_TOKEN "$token"
        ok "bot @$bot_name"
        break
    fi
    warn "Telegram does not accept this token"
    token=""
done

chat_id="$(env_get TELEGRAM_CHAT_ID)"
if [[ -z "$chat_id" ]]; then
    # A webhook would hide the updates from getUpdates (and from the bot listener).
    curl -fsS "https://api.telegram.org/bot$token/deleteWebhook" >/dev/null 2>&1 || true
    echo "  Now open https://t.me/$bot_name in Telegram and send it any message (e.g. /start)."
    echo "  Waiting up to 3 minutes…"
    for _ in $(seq 1 36); do
        chat_id="$(curl -fsS "https://api.telegram.org/bot$token/getUpdates?timeout=5" 2>/dev/null \
            | uv run --quiet python -c 'import json,sys
d=json.load(sys.stdin)
ids=[(u.get("message") or u.get("edited_message") or {}).get("chat",{}).get("id") for u in d.get("result",[])]
ids=[i for i in ids if i is not None]
print(ids[-1] if ids else "")' 2>/dev/null || true)"
        [[ -n "$chat_id" ]] && break
    done
    [[ -n "$chat_id" ]] || die "no message reached the bot; send it one and re-run ./setup.sh"
    env_set TELEGRAM_CHAT_ID "$chat_id"
    ok "chat id $chat_id saved"
else
    ok "chat id is set"
fi
if curl -fsS "https://api.telegram.org/bot$token/sendMessage" \
    --data-urlencode "chat_id=$chat_id" \
    --data-urlencode "text=🏁 f1-brief подключён. Дайджест придёт примерно за 2 дня до FP1." >/dev/null; then
    ok "test message sent — check Telegram"
else
    warn "could not send the test message; check the chat id in .env"
fi

bold "4/7 Check"
uv run f1brief config | sed 's/^/    /'
if uv run f1brief next | sed 's/^/    /'; then ok "calendar reachable"; else warn "could not fetch the calendar (network?)"; fi

bold "5/7 First stories"
if uv run f1brief seed-stories | sed 's/^/    /'; then
    ok "seed stories done (already-seeded articles are skipped)"
else
    warn "seeding failed (is \`claude\` logged in? run \`claude\` once); re-run ./setup.sh later"
fi

bold "6/7 launchd agents"
mkdir -p "$AGENTS" "$LOGS"
for job in "${JOBS[@]}"; do
    uv run f1brief launchd --job "$job" --workdir "$REPO" --log-dir "$LOGS" --output "$(plist "$job")" >/dev/null
    plutil -lint -s "$(plist "$job")"
    launchctl bootstrap "$DOMAIN" "$(plist "$job")"
done
ok "$(label run): every hour (digest, qualifying message, story alerts)"
ok "$(label bot): always on, restarts if it stops"

bold "7/7 Status"
sleep 2
echo "  hourly job: $(agent_state run)"
echo "  bot: $(agent_state bot)"

cat <<EOF

Done. In Telegram, send /stories to @$bot_name.
  • ./setup.sh status     check both agents
  • tail -f $LOGS/run.log $LOGS/bot.err.log
  • ./setup.sh            again after git pull, or after reinstalling uv or claude
EOF
