"""CLI entry point with subcommands."""

from __future__ import annotations

import argparse
import os
import sys
import termios
from pathlib import Path

import requests

from confluence_export.cache import CacheStore
from confluence_export.client import AuthenticationError, ConfluenceClient
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


def _read_hidden_line(prompt: str) -> str:
    """Read a line from the terminal with echo disabled, bypassing the TTY line buffer limit."""
    print(prompt, end="", file=sys.stderr, flush=True)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        new = termios.tcgetattr(fd)
        # Disable echo and canonical mode (removes ~1024 byte line buffer limit)
        new[3] = new[3] & ~(termios.ECHO | termios.ICANON)
        new[6][termios.VMIN] = 1
        new[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, new)

        chars = []
        while True:
            ch = os.read(fd, 1)
            if ch in (b"\r", b"\n", b""):
                break
            if ch == b"\x03":  # Ctrl+C
                raise KeyboardInterrupt
            chars.append(ch)
        print(file=sys.stderr)  # newline after hidden input
        return b"".join(chars).decode("utf-8", errors="replace")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _prompt_browser_credentials(base_url: str, reason: str) -> tuple[str, str]:
    """Prompt for browser credentials. Returns (type, value) where type is 'bearer' or 'cookie'."""
    print(f"\n{reason}", file=sys.stderr)
    print(
        f"\nTo authenticate via browser session:\n"
        f"  1. Open {base_url} in your browser and log in\n"
        f"  2. Open DevTools (F12) -> Network tab\n"
        f"  3. Reload the page and click any API request to /wiki/\n"
        f"  4. Copy the 'Cookie' or 'Authorization' header value\n",
        file=sys.stderr,
    )
    try:
        value = _read_hidden_line("Paste cookie or token (input hidden): ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        sys.exit(1)

    if not value:
        print("No credentials provided.", file=sys.stderr)
        sys.exit(1)

    # Strip accidental "Bearer " prefix
    if value.lower().startswith("bearer "):
        return ("bearer", value[len("bearer "):])

    # Cookies: contain "=" (key=value), optionally with ";" separators
    # Bearer tokens are opaque strings without "="
    if "=" in value:
        return ("cookie", value)

    return ("bearer", value)


def _apply_browser_credentials(client: ConfluenceClient, base_url: str, reason: str) -> None:
    """Prompt for browser credentials, verify with a quick API call, apply to client."""
    cred_type, value = _prompt_browser_credentials(base_url, reason)
    if cred_type == "cookie":
        client.set_cookies(value)
    else:
        client.set_bearer_token(value)

    # Verify credentials with a minimal request (1 space, fast)
    print("Verifying credentials...", file=sys.stderr, flush=True)
    try:
        client._get("/wiki/api/v2/spaces", {"limit": "1"})
    except AuthenticationError as exc:
        print(f"Authentication failed (HTTP {exc.status_code}).", file=sys.stderr)
        sys.exit(1)
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        print(f"Authentication failed (HTTP {status}).", file=sys.stderr)
        sys.exit(1)
    print("Authenticated.", file=sys.stderr, flush=True)


def _is_auth_error(exc: Exception) -> int | None:
    """Return the HTTP status code if exc looks like an auth failure, else None.

    Confluence Cloud v2 API returns 404 (not 401/403) when credentials belong
    to a different Atlassian instance, so we treat 404 as a potential auth
    error in this context.
    """
    if isinstance(exc, AuthenticationError):
        return exc.status_code
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        if exc.response.status_code == 404:
            return 404
    return None


def _with_auth_fallback(fn, client: ConfluenceClient, config) -> object:
    """Call fn(); on auth error, prompt for a browser token and retry once."""
    try:
        return fn()
    except Exception as exc:
        status = _is_auth_error(exc)
        if status is None:
            raise
        reason = f"Authentication failed (HTTP {status}). Browser credentials are needed."
        _apply_browser_credentials(client, config.base_url, reason)
        try:
            return fn()
        except Exception as exc2:
            status2 = _is_auth_error(exc2)
            if status2 is None:
                raise
            print(
                f"\nBrowser token also rejected (HTTP {status2}). "
                f"The token may have expired — try again with a fresh one.",
                file=sys.stderr,
            )
            sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="confluence-export",
        description="LLM-ready Confluence page tree exporter",
    )
    parser.add_argument("--base-url", help="Confluence base URL")
    parser.add_argument("--email", help="User email for authentication")
    parser.add_argument("--api-token", "--pat", help="API token or PAT for authentication")
    parser.add_argument("--cookie", help="Browser cookie string for authentication")
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
    export_p.add_argument("--cached", action="store_true", help="Use cached data instead of refreshing from Confluence")
    export_p.add_argument("--include-html", action="store_true", help="Save raw HTML alongside markdown")
    export_p.add_argument("--include-archived", action="store_true", help="Include archived pages (skipped by default)")
    export_p.add_argument("--no-git", action="store_true", help="Skip automatic git versioning")

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

    if args.cookie:
        client.set_cookies(args.cookie)
        print("Using browser cookies. Verifying credentials...", file=sys.stderr, flush=True)
        try:
            client._get("/wiki/api/v2/spaces", {"limit": "1"})
        except Exception:
            print("Cookie authentication failed.", file=sys.stderr)
            sys.exit(1)
        print("Authenticated.", file=sys.stderr, flush=True)
    elif config.needs_token:
        _apply_browser_credentials(
            client, config.base_url, "No API token configured. Browser credentials are needed."
        )

    cache = CacheStore()

    match args.command:
        case "spaces":
            _with_auth_fallback(lambda: _cmd_spaces(client), client, config)
        case "tree":
            _cmd_tree(client, cache, config, args.space_key)
        case "find":
            _cmd_find(client, cache, config, args.space_key, args.query)
        case "export":
            _cmd_export(
                client,
                cache,
                config,
                space_key=args.space_key,
                path_filter=args.path,
                output_dir=args.output,
                no_children=args.no_children,
                no_media=args.no_media,
                no_drawio_render=args.no_drawio_render,
                force_refresh=not args.cached,
                debug=args.include_html,
                include_archived=args.include_archived,
                no_git=args.no_git,
            )
        case "diff":
            _cmd_diff(
                client,
                cache,
                config,
                space_key=args.space_key,
                export_dir=args.export_dir,
                path_filter=args.path,
                include_archived=args.include_archived,
            )
        case "refresh":
            _cmd_refresh(client, cache, config, args.space_key)


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

    if not base_url:
        print("Error: base_url is required.", file=sys.stderr)
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


