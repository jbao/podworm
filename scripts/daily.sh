#!/usr/bin/env bash
# podworm daily pipeline — intended to be run by launchd
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Source environment (API keys, etc.)
[[ -f "$HOME/.zshenv" ]] && source "$HOME/.zshenv"
[[ -f "$REPO_DIR/.env" ]] && source "$REPO_DIR/.env"

# Ensure PATH includes tools installed via cargo/homebrew/npm
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
LOG_DIR="$HOME/.local/share/podworm/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/daily-$(date +%Y-%m-%d).log"

cd "$REPO_DIR"
echo "=== podworm daily — $(date) ===" > "$LOG_FILE"
uv run podworm daily --obsidian >> "$LOG_FILE" 2>&1
echo "=== done — $(date) ===" >> "$LOG_FILE"
