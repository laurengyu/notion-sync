#!/usr/bin/env python3
"""
notion_sync.py — Pull Notion pages and databases to local markdown files.

Uses the Notion Markdown API (2026-03-11) for page content,
the Blocks API for child discovery, and the Databases API for row queries.
Downloads images to a local _images/ directory and rewrites URLs.
Incremental by default: only re-pulls pages whose last_edited_time has changed.

Usage:
    python3 notion_sync.py                      # incremental sync all workspaces
    python3 notion_sync.py --full               # force full sync (ignore manifest)
    python3 notion_sync.py --config my.json     # use a custom config file
    python3 notion_sync.py --workspace personal # sync only one workspace
    python3 notion_sync.py --dry-run            # show what would be synced without writing
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = "config.json"
API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
RATE_LIMIT_DELAY = 0.35  # seconds between requests (~3 req/s)
MANIFEST_FILE = ".manifest.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """Turn a page title into a safe filename."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.strip(". ")
    if not name:
        name = "Untitled"
    # Truncate to avoid filesystem limits
    if len(name) > 200:
        name = name[:200]
    return name


def extract_page_id(url_or_id: str) -> str:
    """Extract a 32-char hex page ID from a Notion URL or raw ID."""
    # Already a clean UUID with dashes
    if re.match(r"^[0-9a-f]{8}-", url_or_id):
        return url_or_id
    # 32-char hex without dashes (from URL)
    match = re.search(r"([0-9a-f]{32})(?:\?|$)", url_or_id)
    if match:
        h = match.group(1)
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    # URL with dashed UUID
    match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", url_or_id)
    if match:
        return match.group(1)
    return url_or_id


def format_property_value(prop: dict):
    """Convert a Notion property object to a simple Python value for YAML frontmatter."""
    t = prop.get("type", "")
    if t == "title":
        return "".join(rt.get("plain_text", "") for rt in prop.get("title", []))
    elif t == "rich_text":
        return "".join(rt.get("plain_text", "") for rt in prop.get("rich_text", []))
    elif t == "number":
        return prop.get("number")
    elif t == "select":
        sel = prop.get("select")
        return sel["name"] if sel else None
    elif t == "multi_select":
        return [s["name"] for s in prop.get("multi_select", [])]
    elif t == "status":
        st = prop.get("status")
        return st["name"] if st else None
    elif t == "date":
        d = prop.get("date")
        if d:
            start = d.get("start", "")
            end = d.get("end")
            return f"{start} → {end}" if end else start
        return None
    elif t == "checkbox":
        return prop.get("checkbox", False)
    elif t == "url":
        return prop.get("url")
    elif t == "email":
        return prop.get("email")
    elif t == "phone_number":
        return prop.get("phone_number")
    elif t == "created_time":
        return prop.get("created_time")
    elif t == "last_edited_time":
        return prop.get("last_edited_time")
    elif t == "created_by":
        return prop.get("created_by", {}).get("name")
    elif t == "last_edited_by":
        return prop.get("last_edited_by", {}).get("name")
    elif t == "formula":
        f = prop.get("formula", {})
        return f.get(f.get("type", ""), None)
    elif t == "rollup":
        r = prop.get("rollup", {})
        return r.get(r.get("type", ""), None)
    elif t == "relation":
        return [rel["id"] for rel in prop.get("relation", [])]
    elif t == "people":
        return [p.get("name", p.get("id", "")) for p in prop.get("people", [])]
    elif t == "files":
        return [f.get("name", "") for f in prop.get("files", [])]
    elif t == "unique_id":
        uid = prop.get("unique_id", {})
        prefix = uid.get("prefix", "")
        number = uid.get("number", "")
        return f"{prefix}-{number}" if prefix else str(number)
    else:
        return None


def properties_to_frontmatter(properties: dict, exclude_title: bool = True) -> str:
    """Convert Notion page properties to YAML frontmatter string."""
    lines = ["---"]
    for key, prop in sorted(properties.items()):
        if exclude_title and prop.get("type") == "title":
            continue
        val = format_property_value(prop)
        if val is None or val == "" or val == []:
            continue
        if isinstance(val, list):
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"  - {item}")
        elif isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        elif isinstance(val, (int, float)):
            lines.append(f"{key}: {val}")
        elif "\n" in str(val):
            lines.append(f'{key}: "{val}"')
        else:
            # Escape YAML special chars
            sv = str(val)
            if any(c in sv for c in ":#{}[]&*!|>',\"@`"):
                lines.append(f'{key}: "{sv}"')
            else:
                lines.append(f"{key}: {sv}")
    lines.append("---")
    if len(lines) <= 2:
        return ""
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Manifest — tracks last_edited_time and file paths for incremental sync
# ---------------------------------------------------------------------------

