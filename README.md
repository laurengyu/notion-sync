# notion-sync

Pull Notion pages and databases to local markdown files via the Notion Markdown API (2026-03-11). Recursively discovers child pages (even inside column layouts, toggles, and synced blocks), queries databases through the data source API, downloads images to a local directory, and converts database properties to YAML frontmatter.

Built to replace Notion MCP for use with Claude Code and Claude Desktop. Sync once, read locally, zero token overhead.


## How it works

1. For each root page or database in your config, the script calls the Markdown API to get the page content as markdown.
2. It walks the block tree via the Blocks API to find child pages and databases at any nesting depth.
3. Databases are queried through the Data Source API (2025-09-03+), with each row saved as a separate markdown file with properties as YAML frontmatter.
4. Images are downloaded from Notion's pre-signed S3 URLs to a local `_images/` directory, and the markdown is rewritten to use local paths.
5. The companion shell script runs the sync, then commits and pushes each workspace to its own Git repo.


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
python3 notion_sync.py                      # sync all workspaces
python3 notion_sync.py --workspace personal # sync one workspace
python3 notion_sync.py --dry-run            # preview without writing files
./sync.sh                                   # sync + git commit + push
```

`sync.sh` cleans each workspace directory before syncing (preserving `.git` and `.gitignore`), so the Git diff reflects the true current state of Notion -- additions, modifications, and deletions.


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


## Limitations

- One-way sync only (Notion to local). No push-back to Notion.
- Full pull each run, no incremental sync.
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