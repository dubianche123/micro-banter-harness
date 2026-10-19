#!/bin/bash
# Generate by default. Installing and starting are distinct explicit actions.
set -eu
repo="$(cd "$(dirname "$0")/../.." && pwd)"
python="${REGULAR_PYTHON:-$repo/.venv/bin/python}"
cd "$repo"
label="io.github.the-regular"
output="$repo/data/$label.plist"
installed="$HOME/Library/LaunchAgents/$label.plist"
case "${1:-generate}" in
  generate)
    "$python" -m regular.service launchd --directory "$repo" --output "$output"
    ;;
  install)
    "$python" -m regular.service launchd --directory "$repo" --output "$output"
    mkdir -p "$HOME/Library/LaunchAgents"
    cp "$output" "$installed"
    echo "Installed $installed. Start explicitly: bash tools/launchd/install.sh start"
    ;;
  start)
    test -f "$installed" || { echo "Run install first" >&2; exit 1; }
    launchctl bootstrap "gui/$(id -u)" "$installed"
    ;;
  stop)
    launchctl bootout "gui/$(id -u)/$label"
    ;;
  status)
    launchctl print "gui/$(id -u)/$label"
    ;;
  *) echo "Usage: bash tools/launchd/install.sh [generate|install|start|stop|status]" >&2; exit 2 ;;
esac