class Manifest:
    """Tracks page_id → {last_edited_time, path} for incremental sync."""

    def __init__(self, output_dir: Path):
        self.file = output_dir / MANIFEST_FILE
        self.data: dict[str, dict] = {}
        self.load()

    def load(self):
        if self.file.exists():
            try:
                with open(self.file) as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def save(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file, "w") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def get_edited_time(self, page_id: str) -> str | None:
        entry = self.data.get(page_id)
        return entry["last_edited_time"] if entry else None

    def get_path(self, page_id: str) -> str | None:
        entry = self.data.get(page_id)
        return entry["path"] if entry else None

    def set(self, page_id: str, last_edited_time: str, path: str):
        self.data[page_id] = {
            "last_edited_time": last_edited_time,
            "path": path,
        }

    def all_ids(self) -> set[str]:
        return set(self.data.keys())

    def remove(self, page_id: str):
        self.data.pop(page_id, None)


# ---------------------------------------------------------------------------
# NotionSync
# ---------------------------------------------------------------------------

class NotionSync:
    def __init__(self, token: str, output_dir: str, dry_run: bool = False,
                 full: bool = False):
        self.token = token
        self.output_dir = Path(output_dir)
        self.images_dir = self.output_dir / "_images"
        self.dry_run = dry_run
        self.full = full
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self.synced_ids = set()  # avoid infinite loops on linked pages
        self.visited_ids = set()  # track all ids seen this run (for deletion)
        self.manifest = Manifest(self.output_dir)
        self.stats = {"pages": 0, "skipped": 0, "databases": 0,
                      "images": 0, "deleted": 0, "errors": 0}

    def _request(self, method: str, url: str, **kwargs) -> dict | None:
        """Make an API request with rate limiting and error handling."""
        time.sleep(RATE_LIMIT_DELAY)
        try:
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2))
                print(f"  [rate limited] waiting {retry_after}s...")
                time.sleep(retry_after)
                resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 404:
                print(f"  [404] not found: {url}")
                self.stats["errors"] += 1
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            print(f"  [error] {e}")
            self.stats["errors"] += 1
            return None

    # -- API calls --

    def get_page(self, page_id: str) -> dict | None:
        return self._request("GET", f"{API_BASE}/pages/{page_id}")

    def get_page_markdown(self, page_id: str) -> dict | None:
        return self._request("GET", f"{API_BASE}/pages/{page_id}/markdown")

    def get_block_children(self, block_id: str) -> list[dict]:
        """Fetch all child blocks (handles pagination)."""
        blocks = []
        url = f"{API_BASE}/blocks/{block_id}/children?page_size=100"
        while url:
            data = self._request("GET", url)
            if not data:
                break
            blocks.extend(data.get("results", []))
            if data.get("has_more"):
                cursor = data.get("next_cursor")
                url = f"{API_BASE}/blocks/{block_id}/children?page_size=100&start_cursor={cursor}"
            else:
                url = None
        return blocks

    def query_database(self, database_id: str) -> list[dict]:
        """Fetch all rows from a database via its data source (handles pagination).

        Since API version 2025-09-03, databases and data sources are separate.
        Strategy:
          1. GET /v1/databases/{id} and use data_sources[0].id
          2. If no data_sources, try POST /v1/data_sources/{database_id}/query
             (works when data_source_id == database_id)
          3. If that also fails, it's likely a linked database — skip gracefully
        """
        # Get data source ID from database object
        db = self.get_database_metadata(database_id)
        if not db:
            return []
        data_sources = db.get("data_sources", [])
        if data_sources:
            ds_id = data_sources[0]["id"]
            return self._query_endpoint(f"{API_BASE}/data_sources/{ds_id}/query")

        # Fallback: try using database_id as data_source_id directly
        rows = self._query_endpoint(f"{API_BASE}/data_sources/{database_id}/query")
        if rows is not None:
            return rows

        # If we get here, it's probably a linked database
        is_linked = db.get("is_inline") is False or "linked" in str(db.get("parent", {}))
        print(f"  [skip] could not query database {database_id} — likely a linked database.")
        print(f"         Linked databases must be queried via their original source.")
        return []

    def _query_endpoint(self, url: str) -> list[dict] | None:
        """Paginated POST query against a data source endpoint. Returns None on error."""
        rows = []
        payload = {"page_size": 100}
        first_request = True
        while True:
            data = self._request("POST", url, json=payload)
            if not data:
                return None if first_request else rows
            first_request = False
            rows.extend(data.get("results", []))
            if data.get("has_more"):
                payload["start_cursor"] = data["next_cursor"]
            else:
                break
        return rows

    def get_database_metadata(self, database_id: str) -> dict | None:
        return self._request("GET", f"{API_BASE}/databases/{database_id}")

    # -- Image handling --

    def download_image(self, url: str) -> str | None:
        """Download an image, return local relative path from output_dir."""
        if self.dry_run:
            return None
        try:
            # Deterministic filename from URL (strip query params for hash)
            parsed = urlparse(url)
            url_for_hash = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            h = hashlib.sha256(url_for_hash.encode()).hexdigest()[:16]
            # Guess extension from path
            ext = Path(parsed.path).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff"):
                ext = ".png"
            filename = f"{h}{ext}"
            local_path = self.images_dir / filename

            if local_path.exists():
                return f"_images/{filename}"

            self.images_dir.mkdir(parents=True, exist_ok=True)
            time.sleep(RATE_LIMIT_DELAY)
            # Use plain requests (no Notion auth headers) — S3 pre-signed
            # URLs already contain auth via query params and reject extra
            # Authorization headers with 400.
            resp = requests.get(url, stream=True, timeout=30)
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            self.stats["images"] += 1
            return f"_images/{filename}"
        except Exception as e:
            print(f"  [image error] {e}")
            return None

    def process_images_in_markdown(self, markdown: str) -> str:
        """Find image URLs in markdown, download them, replace with local paths."""
        def replace_image(match):
            alt = match.group(1)
            url = match.group(2)
            if url.startswith("_images/") or url.startswith("./"):
                return match.group(0)  # already local
            local = self.download_image(url)
            if local:
                return f"![{alt}]({local})"
            return match.group(0)  # keep original if download failed

        return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", replace_image, markdown)

    # -- Incremental check --

    def _is_unchanged(self, page_id: str, last_edited_time: str) -> bool:
        """Check if a page is unchanged since last sync."""
        if self.full:
            return False
        prev = self.manifest.get_edited_time(page_id)
        return prev is not None and prev == last_edited_time

    # -- Sync logic --

    def _find_child_pages_and_dbs(self, block_id: str) -> tuple[list[dict], list[dict]]:
        """Recursively find all child_page and child_database blocks under a block.

        Digs into container blocks (column_list, column, toggle, synced_block,
        callout, quote, bulleted_list_item, numbered_list_item, etc.) that can
        nest child pages inside them.
        """
        child_pages = []
        child_dbs = []
        blocks = self.get_block_children(block_id)

        for block in blocks:
            btype = block.get("type", "")
            if btype == "child_page":
                child_pages.append(block)
            elif btype == "child_database":
                child_dbs.append(block)
            elif block.get("has_children"):
                # Recurse into any container block that has children
                nested_pages, nested_dbs = self._find_child_pages_and_dbs(block["id"])
                child_pages.extend(nested_pages)
                child_dbs.extend(nested_dbs)

        return child_pages, child_dbs

    def sync_page(self, page_id: str, parent_dir: Path, depth: int = 0):
        """Sync a single page and its children recursively."""
        page_id = extract_page_id(page_id)
        if page_id in self.synced_ids:
            return
        self.synced_ids.add(page_id)
        self.visited_ids.add(page_id)

        indent = "  " * depth

        # Get page metadata for title and last_edited_time
        page = self.get_page(page_id)
        if not page:
            return

        last_edited = page.get("last_edited_time", "")

        # Extract title
        props = page.get("properties", {})
        title = "Untitled"
        for prop in props.values():
            if prop.get("type") == "title":
                title = "".join(rt.get("plain_text", "") for rt in prop.get("title", []))
                break
        if not title:
            title = "Untitled"
        safe_title = sanitize_filename(title)

        # Discover children via blocks API — always do this even if page
        # content is unchanged, because children may have changed independently.
        child_pages, child_dbs = self._find_child_pages_and_dbs(page_id)

        # Determine file path
        if child_pages or child_dbs:
            page_dir = parent_dir / safe_title
            file_path = page_dir / "_index.md"
        else:
            file_path = parent_dir / f"{safe_title}.md"

        rel_path = str(file_path.relative_to(self.output_dir))

        # Check if page is unchanged
        if self._is_unchanged(page_id, last_edited):
            print(f"{indent}[skip] {title}")
            self.stats["skipped"] += 1
            # Update manifest path in case parent structure changed
            self.manifest.set(page_id, last_edited, rel_path)
        else:
            print(f"{indent}[page] {title}")

            # Get markdown content
            md_data = self.get_page_markdown(page_id)
            markdown = md_data.get("markdown", "") if md_data else ""
            truncated = md_data.get("truncated", False) if md_data else False
            unknown_ids = md_data.get("unknown_block_ids", []) if md_data else []

            # Handle truncated pages: fetch unknown blocks
            if truncated and unknown_ids:
                print(f"{indent}  [truncated] fetching {len(unknown_ids)} additional blocks...")
                for block_id in unknown_ids:
                    block_md = self.get_page_markdown(block_id)
                    if block_md and block_md.get("markdown"):
                        markdown += "\n" + block_md["markdown"]

            # Download images
            markdown = self.process_images_in_markdown(markdown)

            # Build frontmatter from properties (skip for pages with only title)
            frontmatter = ""
            non_title_props = {k: v for k, v in props.items() if v.get("type") != "title"}
            if non_title_props:
                frontmatter = properties_to_frontmatter(props)

            if not self.dry_run:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                content = frontmatter + markdown
                file_path.write_text(content, encoding="utf-8")

            self.manifest.set(page_id, last_edited, rel_path)
            self.stats["pages"] += 1

        # Always recurse into children
        for block in child_pages:
            child_id = block["id"]
            self.sync_page(child_id, parent_dir / safe_title, depth + 1)

        for block in child_dbs:
            child_id = block["id"]
            db_title = block.get("child_database", {}).get("title", "Untitled Database")
            self.sync_database(child_id, parent_dir / safe_title, db_title, depth + 1)

    def sync_database(self, database_id: str, parent_dir: Path, title: str = "", depth: int = 0):
        """Sync a database: create a directory, each row becomes a markdown file."""
        database_id = extract_page_id(database_id)
        if database_id in self.synced_ids:
            return
        self.synced_ids.add(database_id)
        self.visited_ids.add(database_id)

        indent = "  " * depth

        # Get database metadata for title if not provided
        if not title:
            db_meta = self.get_database_metadata(database_id)
            if db_meta:
                title_parts = db_meta.get("title", [])
                title = "".join(rt.get("plain_text", "") for rt in title_parts) or "Untitled Database"
            else:
                title = "Untitled Database"

        safe_title = sanitize_filename(title)
        print(f"{indent}[database] {title}")

        db_dir = parent_dir / safe_title
        if not self.dry_run:
            db_dir.mkdir(parents=True, exist_ok=True)

        # Query all rows
        rows = self.query_database(database_id)
        print(f"{indent}  {len(rows)} rows")

        for row in rows:
            row_id = row["id"]
            if row_id in self.synced_ids:
                continue
            self.synced_ids.add(row_id)
            self.visited_ids.add(row_id)

            last_edited = row.get("last_edited_time", "")

            # Extract row title
            row_props = row.get("properties", {})
            row_title = "Untitled"
            for prop in row_props.values():
                if prop.get("type") == "title":
                    row_title = "".join(rt.get("plain_text", "") for rt in prop.get("title", []))
                    break
            if not row_title:
                row_title = "Untitled"
            safe_row_title = sanitize_filename(row_title)

            # Check children for path determination
            child_pages, child_dbs = self._find_child_pages_and_dbs(row_id)

            if child_pages or child_dbs:
                file_path = db_dir / safe_row_title / "_index.md"
            else:
                file_path = db_dir / f"{safe_row_title}.md"

            rel_path = str(file_path.relative_to(self.output_dir))

            # Check if row is unchanged
            if self._is_unchanged(row_id, last_edited):
                print(f"{indent}  [skip] {row_title}")
                self.stats["skipped"] += 1
                self.manifest.set(row_id, last_edited, rel_path)
            else:
                print(f"{indent}  [row] {row_title}")

                # Get markdown content for this row
                md_data = self.get_page_markdown(row_id)
                markdown = md_data.get("markdown", "") if md_data else ""

                # Download images
                markdown = self.process_images_in_markdown(markdown)

                # Properties as frontmatter
                frontmatter = properties_to_frontmatter(row_props)

                if not self.dry_run:
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    content = frontmatter + markdown
                    file_path.write_text(content, encoding="utf-8")

                self.manifest.set(row_id, last_edited, rel_path)
                self.stats["pages"] += 1

            # Always recurse into children
            for block in child_pages:
                self.sync_page(block["id"], db_dir / safe_row_title, depth + 2)
            for block in child_dbs:
                db_t = block.get("child_database", {}).get("title", "Untitled Database")
                self.sync_database(block["id"], db_dir / safe_row_title, db_t, depth + 2)

        self.stats["databases"] += 1

    def detect_and_sync(self, root_id: str, output_dir: Path, depth: int = 0):
        """Auto-detect whether root_id is a page or database and sync accordingly."""
        rid = extract_page_id(root_id)

        # Try as page first
        page = self.get_page(rid)
        if page and page.get("object") == "page":
            self.sync_page(rid, output_dir, depth)
            return

        # Try as database
        db = self.get_database_metadata(rid)
        if db and db.get("object") == "database":
            title_parts = db.get("title", [])
            title = "".join(rt.get("plain_text", "") for rt in title_parts) or "Untitled Database"
            self.sync_database(rid, output_dir, title, depth)
            return

        print(f"  [error] could not resolve ID: {rid}")
        self.stats["errors"] += 1

    def _cleanup_deleted(self):
        """Remove local files for pages that no longer exist in Notion."""
        old_ids = self.manifest.all_ids()
        deleted_ids = old_ids - self.visited_ids
        for page_id in deleted_ids:
            rel_path = self.manifest.get_path(page_id)
            if rel_path:
                full_path = self.output_dir / rel_path
                if full_path.exists():
                    full_path.unlink()
                    print(f"[deleted] {rel_path}")
                    self.stats["deleted"] += 1
                # Clean up empty parent directories
                parent = full_path.parent
                while parent != self.output_dir:
                    try:
                        parent.rmdir()  # only removes if empty
                    except OSError:
                        break
                    parent = parent.parent
            self.manifest.remove(page_id)

    def run(self, root_ids: list[str]):
        """Sync all root pages/databases."""
        if not self.dry_run:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        for rid in root_ids:
            self.detect_and_sync(rid, self.output_dir)

        # Clean up pages that were deleted in Notion
        if not self.dry_run:
            self._cleanup_deleted()
            self.manifest.save()

        print(f"\nDone: {self.stats['pages']} updated, {self.stats['skipped']} unchanged, "
              f"{self.stats['databases']} databases, {self.stats['images']} images, "
              f"{self.stats['deleted']} deleted, {self.stats['errors']} errors")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"Config file not found: {path}")
        print(f"Create one from config.example.json and try again.")
        sys.exit(1)
    with open(p) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="Sync Notion to local markdown.")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to config.json")
    parser.add_argument("--workspace", help="Sync only this workspace (by name)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be synced")
    parser.add_argument("--full", action="store_true",
                        help="Force full sync, ignoring manifest (re-pull everything)")
    args = parser.parse_args()

    config = load_config(args.config)
    workspaces = config.get("workspaces", [])

    if args.workspace:
        workspaces = [w for w in workspaces if w["name"] == args.workspace]
        if not workspaces:
            print(f"Workspace '{args.workspace}' not found in config.")
            sys.exit(1)

    for ws in workspaces:
        name = ws["name"]
        token = ws["token"]
        roots = ws.get("roots", [])
        output_dir = ws.get("output_dir", f"./{name}")

        print(f"\n{'='*60}")
        print(f"Syncing workspace: {name}")
        print(f"Output: {output_dir}")
        print(f"Roots: {len(roots)}")
        mode = "full" if args.full else "incremental"
        print(f"Mode: {mode}")
        print(f"{'='*60}\n")

        syncer = NotionSync(token=token, output_dir=output_dir,
                            dry_run=args.dry_run, full=args.full)
        syncer.run(roots)


if __name__ == "__main__":
    main()
