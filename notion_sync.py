#!/usr/bin/env python3
"""
notion_sync.py — Pull Notion pages and databases to local markdown files.

Uses the Notion Markdown API (2026-03-11) for page content,
the Blocks API for child discovery, and the Databases API for row queries.
Downloads images to a local _images/ directory and rewrites URLs.
Incremental by default: only re-pulls pages whose last_edited_time has changed,
and only walks the block tree for pages/rows that changed (unchanged subtrees
reuse the child list recorded in the manifest).

Usage:
    python3 notion_sync.py                      # incremental sync all workspaces
    python3 notion_sync.py --full               # force full sync (ignore manifest)
    python3 notion_sync.py --config my.json     # use a custom config file
    python3 notion_sync.py --workspace personal # sync only one workspace
    python3 notion_sync.py --dry-run            # show what would be synced without writing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = "config.json"
API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
RATE_LIMIT_DELAY = 0.2  # seconds between requests; 429s are retried with backoff
MANIFEST_FILE = ".manifest.json"
MAX_WORKERS = 3
RATE_LIMIT_RPS = 4  # max requests per second across all threads


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class TokenBucket:
    """Token-bucket rate limiter with adaptive throttling, safe across threads."""

    def __init__(self, rate: float, min_rate: float = 1.0):
        self._initial_rate = rate
        self._min_rate = min_rate
        self._rate = rate
        self._capacity = rate
        self._tokens = rate
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity,
                                   self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._rate
            time.sleep(wait)

    def throttle(self):
        """Reduce rate by 25% after a 429. Called by _request on rate-limit hits."""
        with self._lock:
            new_rate = max(self._min_rate, self._rate * 0.75)
            if new_rate < self._rate:
                self._rate = new_rate
                self._capacity = new_rate
                print(f"  [throttle] reduced to {self._rate:.1f} req/s")

    def recover(self):
        """Nudge rate back up by 10% after a successful request."""
        with self._lock:
            if self._rate < self._initial_rate:
                self._rate = min(self._initial_rate, self._rate * 1.1)
                self._capacity = self._rate


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
        """Write the manifest atomically so an interrupted run can't corrupt it."""
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_name(self.file.name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        tmp.replace(self.file)

    def get_edited_time(self, page_id: str) -> str | None:
        entry = self.data.get(page_id)
        return entry.get("last_edited_time") if entry else None

    def get_path(self, page_id: str) -> str | None:
        entry = self.data.get(page_id)
        return entry.get("path") if entry else None

    def is_linked_database(self, database_id: str) -> bool:
        """True if a past sync determined this database is a linked view (unqueryable)."""
        entry = self.data.get(database_id)
        return bool(entry and entry.get("linked_database"))

    def mark_linked_database(self, database_id: str):
        self.data[database_id] = {"linked_database": True}

    def get_children(self, page_id: str) -> list[dict] | None:
        """Child page/database specs recorded on the last sync, or None if unknown."""
        entry = self.data.get(page_id)
        return entry.get("children") if entry else None

    def set(self, page_id: str, last_edited_time: str, path: str,
            children: list[dict] | None = None):
        entry = {
            "last_edited_time": last_edited_time,
            "path": path,
        }
        if children is not None:
            entry["children"] = children
        else:
            # Preserve a previously recorded child list when the caller
            # skipped the tree walk this run.
            prev = self.data.get(page_id)
            if prev and "children" in prev:
                entry["children"] = prev["children"]
        self.data[page_id] = entry

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
        self.claimed_paths = set()  # rel paths written/claimed this run (move cleanup)
        self.manifest = Manifest(self.output_dir)
        self.stats = {"pages": 0, "skipped": 0, "databases": 0,
                      "images": 0, "deleted": 0, "errors": 0}
        self._checkpoint_interval = 20.0  # seconds between incremental manifest saves
        self._last_checkpoint = time.time()
        self._lock = threading.Lock()  # protects stats, manifest, synced/visited/claimed
        self._rate_limiter = TokenBucket(RATE_LIMIT_RPS)
        self._pending: list[dict] = []  # work items deferred to the concurrent fetch pass
        self._edit_times: dict[str, str] = {}  # page_id → last_edited_time from Search prefetch

    def _checkpoint(self):
        """Persist the manifest periodically so an interrupted run keeps its progress."""
        if self.dry_run:
            return
        now = time.time()
        with self._lock:
            if now - self._last_checkpoint >= self._checkpoint_interval:
                self.manifest.save()
                self._last_checkpoint = now

    def _request(self, method: str, url: str, **kwargs) -> dict | None:
        """Make an API request with adaptive rate limiting and error handling."""
        for attempt in range(5):
            self._rate_limiter.acquire()
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", 2))
                    self._rate_limiter.throttle()
                    print(f"  [rate limited] waiting {retry_after}s (attempt {attempt + 1}/5)...")
                    time.sleep(retry_after)
                    continue
                if resp.status_code == 404:
                    print(f"  [404] not found: {url}")
                    with self._lock:
                        self.stats["errors"] += 1
                    return None
                resp.raise_for_status()
                self._rate_limiter.recover()
                return resp.json()
            except requests.RequestException as e:
                print(f"  [error] {e}")
                with self._lock:
                    self.stats["errors"] += 1
                return None
        print(f"  [error] gave up after repeated rate limiting: {url}")
        with self._lock:
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
            return self._query_endpoint(f"{API_BASE}/data_sources/{ds_id}/query") or []

        # Fallback: try using database_id as data_source_id directly
        rows = self._query_endpoint(f"{API_BASE}/data_sources/{database_id}/query")
        if rows is not None:
            return rows

        # If we get here, it's probably a linked database. Record that so future
        # syncs skip it up front instead of repeating these failing requests.
        print(f"  [skip] could not query database {database_id} — likely a linked database.")
        print(f"         Linked databases must be queried via their original source.")
        self.manifest.mark_linked_database(database_id)
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

    def search_root_pages(self) -> list[dict]:
        """Discover all workspace-level pages and databases via the Search API."""
        roots = []
        payload = {
            "filter": {"property": "object", "value": "page"},
            "page_size": 100,
        }
        while True:
            data = self._request("POST", f"{API_BASE}/search", json=payload)
            if not data:
                break
            for result in data.get("results", []):
                parent = result.get("parent", {})
                if parent.get("type") == "workspace":
                    roots.append(result)
            if data.get("has_more"):
                payload["start_cursor"] = data["next_cursor"]
            else:
                break
        return roots

    def prefetch_edit_times(self):
        """Pre-fetch last_edited_time for all pages via Search API.

        Populates self._edit_times so sync_page can check whether a page
        changed without calling get_page.  Falls back gracefully: pages not
        in the dict still get a get_page call.
        """
        print("[prefetch] loading edit times via Search API...")
        payload: dict = {"page_size": 100}
        count = 0
        while True:
            data = self._request("POST", f"{API_BASE}/search", json=payload)
            if not data:
                break
            for result in data.get("results", []):
                if result.get("object") == "page":
                    self._edit_times[result["id"]] = result.get("last_edited_time", "")
                    count += 1
            if data.get("has_more"):
                payload["start_cursor"] = data["next_cursor"]
            else:
                break
        print(f"[prefetch] {count} pages indexed in {len(self._edit_times)} entries")

    def _get_edit_time(self, page_id: str) -> str | None:
        """Look up a page's last_edited_time from the prefetch cache.

        Returns None if the page wasn't in the Search results (new page,
        consistency delay, etc.) — the caller should fall back to get_page.
        """
        return self._edit_times.get(page_id)

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
        """Find image URLs in markdown, download them concurrently, replace with local paths."""
        matches = list(re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", markdown))
        if not matches:
            return markdown

        remote = [(m, m.group(2)) for m in matches
                  if not m.group(2).startswith(("_images/", "./"))]
        if not remote:
            return markdown

        url_to_local: dict[str, str | None] = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(self.download_image, url): url
                       for _, url in remote}
            for fut in as_completed(futures):
                url_to_local[futures[fut]] = fut.result()

        def replace_image(match):
            url = match.group(2)
            local = url_to_local.get(url)
            if local:
                return f"![{match.group(1)}]({local})"
            return match.group(0)

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

    def _discover_children(self, block_id: str, unchanged: bool,
                           cached: list[dict] | None) -> tuple[list[dict], bool]:
        """Return (child_specs, walked).

        child_specs is a list of {"id", "kind"[, "title"]} dicts for the child
        pages and databases directly under this page/row. When the page is
        unchanged and a child list was recorded on a previous sync, trust it and
        skip the Blocks API tree walk: adding or removing a child edits the page
        and would have bumped its last_edited_time, so an unchanged page's
        children are exactly what they were last run. This saves one Blocks API
        call per unchanged page — and, crucially, per unchanged database row.
        """
        if unchanged and cached is not None:
            return cached, False
        child_pages, child_dbs = self._find_child_pages_and_dbs(block_id)
        specs = [
            {"id": b["id"], "kind": "page",
             "title": b.get("child_page", {}).get("title", "Untitled"),
             "last_edited_time": b.get("last_edited_time", "")}
            for b in child_pages
        ]
        specs += [
            {"id": b["id"], "kind": "database",
             "title": b.get("child_database", {}).get("title", "Untitled Database")}
            for b in child_dbs
        ]
        return specs, True

    def _recurse_children(self, child_specs: list[dict], parent_dir: Path, depth: int,
                          from_cache: bool = False):
        if not child_specs:
            return

        # Databases are kept serial — they have their own internal concurrency.
        # Sibling pages are explored concurrently so their get_page /
        # get_block_children calls overlap instead of waiting in sequence.
        page_specs = [s for s in child_specs if s.get("kind") != "database"]
        db_specs = [s for s in child_specs if s.get("kind") == "database"]

        # Only trust hint_title/hint_edited_time when the child list was freshly
        # walked from the Blocks API. Cached specs carry stale values — a child
        # page renamed or edited since the last sync would have a new
        # last_edited_time that the cache doesn't reflect, so we must call
        # get_page to check.
        def _hints(spec):
            if from_cache:
                return {}
            return {"hint_title": spec.get("title"),
                    "hint_edited_time": spec.get("last_edited_time")}

        if len(page_specs) > 1:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {
                    pool.submit(
                        self.sync_page, spec["id"], parent_dir, depth,
                        **_hints(spec),
                    ): spec
                    for spec in page_specs
                }
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception as e:
                        spec = futures[fut]
                        print(f"  [error] sync_page failed for {spec['id']}: {e}")
                        with self._lock:
                            self.stats["errors"] += 1
        else:
            for spec in page_specs:
                self.sync_page(spec["id"], parent_dir, depth, **_hints(spec))

        for spec in db_specs:
            self.sync_database(spec["id"], parent_dir, spec.get("title", ""), depth)

    def sync_page(self, page_id: str, parent_dir: Path, depth: int = 0,
                  hint_title: str | None = None,
                  hint_edited_time: str | None = None):
        """Sync a single page and its children recursively.

        Uses three sources for last_edited_time, in priority order:
          1. Search API prefetch cache (_edit_times) — cheapest, covers most pages
          2. Hint from parent's freshly-walked block listing — free, but only
             available when the parent was changed and re-walked
          3. get_page API call — always correct, used as fallback
        """
        page_id = extract_page_id(page_id)
        with self._lock:
            if page_id in self.synced_ids:
                return
            self.synced_ids.add(page_id)
            self.visited_ids.add(page_id)

        indent = "  " * depth

        # Resolve last_edited_time without calling get_page if possible.
        prefetched_time = self._get_edit_time(page_id)
        edit_time = prefetched_time or hint_edited_time

        if edit_time and self._is_unchanged(page_id, edit_time):
            # Page hasn't changed — skip content fetch, reuse manifest data.
            # We still need the title for the file path. Use hint if available,
            # otherwise derive from the manifest's recorded path.
            title = hint_title
            if not title:
                prev_path = self.manifest.get_path(page_id)
                if prev_path:
                    p = Path(prev_path)
                    if p.name == "_index.md":
                        title = p.parent.name
                    else:
                        title = p.stem
            title = title or "Untitled"
            safe_title = sanitize_filename(title)

            child_specs, _ = self._discover_children(
                page_id, True, self.manifest.get_children(page_id))

            if child_specs:
                file_path = parent_dir / safe_title / "_index.md"
            else:
                file_path = parent_dir / f"{safe_title}.md"

            rel_path = str(file_path.relative_to(self.output_dir))
            with self._lock:
                needs_refetch = self._claim_path(page_id, rel_path)
            if needs_refetch:
                pass  # fall through to full path below
            else:
                with self._lock:
                    print(f"{indent}[skip] {title}")
                    self.stats["skipped"] += 1
                    self.manifest.set(page_id, edit_time, rel_path)
                self._checkpoint()
                self._recurse_children(child_specs, parent_dir / safe_title, depth + 1,
                                       from_cache=True)
                return

        # Full path: fetch page metadata (needed for properties / frontmatter).
        # Reached when the page changed, is new, or the local file is missing.
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

        # Discover child pages/databases. For an unchanged page this reuses the
        # child list recorded last run instead of walking the block tree again.
        unchanged = self._is_unchanged(page_id, last_edited)
        child_specs, walked = self._discover_children(
            page_id, unchanged, self.manifest.get_children(page_id))
        children_to_store = child_specs if walked else None

        # Determine file path
        if child_specs:
            file_path = parent_dir / safe_title / "_index.md"
        else:
            file_path = parent_dir / f"{safe_title}.md"

        rel_path = str(file_path.relative_to(self.output_dir))
        with self._lock:
            needs_refetch = self._claim_path(page_id, rel_path)

            if unchanged and not needs_refetch:
                print(f"{indent}[skip] {title}")
                self.stats["skipped"] += 1
                self.manifest.set(page_id, last_edited, rel_path, children_to_store)
            else:
                print(f"{indent}[page] {title}")
                self._pending.append({
                    "page_id": page_id,
                    "file_path": file_path,
                    "rel_path": rel_path,
                    "props": props,
                    "last_edited": last_edited,
                    "children_to_store": children_to_store,
                    "indent": indent,
                })

        self._checkpoint()

        # Always recurse into children
        self._recurse_children(child_specs, parent_dir / safe_title, depth + 1,
                               from_cache=not walked)

    def sync_database(self, database_id: str, parent_dir: Path, title: str = "", depth: int = 0):
        """Sync a database: create a directory, each row becomes a markdown file."""
        database_id = extract_page_id(database_id)
        with self._lock:
            if database_id in self.synced_ids:
                return
            self.synced_ids.add(database_id)
            self.visited_ids.add(database_id)

        indent = "  " * depth

        # A database previously found to be a linked view can't be queried; don't
        # waste requests rediscovering that every run (--full re-checks).
        if not self.full and self.manifest.is_linked_database(database_id):
            print(f"{indent}[skip] linked database {title or database_id}")
            return

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

            row_props = row.get("properties", {})
            row_title = "Untitled"
            for prop in row_props.values():
                if prop.get("type") == "title":
                    row_title = "".join(
                        rt.get("plain_text", "") for rt in prop.get("title", []))
                    break
            if not row_title:
                row_title = "Untitled"
            safe_row_title = sanitize_filename(row_title)

            unchanged = self._is_unchanged(row_id, last_edited)
            child_specs, walked = self._discover_children(
                row_id, unchanged, self.manifest.get_children(row_id))
            children_to_store = child_specs if walked else None

            if child_specs:
                file_path = db_dir / safe_row_title / "_index.md"
            else:
                file_path = db_dir / f"{safe_row_title}.md"

            rel_path = str(file_path.relative_to(self.output_dir))
            needs_refetch = self._claim_path(row_id, rel_path)

            if unchanged and not needs_refetch:
                print(f"{indent}  [skip] {row_title}")
                self.stats["skipped"] += 1
                self.manifest.set(row_id, last_edited, rel_path, children_to_store)
            else:
                print(f"{indent}  [row] {row_title}")
                self._pending.append({
                    "page_id": row_id,
                    "file_path": file_path,
                    "rel_path": rel_path,
                    "props": row_props,
                    "last_edited": last_edited,
                    "children_to_store": children_to_store,
                    "indent": indent + "  ",
                })

            self._checkpoint()
            self._recurse_children(child_specs, db_dir / safe_row_title, depth + 2,
                                   from_cache=not walked)

        self.stats["databases"] += 1

    def _fetch_item(self, item: dict):
        """Pass 2: fetch markdown content, download images, and write one file."""
        page_id = item["page_id"]
        file_path = item["file_path"]
        indent = item["indent"]

        md_data = self.get_page_markdown(page_id)
        markdown = md_data.get("markdown", "") if md_data else ""
        truncated = md_data.get("truncated", False) if md_data else False
        unknown_ids = md_data.get("unknown_block_ids", []) if md_data else []

        if truncated and unknown_ids:
            print(f"{indent}  [truncated] fetching {len(unknown_ids)} additional blocks...")
            for block_id in unknown_ids:
                block_md = self.get_page_markdown(block_id)
                if block_md and block_md.get("markdown"):
                    markdown += "\n" + block_md["markdown"]

        markdown = self.process_images_in_markdown(markdown)

        props = item["props"]
        frontmatter = ""
        non_title_props = {k: v for k, v in props.items() if v.get("type") != "title"}
        if non_title_props:
            frontmatter = properties_to_frontmatter(props)

        if not self.dry_run:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            content = frontmatter + markdown
            file_path.write_text(content, encoding="utf-8")

        with self._lock:
            self.manifest.set(page_id, item["last_edited"], item["rel_path"],
                              item["children_to_store"])
            self.stats["pages"] += 1

        self._checkpoint()

    def _fetch_all_pending(self):
        """Pass 2: fetch content for all discovered pages concurrently."""
        if not self._pending:
            return
        print(f"\n[fetch] downloading content for {len(self._pending)} pages...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(self._fetch_item, item): item
                       for item in self._pending}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    item = futures[fut]
                    print(f"  [error] fetch failed for {item['page_id']}: {e}")
                    with self._lock:
                        self.stats["errors"] += 1
        self._pending.clear()

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

    def _remove_local_file(self, rel_path: str, reason: str):
        """Delete one tracked local file and prune parent dirs left empty."""
        if self.dry_run or not rel_path:
            return
        full_path = self.output_dir / rel_path
        if full_path.exists():
            full_path.unlink()
            print(f"[{reason}] {rel_path}")
            self.stats["deleted"] += 1
        parent = full_path.parent
        while parent != self.output_dir:
            try:
                parent.rmdir()  # only removes if empty
            except OSError:
                break
            parent = parent.parent

    def _claim_path(self, page_id: str, rel_path: str) -> bool:
        """Record this page's current file path and handle stale files.

        Returns True if the file at rel_path is missing and needs a re-fetch
        (the old file was already gone, or the path changed and move failed).
        """
        needs_refetch = False
        prev_path = self.manifest.get_path(page_id)
        if (prev_path and prev_path != rel_path
                and prev_path not in self.claimed_paths):
            moved = self._move_local_file(prev_path, rel_path)
            if not moved:
                needs_refetch = True
        elif not (self.output_dir / rel_path).exists():
            needs_refetch = True
        self.claimed_paths.add(rel_path)
        return needs_refetch

    def _move_local_file(self, old_rel: str, new_rel: str) -> bool:
        """Move a tracked local file to a new path, pruning empty parents.

        Returns True if the file was moved, False if the source was missing.
        """
        if self.dry_run:
            return True
        old_path = self.output_dir / old_rel
        new_path = self.output_dir / new_rel
        moved = False
        if old_path.exists():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.rename(new_path)
            print(f"[moved] {old_rel} → {new_rel}")
            moved = True
        else:
            print(f"[moved] {old_rel} → {new_rel} (source missing, will re-fetch)")
        # Prune empty parent dirs left behind
        parent = old_path.parent
        while parent != self.output_dir:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return moved

    def _cleanup_deleted(self):
        """Remove local files for pages that no longer exist in Notion."""
        old_ids = self.manifest.all_ids()
        deleted_ids = old_ids - self.visited_ids
        for page_id in deleted_ids:
            self._remove_local_file(self.manifest.get_path(page_id), "deleted")
            self.manifest.remove(page_id)

    def _extract_title(self, page: dict) -> str:
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                t = "".join(rt.get("plain_text", "") for rt in prop.get("title", []))
                if t:
                    return t
        return "Untitled"

    def run(self, root_ids: list[str] | None = None):
        """Sync all root pages/databases.

        If root_ids is empty or None, auto-discover workspace-level pages.
        """
        if not self.dry_run:
            self.output_dir.mkdir(parents=True, exist_ok=True)

        # Resolve roots
        if root_ids:
            resolved = [(extract_page_id(rid), None) for rid in root_ids]
        else:
            print("No roots configured — auto-discovering workspace-level pages...")
            pages = self.search_root_pages()
            resolved = [(p["id"], self._extract_title(p)) for p in pages]

        print(f"\nRoot pages ({len(resolved)}):")
        for rid, title in resolved:
            if title:
                print(f"  • {title}  ({rid})")
            else:
                page = self.get_page(rid)
                name = self._extract_title(page) if page else rid
                print(f"  • {name}  ({rid})")
        print()

        # Save whatever progress we have if the run is interrupted (Ctrl-C or a
        # kill from a task runner), so the child-list cache built so far survives.
        def _save_and_exit(signum, frame):
            print("\n[interrupted] saving manifest before exit...")
            if not self.dry_run:
                self.manifest.save()
            sys.exit(130)

        old_handlers = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, _save_and_exit)
        try:
            # Pre-fetch edit times so Pass 1 can skip get_page for unchanged pages
            if not self.full:
                self.prefetch_edit_times()

            # Pass 1: discover tree structure
            for rid, _ in resolved:
                self.detect_and_sync(rid, self.output_dir)

            # Pass 2: fetch content concurrently
            self._fetch_all_pending()

            # Clean up pages that were deleted in Notion
            if not self.dry_run:
                self._cleanup_deleted()
                self.manifest.save()
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)

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
    parser.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                        help="Wait before syncing (e.g. --wait 45 after a recent edit)")
    args = parser.parse_args()

    if args.wait > 0:
        print(f"Waiting {args.wait}s for Notion to index recent changes...")
        time.sleep(args.wait)

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
        mode = "full" if args.full else "incremental"
        print(f"Mode: {mode}")
        print(f"{'='*60}")

        syncer = NotionSync(token=token, output_dir=output_dir,
                            dry_run=args.dry_run, full=args.full)
        syncer.run(roots or None)


if __name__ == "__main__":
    main()