#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "=== Notion Sync: $TIMESTAMP ==="

# Run the Python sync script (incremental by default)
python3 "$SCRIPT_DIR/notion_sync.py" --wait 45 --config "$SCRIPT_DIR/config.json"

# Git commit and push each workspace.
# Output dirs are read from config.json so this never drifts from the real config.
OUTPUT_DIRS=$(python3 -c "import json, sys; c = json.load(open(sys.argv[1])); print('\n'.join(w.get('output_dir', './' + w['name']) for w in c.get('workspaces', [])))" "$SCRIPT_DIR/config.json")

echo "$OUTPUT_DIRS" | while IFS= read -r rel_dir; do
    [ -z "$rel_dir" ] && continue
    ws_dir="$(cd "$SCRIPT_DIR" && cd "$rel_dir" 2>/dev/null && pwd || true)"

    if [ -z "$ws_dir" ] || [ ! -d "$ws_dir/.git" ]; then
        echo "Skipping $rel_dir (no git repo)"
        continue
    fi

    ws_name=$(basename "$ws_dir")

    if [ -n "$(git -C "$ws_dir" status --porcelain)" ]; then
        git -C "$ws_dir" add -A
        git -C "$ws_dir" commit -m "sync: $TIMESTAMP"
        git -C "$ws_dir" push
        echo "[$ws_name] changes committed and pushed."
    else
        echo "[$ws_name] no changes."
    fi
done

echo "=== Done ==="
