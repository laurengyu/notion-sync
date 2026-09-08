#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "=== Notion Sync: $TIMESTAMP ==="

# Clean workspace directories before sync (keep .git and .gitignore)
for ws_dir in "$SCRIPT_DIR"/personal "$SCRIPT_DIR"/work; do
    if [ -d "$ws_dir" ]; then
        find "$ws_dir" -mindepth 1 -not -path "$ws_dir/.git/*" -not -name ".git" -not -name ".gitignore" -delete 2>/dev/null || true
    fi
done

# Run the Python sync script
python3 "$SCRIPT_DIR/notion_sync.py" --config "$SCRIPT_DIR/config.json"

# Git commit and push each workspace
for ws_dir in "$SCRIPT_DIR"/personal "$SCRIPT_DIR"/work; do
    if [ ! -d "$ws_dir/.git" ]; then
        echo "Skipping $ws_dir (no git repo)"
        continue
    fi

    ws_name=$(basename "$ws_dir")
    cd "$ws_dir"

    if [ -n "$(git status --porcelain)" ]; then
        git add -A
        git commit -m "sync: $TIMESTAMP"
        git push
        echo "[$ws_name] changes committed and pushed."
    else
        echo "[$ws_name] no changes."
    fi
done

echo "=== Done ==="
