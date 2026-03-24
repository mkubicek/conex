"""CLI entry point with subcommands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from confluence_export.cache import CacheStore
from confluence_export.client import ConfluenceClient
from confluence_export.config import Config, config_path, load_config, save_config
from confluence_export.diff import compute_diff, format_diff, scan_export_dir
from confluence_export.tree import (
    build_tree,
    collect_subtree,
    find_node_by_path,
    find_pages,
    format_tree,
    page_path,
)
from confluence_export.exporter import Exporter
from confluence_export.types import Space


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="confluence-export",
        description="LLM-ready Confluence page tree exporter",
    )
    parser.add_argument("--base-url", help="Confluence base URL")
    parser.add_argument("--email", help="User email for authentication")
    parser.add_argument("--api-token", "--pat", help="API token or PAT for authentication")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    sub = parser.add_subparsers(dest="command")

    # configure
    sub.add_parser("configure", help="Interactive credential setup")

    # spaces
    sub.add_parser("spaces", help="List accessible spaces")

    # tree
    tree_p = sub.add_parser("tree", help="Show page hierarchy")
    tree_p.add_argument("space_key", help="Space key")

    # find
    find_p = sub.add_parser("find", help="Search pages by title")
    find_p.add_argument("space_key", help="Space key")
    find_p.add_argument("query", help="Search query")

    # export
    export_p = sub.add_parser("export", help="Export page tree as LLM-ready markdown")
    export_p.add_argument("space_key", help="Space key")
    export_p.add_argument("--path", help="Subtree path (e.g. /Engineering/ADRs)")
    export_p.add_argument("-o", "--output", default="./output", help="Output directory")
    export_p.add_argument("--no-children", action="store_true", help="Single page only")
    export_p.add_argument("--no-media", action="store_true", help="Skip attachment download")
    export_p.add_argument("--no-drawio-render", action="store_true", help="Skip draw.io to PNG")
    export_p.add_argument("--refresh", action="store_true", help="Force-refresh cache before export")
    export_p.add_argument("--include-html", action="store_true", help="Save raw HTML alongside markdown")
    export_p.add_argument("--include-archived", action="store_true", help="Include archived pages (skipped by default)")

    # diff
    diff_p = sub.add_parser("diff", help="Compare export dir against current Confluence state")
    diff_p.add_argument("space_key", help="Space key")
    diff_p.add_argument("export_dir", help="Path to existing export directory")
    diff_p.add_argument("--path", help="Subtree path filter (e.g. /Engineering/ADRs)")
    diff_p.add_argument("--include-archived", action="store_true", help="Include archived pages")

    # refresh
    refresh_p = sub.add_parser("refresh", help="Force-refresh cache for a space")
    refresh_p.add_argument("space_key", help="Space key")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "configure":
        _cmd_configure()
        return

    # All other commands need config + client
    try:
        config = load_config(
            base_url=args.base_url,
            email=args.email,
            api_token=args.api_token,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(f"\nRun 'confluence-export configure' to set up credentials.", file=sys.stderr)
        sys.exit(1)

    client = ConfluenceClient(config, verbose=args.verbose)
    cache = CacheStore()

    match args.command:
        case "spaces":
            _cmd_spaces(client)
        case "tree":
            _cmd_tree(client, cache, args.space_key)
        case "find":
            _cmd_find(client, cache, args.space_key, args.query)
        case "export":
            _cmd_export(
                client,
                cache,
                config.base_url,
                space_key=args.space_key,
                path_filter=args.path,
                output_dir=args.output,
                no_children=args.no_children,
                no_media=args.no_media,
                no_drawio_render=args.no_drawio_render,
                force_refresh=args.refresh,
                debug=args.include_html,
                include_archived=args.include_archived,
            )
        case "diff":
            _cmd_diff(
                client,
                cache,
                space_key=args.space_key,
                export_dir=args.export_dir,
                path_filter=args.path,
                include_archived=args.include_archived,
            )
        case "refresh":
            _cmd_refresh(client, cache, args.space_key)


# -- command implementations -------------------------------------------------


def _cmd_configure() -> None:
    """Interactive credential setup."""
    print("Confluence Export - Configuration")
    print(f"Config file: {config_path()}\n")

    # Load existing config for defaults
    existing: dict = {}
    cp = config_path()
    if cp.exists():
        import json

        with open(cp) as f:
            existing = json.load(f)

    base_url = input(f"Base URL [{existing.get('base_url', '')}]: ").strip()
    if not base_url:
        base_url = existing.get("base_url", "")

    email = input(f"Email (leave empty for PAT Bearer auth) [{existing.get('email', '')}]: ").strip()
    if not email:
        email = existing.get("email", "")

    # Mask existing token
    existing_token = existing.get("api_token", "")
    masked = f"{existing_token[:4]}...{existing_token[-4:]}" if len(existing_token) > 8 else ""
    token_label = "API Token" if email else "Personal Access Token (PAT)"
    token = input(f"{token_label} [{masked}]: ").strip()
    if not token:
        token = existing_token

    if not base_url or not token:
        print("Error: base_url and api_token/PAT are required.", file=sys.stderr)
        sys.exit(1)

    if email:
        print("\nAuth mode: Basic Auth (email + API token)")
    else:
        print("\nAuth mode: Bearer token (PAT)")

    cfg = Config(base_url=base_url.rstrip("/"), email=email, api_token=token)
    path = save_config(cfg)
    print(f"\nConfiguration saved to {path}")


def _cmd_spaces(client: ConfluenceClient) -> None:
    """List accessible spaces."""
    spaces = client.get_spaces()
    if not spaces:
        print("No spaces found.")
        return

    # Column widths
    key_w = max(len(s.key) for s in spaces)
    name_w = max(len(s.name) for s in spaces)

    print(f"{'KEY':<{key_w}}  {'NAME':<{name_w}}  TYPE      STATUS")
    print(f"{'-' * key_w}  {'-' * name_w}  --------  ------")
    for s in spaces:
        print(f"{s.key:<{key_w}}  {s.name:<{name_w}}  {s.type:<8}  {s.status}")


def _cmd_tree(client: ConfluenceClient, cache: CacheStore, space_key: str) -> None:
    """Show page hierarchy."""
    space = _resolve_space(client, space_key)
    cs = cache.ensure_loaded(client, space)

    roots = build_tree(cs.pages)
    tree_str = format_tree(roots)
    print(tree_str)
    print(f"\n{len(cs.pages)} pages (cached {cs.updated_at})")


def _cmd_find(
    client: ConfluenceClient, cache: CacheStore, space_key: str, query: str
) -> None:
    """Search pages by title."""
    space = _resolve_space(client, space_key)
    cs = cache.ensure_loaded(client, space)

    results = find_pages(cs.pages, query)
    if not results:
        print(f"No pages matching '{query}'.")
        return

    id_w = max(len(p.id) for p in results)
    for p in results:
        path = page_path(cs.pages, p.id)
        print(f"{p.id:<{id_w}}  {path}")


def _cmd_export(
    client: ConfluenceClient,
    cache: CacheStore,
    base_url: str,
    *,
    space_key: str,
    path_filter: str | None,
    output_dir: str,
    no_children: bool,
    no_media: bool,
    no_drawio_render: bool,
    force_refresh: bool,
    debug: bool = False,
    include_archived: bool = False,
) -> None:
    """Export page tree as LLM-ready markdown."""
    space = _resolve_space(client, space_key)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    exporter = Exporter(
        client,
        cache,
        base_url,
        download_media=not no_media,
        render_drawio=not no_drawio_render,
        debug=debug,
    )

    count = exporter.export_space(
        space,
        out,
        path_filter=path_filter,
        no_children=no_children,
        force_refresh=force_refresh,
        include_archived=include_archived,
    )

    print(f"\nExported {count} page(s) to {out.resolve()}")


def _cmd_diff(
    client: ConfluenceClient,
    cache: CacheStore,
    *,
    space_key: str,
    export_dir: str,
    path_filter: str | None,
    include_archived: bool,
) -> None:
    """Compare an export directory against current Confluence state."""
    export_path = Path(export_dir)
    if not export_path.is_dir():
        print(f"Error: '{export_dir}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    # Scan local export
    exported = scan_export_dir(export_path, space_key)
    print(f"Scanned {len(exported)} page(s) from export directory.", file=sys.stderr)

    # Always refresh — diff against stale cache is useless
    cs = cache.load(space_key)
    if cs is not None:
        space = cs.space
    else:
        space = _resolve_space(client, space_key)
    cs = cache.refresh(client, space)

    # Filter API pages
    api_pages = [p for p in cs.pages if p.status != "folder"]

    roots = build_tree(cs.pages)
    if not include_archived:
        roots = [r for r in roots if r.page.id != "__archived__"]
        # Rebuild page list from filtered tree
        filtered_ids: set[str] = set()
        for root in roots:
            for node in collect_subtree(root):
                filtered_ids.add(node.page.id)
        api_pages = [p for p in api_pages if p.id in filtered_ids]

    # Apply subtree filter
    if path_filter:
        node = find_node_by_path(roots, path_filter)
        if not node:
            print(f"Error: path '{path_filter}' not found in space {space_key}", file=sys.stderr)
            sys.exit(1)
        subtree_ids = {n.page.id for n in collect_subtree(node)}
        api_pages = [p for p in api_pages if p.id in subtree_ids]

    # Compute and print diff
    result = compute_diff(exported, api_pages)
    print(format_diff(result, cs.pages))


def _cmd_refresh(client: ConfluenceClient, cache: CacheStore, space_key: str) -> None:
    """Force-refresh cache for a space."""
    space = _resolve_space(client, space_key)
    cs = cache.refresh(client, space)
    print(f"Cache refreshed: {len(cs.pages)} pages (updated {cs.updated_at})")


# -- helpers -----------------------------------------------------------------


def _resolve_space(client: ConfluenceClient, space_key: str) -> Space:
    """Find a space by key (case-insensitive)."""
    spaces = client.get_spaces()
    key_upper = space_key.upper()
    for s in spaces:
        if s.key.upper() == key_upper:
            return s
    print(f"Error: space '{space_key}' not found.", file=sys.stderr)
    sys.exit(1)
