"""CLI entry point for conex v2.

Entry point: main() — registered as `conex` script in pyproject.toml (wave 4).

Command surface:
  configure [--local DIR]
  spaces
  tree SPACE
  find SPACE QUERY
  export SPACE -o DIR [options]
  refresh SPACE -o DIR
  diff SPACE -o DIR [options]

export flow (exact):
  1. resolve_config  (with CLI overrides)
  2. preflight banner  (config source, auth mode, API mode, site, output dir)
  3. ExportLock  (exclusive flock on .conex/lock)
  4. clear .conex/tmp  (EXACTLY once per locked command; stores never do this)
  5. pull  (unless --cached; --cached with no snapshot → clean ConexError)
  6. commit_user_changes  (if git enabled and repo ensured)
  7. build
  8. commit_export
  9. summary line  (written/skipped/moved/pruned counts + warnings recap)
  exit 0 even with warnings (v1 parity).

refresh flow: lock → clear tmp → pull only.
diff flow: lock → clear tmp → pull → report add/change/move/delete vs state.

GitError at CLI level → warn and continue (v1 parity).
ConexError subclasses → clean one-line stderr + exit 1; never a traceback.
LockHeldError message names the lock path (v1 parity).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Sequence

from conex.config import (
    Dialect,
    ResolvedConfig,
    resolve_config,
    save_global_config,
    save_local_config,
    configure as config_configure,
)
from conex.errors import ConexError, GitError, LockHeldError
from conex.store.lock import ExportLock


# ---------------------------------------------------------------------------
# Dialect / auth label helpers
# ---------------------------------------------------------------------------


def _auth_mode_label(cfg: ResolvedConfig) -> str:
    """Human-readable auth mode for the preflight banner."""
    dialect = cfg.dialect
    if dialect is Dialect.COOKIE_V1:
        return "cookie session"
    # Distinguish basic vs bearer from the header content.
    auth = cfg.auth_headers.get("Authorization", "")
    if auth.startswith("Basic "):
        return "Basic API token"
    if auth.startswith("Bearer "):
        return "Bearer token (PAT)"
    return "unknown"


def _api_mode_label(cfg: ResolvedConfig) -> str:
    """Human-readable API mode for the preflight banner."""
    if cfg.dialect is Dialect.CLOUD_V2:
        return "Confluence REST v2"
    if cfg.dialect is Dialect.GATEWAY_V2:
        return "OAuth gateway"
    if cfg.dialect is Dialect.COOKIE_V1:
        return "Confluence REST v1 compatibility"
    return str(cfg.dialect)  # pragma: no cover


# ---------------------------------------------------------------------------
# Preflight banner — NEVER emit credentials
# ---------------------------------------------------------------------------


def _print_preflight_banner(cfg: ResolvedConfig, output_dir: Path) -> None:
    """Print preflight information to stderr; never include credential values."""
    print(f"Config source: {cfg.source_description}", file=sys.stderr)
    print(f"Auth: {_auth_mode_label(cfg)}", file=sys.stderr)
    print(f"API mode: {_api_mode_label(cfg)}", file=sys.stderr)
    print(f"Site: {cfg.site_url}", file=sys.stderr)
    print(f"Output: {output_dir.resolve()}", file=sys.stderr)


# ---------------------------------------------------------------------------
# tmp-clear helper
# ---------------------------------------------------------------------------


def _clear_tmp(root: Path) -> None:
    """Remove and recreate <root>/.conex/tmp/.

    Called EXACTLY ONCE per locked command, immediately after acquiring the lock.
    Stores must never clear tmp themselves (I4).
    """
    tmp = root / ".conex" / "tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Overrides dict from parsed args
# ---------------------------------------------------------------------------


def _overrides_from_args(args: argparse.Namespace) -> dict:
    """Collect CLI credential/config flags into a dict for resolve_config."""
    overrides: dict = {}
    site_url = getattr(args, "site_url", None) or getattr(args, "base_url", None)
    if site_url:
        overrides["site_url"] = site_url
    if getattr(args, "api_base_url", None):
        overrides["api_base_url"] = args.api_base_url
    if getattr(args, "cloud_id", None):
        overrides["cloud_id"] = args.cloud_id
    if getattr(args, "email", None):
        overrides["email"] = args.email
    if getattr(args, "api_token", None):
        overrides["api_token"] = args.api_token
    if getattr(args, "cookie", None):
        overrides["cookie"] = args.cookie
    if getattr(args, "auth_type", None):
        overrides["auth_type"] = args.auth_type
    if getattr(args, "verbose", False):
        overrides["verbose"] = True
    return overrides


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


def _cmd_configure(args: argparse.Namespace) -> None:
    """Interactive credential setup.

    When --local DIR is given, writes .conex/config.json under DIR.
    """
    local_dir = getattr(args, "local", None)
    try:
        config_configure(output_dir=local_dir, local=local_dir is not None)
    except ConexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_spaces(args: argparse.Namespace) -> None:
    """List accessible Confluence spaces."""
    overrides = _overrides_from_args(args)
    output_dir = Path.cwd()
    try:
        cfg = resolve_config(output_dir, overrides)
    except ConexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    from conex.http import Http

    # The ConfluenceAPI protocol only exposes get_space(key) — for listing all
    # spaces we call the underlying HTTP layer directly (not part of the frozen
    # protocol; used only by this command).
    http = Http(auth_headers=cfg.auth_headers)
    base = cfg.api_base_url.rstrip("/")

    try:
        if cfg.dialect is Dialect.COOKIE_V1:
            # v1 REST: /wiki/rest/api/space (offset pagination via _links.next)
            spaces = []
            url: str | None = base + "/wiki/rest/api/space"
            params: dict | None = {"limit": "50", "expand": ""}
            while url:
                data = http.get_json(url, params)
                results = data.get("results") or []
                for r in results:
                    spaces.append({
                        "key": r.get("key", ""),
                        "name": r.get("name", ""),
                        "type": r.get("type", ""),
                    })
                next_link = (data.get("_links") or {}).get("next")
                if next_link:
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(next_link)
                    url = base + parsed.path
                    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                else:
                    url = None
        else:
            # v2: /wiki/api/v2/spaces (cursor pagination via _links.next)
            spaces = []
            url = base + "/wiki/api/v2/spaces"
            params = {"limit": "50"}
            while url:
                from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs
                data = http.get_json(url, params)
                results = data.get("results") or []
                for r in results:
                    spaces.append({
                        "key": r.get("key", ""),
                        "name": r.get("name", ""),
                        "type": r.get("type", ""),
                    })
                next_link = (data.get("_links") or {}).get("next")
                if next_link:
                    parsed2 = _urlparse(next_link)
                    url = base + parsed2.path
                    params = {k: v[0] for k, v in _parse_qs(parsed2.query).items()}
                else:
                    url = None
    except ConexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not spaces:
        print("No spaces found.")
        return

    key_w = max(len(s["key"]) for s in spaces)
    name_w = max(len(s["name"]) for s in spaces)
    print(f"{'KEY':<{key_w}}  {'NAME':<{name_w}}  TYPE")
    print(f"{'-' * key_w}  {'-' * name_w}  ----")
    for s in spaces:
        print(f"{s['key']:<{key_w}}  {s['name']:<{name_w}}  {s.get('type', '')}")


def _cmd_tree(args: argparse.Namespace) -> None:
    """Show the page hierarchy for a space."""
    overrides = _overrides_from_args(args)
    output_dir = Path.cwd()
    try:
        cfg = resolve_config(output_dir, overrides)
    except ConexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    from conex.api import make_api
    from conex.layout import plan_layout

    try:
        api = make_api(cfg)
        space = api.get_space(args.space_key)
        pages = api.get_pages(space.id, space.key, include_archived=False)
        folders = api.get_folders(space.id)
    except ConexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    plan = plan_layout(space, pages, folders)

    # Build a simple tree display.
    _print_tree(space, pages, folders, plan)
    print(f"\n{len(pages)} pages")


def _print_tree(space, pages, folders, plan) -> None:
    """Print a depth-first tree to stdout."""
    # Build id → title map for display.
    id_to_title: dict[str, str] = {p.id: p.title for p in pages}

    # Depth: path has 1 slash per level above root ("Space/Page" = depth 0,
    # "Space/Parent/Child" = depth 1).  Subtract 1 to anchor roots at 0.
    items = []
    for pid in plan.order:
        title = id_to_title.get(pid, pid)
        path_str = str(plan.dirs[pid])
        depth = max(0, path_str.count("/") - 1)
        items.append((depth, title))

    for depth, title in items:
        indent = "  " * depth
        print(f"{indent}{title}")


def _cmd_find(args: argparse.Namespace) -> None:
    """Search pages in a space by title substring."""
    overrides = _overrides_from_args(args)
    output_dir = Path.cwd()
    try:
        cfg = resolve_config(output_dir, overrides)
    except ConexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    from conex.api import make_api
    from conex.layout import plan_layout

    try:
        api = make_api(cfg)
        space = api.get_space(args.space_key)
        pages = api.get_pages(space.id, space.key, include_archived=False)
        folders = api.get_folders(space.id)
    except ConexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    query = args.query.lower()
    matches = [p for p in pages if query in p.title.lower()]

    if not matches:
        print(f"No pages matching {args.query!r}.")
        return

    plan = plan_layout(space, pages, folders)
    id_w = max(len(p.id) for p in matches)
    for page in matches:
        path_str = str(plan.dirs.get(page.id, PurePosixPath(page.title)))
        print(f"{page.id:<{id_w}}  {path_str}")


def _cmd_export(args: argparse.Namespace) -> None:
    """Export a Confluence space as LLM-ready markdown."""
    output_dir = Path(args.output).expanduser().resolve()
    overrides = _overrides_from_args(args)

    try:
        cfg = resolve_config(output_dir, overrides)
    except ConexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    _print_preflight_banner(cfg, output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with ExportLock(output_dir):
            _clear_tmp(output_dir)
            _run_export(args, cfg, output_dir)
    except LockHeldError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except ConexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _run_export(args: argparse.Namespace, cfg: ResolvedConfig, output_dir: Path) -> None:
    """Inner export logic; called with lock held."""
    from conex.api import make_api
    from conex.build import build, BuildOptions
    from conex.pull import pull, PullOptions
    from conex.store.blobs import BlobStore
    from conex.store.state import StateStore, SnapshotStore

    blobs = BlobStore(output_dir)
    snapshot_store = SnapshotStore(output_dir)
    state_store = StateStore(output_dir)

    no_author_lookup = getattr(args, "no_author_lookup", False)
    include_archived = getattr(args, "include_archived", False)
    cached = getattr(args, "cached", False)

    if cached:
        snapshot = snapshot_store.load()
        if snapshot is None:
            raise ConexError(
                "--cached specified but no snapshot found; run without --cached first"
            )
    else:
        api = make_api(cfg)
        prev_snapshot = snapshot_store.load()
        pull_opts = PullOptions(
            include_archived=include_archived,
            fetch_media=not getattr(args, "no_media", False),
            author_lookup=not no_author_lookup,
        )
        snapshot = pull(api, args.space_key, output_dir, blobs, prev_snapshot, pull_opts)

    prev_state = state_store.load()

    # git: ensure repo + commit user changes before export
    use_git = not getattr(args, "no_git", False)
    if use_git:
        try:
            import conex.gitio as gitio
            gitio.ensure_repo(output_dir)
            gitio.commit_user_changes(output_dir)
        except GitError as exc:
            print(f"Warning: git error (continuing without git): {exc}", file=sys.stderr)
            use_git = False

    build_opts = BuildOptions(
        include_html=getattr(args, "include_html", False),
        media=not getattr(args, "no_media", False),
        render_drawio=not getattr(args, "no_drawio_render", False),
        author_lookup=not no_author_lookup,
        subtree=getattr(args, "path", None),
        no_children=getattr(args, "no_children", False),
    )

    api_for_build = None
    if not cached and build_opts.author_lookup:
        try:
            api_for_build = make_api(cfg)
        except ConexError:
            pass

    result, new_state = build(output_dir, snapshot, blobs, prev_state, build_opts, api_for_build)

    # git: commit export delta
    if use_git and (result.written or result.deleted):
        try:
            import conex.gitio as gitio
            import datetime
            now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            message = f"conex export {args.space_key} {now}"
            gitio.commit_export(output_dir, result, message)
        except GitError as exc:
            print(f"Warning: git error after build: {exc}", file=sys.stderr)

    # Summary line
    pruned = 0
    if prev_state is not None:
        prev_ids = set(prev_state.pages.keys())
        new_ids = set(new_state.pages.keys())
        pruned = len(prev_ids - new_ids)

    print(
        f"Export complete: "
        f"{len(result.written)} written, "
        f"{result.skipped} skipped, "
        f"{len(result.moved)} moved, "
        f"{pruned} pruned"
    )

    if result.warnings:
        print(f"Warnings ({len(result.warnings)}):", file=sys.stderr)
        for w in result.warnings:
            print(f"  {w}", file=sys.stderr)


def _cmd_refresh(args: argparse.Namespace) -> None:
    """Pull the latest data from Confluence without building the output tree."""
    output_dir = Path(args.output).expanduser().resolve()
    overrides = _overrides_from_args(args)

    try:
        cfg = resolve_config(output_dir, overrides)
    except ConexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with ExportLock(output_dir):
            _clear_tmp(output_dir)
            _run_refresh(args, cfg, output_dir)
    except LockHeldError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except ConexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _run_refresh(args: argparse.Namespace, cfg: ResolvedConfig, output_dir: Path) -> None:
    """Inner refresh logic; called with lock held."""
    from conex.api import make_api
    from conex.pull import pull, PullOptions
    from conex.store.blobs import BlobStore
    from conex.store.state import SnapshotStore

    blobs = BlobStore(output_dir)
    snapshot_store = SnapshotStore(output_dir)

    api = make_api(cfg)
    prev_snapshot = snapshot_store.load()
    pull_opts = PullOptions()
    snapshot = pull(api, args.space_key, output_dir, blobs, prev_snapshot, pull_opts)
    print(f"Refreshed: {len(snapshot.pages)} pages fetched at {snapshot.fetched_at}")


def _cmd_diff(args: argparse.Namespace) -> None:
    """Pull and report what has changed since the last export."""
    output_dir = Path(args.output).expanduser().resolve()
    overrides = _overrides_from_args(args)
    include_archived = getattr(args, "include_archived", False)

    try:
        cfg = resolve_config(output_dir, overrides)
    except ConexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with ExportLock(output_dir):
            _clear_tmp(output_dir)
            _run_diff(args, cfg, output_dir, include_archived)
    except LockHeldError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except ConexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _run_diff(
    args: argparse.Namespace,
    cfg: ResolvedConfig,
    output_dir: Path,
    include_archived: bool,
) -> None:
    """Inner diff logic; called with lock held."""
    from conex.api import make_api
    from conex.pull import pull, PullOptions
    from conex.store.blobs import BlobStore
    from conex.store.state import StateStore, SnapshotStore

    blobs = BlobStore(output_dir)
    snapshot_store = SnapshotStore(output_dir)
    state_store = StateStore(output_dir)

    api = make_api(cfg)
    prev_snapshot = snapshot_store.load()
    pull_opts = PullOptions(include_archived=include_archived, fetch_media=False)
    snapshot = pull(api, args.space_key, output_dir, blobs, prev_snapshot, pull_opts)

    prev_state = state_store.load()
    if prev_state is None:
        print("No previous export state found; run `conex export` first.")
        return

    _report_diff(snapshot, prev_state, args)


def _report_diff(snapshot, prev_state, args) -> None:
    """Compute and print add/change/move/delete vs prev state."""
    from conex.layout import plan_layout

    path_filter = getattr(args, "path", None)
    plan = plan_layout(snapshot.space, snapshot.pages, snapshot.folders, subtree=path_filter)

    current_ids = set(plan.dirs.keys())

    # When a subtree filter is active, restrict prev_ids to only those pages
    # whose recorded dir is inside the subtree's planned root directory.
    # Pages outside the subtree scope are out-of-scope — not deleted.
    if path_filter is not None and plan.subtree_dir is not None:
        subtree_prefix = str(plan.subtree_dir)
        prev_ids = {
            pid
            for pid, ps in prev_state.pages.items()
            if ps.dir == subtree_prefix or ps.dir.startswith(subtree_prefix + "/")
        }
    elif path_filter is not None and plan.subtree_dir is None:
        # Subtree filter specified but subtree not found — plan is empty.
        # prev_ids is empty too so nothing is reported as deleted.
        prev_ids = set()
    else:
        prev_ids = set(prev_state.pages.keys())

    added = current_ids - prev_ids
    deleted = prev_ids - current_ids
    changed = set()
    moved = set()

    for pid in current_ids & prev_ids:
        prev_ps = prev_state.pages[pid]
        planned_dir = str(plan.dirs[pid])
        if prev_ps.dir != planned_dir:
            moved.add(pid)

        # Check version change
        snap_page = next((p for p in snapshot.pages if p.id == pid), None)
        if snap_page and snap_page.version.number != prev_ps.version:
            changed.add(pid)

    # Remove moved from changed to avoid double-counting
    changed -= moved

    id_to_title = {p.id: p.title for p in snapshot.pages}
    prev_title = {pid: ps.title for pid, ps in prev_state.pages.items()}

    def _label(pid: str) -> str:
        return id_to_title.get(pid) or prev_title.get(pid) or pid

    if added:
        print(f"Added ({len(added)}):")
        for pid in sorted(added):
            print(f"  + {_label(pid)}")
    if changed:
        print(f"Changed ({len(changed)}):")
        for pid in sorted(changed):
            print(f"  ~ {_label(pid)}")
    if moved:
        print(f"Moved ({len(moved)}):")
        for pid in sorted(moved):
            old_dir = prev_state.pages[pid].dir
            new_dir = str(plan.dirs[pid])
            print(f"  > {_label(pid)}: {old_dir} -> {new_dir}")
    if deleted:
        print(f"Deleted ({len(deleted)}):")
        for pid in sorted(deleted):
            print(f"  - {_label(pid)}")
    if not any([added, changed, moved, deleted]):
        print("No changes.")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="conex",
        description="LLM-ready Confluence page tree exporter (v2)",
    )

    # Global connection/auth flags (v1 parity)
    parser.add_argument("--site-url", help="Confluence site URL")
    parser.add_argument("--base-url", help="Alias for --site-url")
    parser.add_argument("--api-base-url", help="Confluence API base URL override")
    parser.add_argument("--cloud-id", help="Atlassian cloud ID for gateway routing")
    parser.add_argument("--email", help="User email for authentication")
    parser.add_argument("--api-token", "--pat", dest="api_token", help="API token or PAT")
    parser.add_argument("--cookie", help="Browser cookie header for authentication")
    parser.add_argument(
        "--auth-type",
        choices=["basic_api_token", "scoped_api_token", "bearer_pat", "cookie"],
        help="Authentication mode override",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    sub = parser.add_subparsers(dest="command")

    # configure
    configure_p = sub.add_parser("configure", help="Interactive credential setup")
    configure_p.add_argument(
        "--local",
        nargs="?",
        const=".",
        default=None,
        metavar="DIR",
        help="Write DIR/.conex/config.json instead of the global config",
    )

    # spaces
    sub.add_parser("spaces", help="List accessible Confluence spaces")

    # tree
    tree_p = sub.add_parser("tree", help="Show page hierarchy for a space")
    tree_p.add_argument("space_key", help="Space key")

    # find
    find_p = sub.add_parser("find", help="Search pages by title substring")
    find_p.add_argument("space_key", help="Space key")
    find_p.add_argument("query", help="Title substring to search for")

    # export
    export_p = sub.add_parser("export", help="Export page tree as LLM-ready markdown")
    export_p.add_argument("space_key", help="Space key")
    export_p.add_argument("-o", "--output", required=True, metavar="DIR", help="Output directory")
    export_p.add_argument("--path", metavar="P", help="Subtree path (e.g. /Engineering/ADRs)")
    export_p.add_argument("--no-children", action="store_true", help="Export only the subtree root")
    export_p.add_argument("--include-archived", action="store_true", help="Include archived pages")
    export_p.add_argument("--cached", action="store_true", help="Use cached snapshot; skip pull")
    export_p.add_argument("--include-html", action="store_true", help="Write raw HTML alongside markdown")
    export_p.add_argument("--no-media", action="store_true", help="Skip attachment download")
    export_p.add_argument("--no-drawio-render", action="store_true", help="Skip draw.io PNG rendering")
    export_p.add_argument("--no-git", action="store_true", help="Skip git versioning")
    export_p.add_argument(
        "--no-author-lookup",
        action="store_true",
        help="Skip Confluence user lookups",
    )

    # refresh
    refresh_p = sub.add_parser("refresh", help="Pull latest data without rebuilding the tree")
    refresh_p.add_argument("space_key", help="Space key")
    refresh_p.add_argument("-o", "--output", required=True, metavar="DIR", help="Output directory")

    # diff
    diff_p = sub.add_parser("diff", help="Report changes since last export")
    diff_p.add_argument("space_key", help="Space key")
    diff_p.add_argument("-o", "--output", required=True, metavar="DIR", help="Output directory")
    diff_p.add_argument("--path", metavar="P", help="Subtree path filter")
    diff_p.add_argument("--include-archived", action="store_true", help="Include archived pages")

    return parser


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point.

    All ConexError subclasses produce a clean one-line stderr message + exit 1.
    No traceback is printed for expected failures.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        match args.command:
            case "configure":
                _cmd_configure(args)
            case "spaces":
                _cmd_spaces(args)
            case "tree":
                _cmd_tree(args)
            case "find":
                _cmd_find(args)
            case "export":
                _cmd_export(args)
            case "refresh":
                _cmd_refresh(args)
            case "diff":
                _cmd_diff(args)
            case _:  # pragma: no cover
                parser.print_help()
                sys.exit(1)
    except SystemExit:
        raise
    except ConexError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # pragma: no cover
        print(f"Unexpected error: {exc}", file=sys.stderr)
        sys.exit(1)
