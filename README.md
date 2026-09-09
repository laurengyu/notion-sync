# notion-sync

Pull Notion pages and databases to local markdown files via the Notion Markdown API (2026-03-11). Recursively discovers child pages (even inside column layouts, toggles, and synced blocks), queries databases through the data source API, downloads images to a local directory, and converts database properties to YAML frontmatter.

Built to replace Notion MCP for use with Claude Code and Claude Desktop. Sync once, read locally, zero token overhead.


## How it works

The sync runs in three phases:

**Prefetch.** On incremental syncs, the script calls the Search API to bulk-fetch `last_edited_time` for all pages (~16 requests for a 1600-page workspace). This builds an in-memory lookup so most pages can be checked for changes without individual API calls. Skipped on `--full`.

**Pass 1 — Discover tree structure.** For each root page or database, the script walks the block tree via the Blocks API to discover child pages and databases at any nesting depth. Sibling pages are explored concurrently (3 workers). For each discovered page, the script checks its `last_edited_time` against the manifest -- first from the prefetch cache, then from parent hints, falling back to a `get_page` call only when neither is available (e.g. newly created pages not yet indexed by Search). Unchanged pages reuse their cached child list from the manifest. Changed pages are queued for content fetching. Databases are fully queried every run so new and deleted rows are always detected.

**Pass 2 — Fetch content concurrently.** All queued pages are processed in parallel (3 workers) -- fetching markdown, downloading images, and writing files. An adaptive token-bucket rate limiter (default 4 req/s) is shared across all threads: it backs off on 429s and recovers after successful requests.

**Cleanup.** Pages present in the manifest but not encountered during the tree walk are treated as deleted in Notion -- their local files are removed. A page that moved or was renamed keeps its ID but resolves to a new path; the script moves the file to its new location (or re-fetches it if the old file is missing) and prunes any directory left empty.

The companion shell script runs the sync, then commits and pushes each workspace to its own Git repo.


## Setup

Requirements: Python 3.10+, `requests`, Git.

```
pip install requests
```

### 1. Create Personal Access Tokens

Go to https://www.notion.so/developers, create a PAT for each workspace with Notion API capability. PATs inherit your user permissions -- no need to connect an integration to individual pages.

### 2. Configure

Copy `config.example.json` to `config.json` and fill in your tokens and root page/database URLs:

```json
{
  "workspaces": [
    {
      "name": "personal",
      "token": "ntn_...",
      "output_dir": "./personal",
      "roots": [
        "https://www.notion.so/Projects-abc123...",
        "https://www.notion.so/Notes-def456..."
      ]
    },
    {
      "name": "work",
      "token": "ntn_...",
      "output_dir": "./work",
      "roots": [
        "https://www.notion.so/Work-ghi789..."
      ]
    }
  ]
}
```

Roots can be page URLs, database URLs, or raw page/database IDs. The script auto-detects whether each root is a page or a database. Child pages and inline databases are discovered recursively -- you only need to list top-level entry points.

If you omit `roots` (or leave it as an empty list), the script auto-discovers all workspace-level pages via the Search API.

### 3. First sync

```
python3 notion_sync.py
```

### 4. Git setup (optional)

Each workspace output directory can be its own Git repo for backup and version history:

```
cd personal
git init
echo ".DS_Store" > .gitignore
git add . && git commit -m "Initial sync"
git remote add origin git@github.com:you/notion-backup-personal.git
git push -u origin main
```


## Usage

```
python3 notion_sync.py                      # incremental sync all workspaces
python3 notion_sync.py --wait 45            # wait 45s for Search API to index recent edits
python3 notion_sync.py --full               # force full sync (ignore manifest)
python3 notion_sync.py --workspace personal # sync one workspace
python3 notion_sync.py --dry-run            # preview without writing files
./sync.sh                                   # sync + git commit + push
```

Incremental sync is the default. The script pre-fetches `last_edited_time` for all pages via the Search API, then only re-pulls pages that have changed. Use `--wait 45` when syncing right after making changes in Notion -- the Search API has a few seconds of indexing delay, and the wait ensures recent edits are picked up. Use `--full` to re-pull everything, e.g. after changing the script's output format.


## Output structure

```
personal/
  _images/
    a1b2c3d4e5f6g7h8.png
  Projects/
    _index.md                 # page content (has children)
    Project Alpha.md          # leaf page
    Project Beta.md
  Reading List/               # database
    Book Title.md             # database row with frontmatter
```

- Pages with children become directories; the page's own content goes in `_index.md`.
- Leaf pages are single `.md` files.
- Database rows are `.md` files with properties as YAML frontmatter.
- Images are content-addressed (SHA256 of the URL path) so duplicates are not re-downloaded.


## Performance

The script uses Search API prefetching, a two-pass architecture with concurrent fetching (3 workers), and an adaptive rate limiter (4 req/s, shared across threads). On incremental syncs, the Search API bulk-fetches edit times for all pages in ~16 requests, so most pages can be skipped without individual API calls. The manifest is checkpointed every 20 seconds and saved on interrupt (Ctrl-C / SIGTERM), so a partial run's progress is preserved.

Rough estimates for a ~1600-page workspace:
- Initial full sync: ~4500 API requests, ~20-25 minutes
- Incremental sync (nothing changed): ~140 requests (Search prefetch + database queries), ~1 minute
- Incremental sync (a few pages changed): ~150 requests, ~1 minute


## Limitations

- One-way sync only (Notion to local). No push-back to Notion (use the Markdown API directly via Claude Code for writes).
- The Search API has a few seconds of indexing delay. If you sync immediately after editing in Notion, use `--wait 45` to ensure the change is picked up. Without `--wait`, changes made in the last few seconds may be missed until the next sync.
- Tree walking (Blocks API calls to discover children) is incremental: it runs only for changed pages/rows and for anything new. Content pulls (Markdown API) are incremental too. Moves and renames are handled (the file is moved to its new path), but if Notion does not bump a page's `last_edited_time` when a child is moved *out* of it, that page keeps a stale cached child list until the next `--full` sync -- so run `--full` occasionally after a big reorganization.
- Linked databases are skipped (data comes through the original database).
- Bookmark, embed, and link preview blocks appear as `<unknown>` tags in the markdown.
- Image URLs from Notion are temporary; the script downloads them, but if a sync fails partway through, some image links in the markdown may point to expired URLs until the next successful sync.
- Pages over ~20,000 blocks may be truncated; the script attempts to fetch missing blocks automatically.


## Files

| File | Purpose |
|---|---|
| `notion_sync.py` | Main sync script |
| `config.json` | Your workspace config (gitignored) |
| `config.example.json` | Template for config.json |
| `sync.sh` | One-command sync + git commit + push |


## Security

`config.json` contains your PATs. Keep it out of version control:

```
echo "config.json" >> .gitignore
```