def _cmd_tree(client: ConfluenceClient, cache: CacheStore, config: Config, space_key: str) -> None:
    """Show page hierarchy."""
    space = _with_auth_fallback(lambda: _resolve_space(client, space_key), client, config)
    cs = cache.ensure_loaded(client, space)

    roots = build_tree(cs.pages)
    tree_str = format_tree(roots)
    print(tree_str)
    print(f"\n{len(cs.pages)} pages (cached {cs.updated_at})")


def _cmd_find(
    client: ConfluenceClient, cache: CacheStore, config: Config, space_key: str, query: str
) -> None:
    """Search pages by title."""
    space = _with_auth_fallback(lambda: _resolve_space(client, space_key), client, config)
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
    config: Config,
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
    no_git: bool = False,
) -> None:
    """Export page tree as LLM-ready markdown."""
    space = _with_auth_fallback(lambda: _resolve_space(client, space_key), client, config)

    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    # Git: pre-export
    use_git = False
    if not no_git:
        from confluence_export.git import git_available, ensure_repo, commit_local_changes

        if git_available():
            if ensure_repo(out):
                use_git = True
                commit_local_changes(out)
        else:
            print("Warning: git not found, skipping git versioning.", file=sys.stderr)

    exporter = Exporter(
        client,
        cache,
        config.base_url,
        download_media=not no_media,
        render_drawio=not no_drawio_render,
        debug=debug,
    )

    result = exporter.export_space(
        space,
        out,
        path_filter=path_filter,
        no_children=no_children,
        force_refresh=force_refresh,
        include_archived=include_archived,
    )

    # Git: post-export
    if use_git and result.written_files:
        from confluence_export.git import commit_export

        commit_export(out, result.written_files, space_key)

    print(f"\nExported {result.count} page(s) to {out.resolve()}")


def _cmd_diff(
    client: ConfluenceClient,
    cache: CacheStore,
    config: Config,
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
        space = _with_auth_fallback(lambda: _resolve_space(client, space_key), client, config)
    cs = _with_auth_fallback(lambda: cache.refresh(client, space), client, config)

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


def _cmd_refresh(client: ConfluenceClient, cache: CacheStore, config: Config, space_key: str) -> None:
    """Force-refresh cache for a space."""
    space = _with_auth_fallback(lambda: _resolve_space(client, space_key), client, config)
    cs = cache.refresh(client, space)
    print(f"Cache refreshed: {len(cs.pages)} pages (updated {cs.updated_at})")


# -- helpers -----------------------------------------------------------------


def _resolve_space(client: ConfluenceClient, space_key: str) -> Space:
    """Find a space by key (case-insensitive)."""
    print(f"Resolving space '{space_key}'...", file=sys.stderr, flush=True)
    spaces = client.get_spaces()
    key_upper = space_key.upper()
    for s in spaces:
        if s.key.upper() == key_upper:
            return s
    print(f"Error: space '{space_key}' not found.", file=sys.stderr)
    sys.exit(1)
