"""CLI entry point with subcommands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

from confluence_export.cache import CacheStore
from confluence_export.client import AuthenticationError, ConfluenceClient
from confluence_export.config import (
    ApiDialect,
    AuthConfig,
    AuthMode,
    ConnectionProfile,
    ConnectionProfileError,
    config_path,
    gateway_url,
    is_gateway_url,
    is_scoped_token,
    load_connection_profile,
    local_config_path,
    resolve_cloud_id,
    save_connection_config,
)
from confluence_export.diff import compute_diff, format_diff, scan_export_dir
from confluence_export.tree import (
    build_tree,
    collect_subtree,
    find_node_by_path,
    find_pages,
    format_tree,
    page_path,
)
from confluence_export.exporter import Exporter, is_full_export
from confluence_export.types import Space


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


def _exit_on_auth_error(fn) -> object:
    """Call fn(); on auth error, fail clearly without prompting."""
    try:
        return fn()
    except Exception as exc:
        status = _is_auth_error(exc)
        if status is None:
            raise
        print(
            "Authentication failed and no interactive prompt is available.",
            file=sys.stderr,
        )
        print(f"Reason: HTTP {status} from Confluence.", file=sys.stderr)
        print(
            "Next step: run `confluence-export configure` or provide explicit credentials.",
            file=sys.stderr,
        )
        sys.exit(1)


def _config_start_dir(args: argparse.Namespace) -> Path:
    if args.command == "export":
        return Path(args.output)
    if args.command == "diff":
        return Path(args.export_dir)
    return Path.cwd()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="confluence-export",
        description="LLM-ready Confluence page tree exporter",
    )
    parser.add_argument("--site-url", help="Confluence site URL")
    parser.add_argument("--base-url", help="Alias for --site-url")
    parser.add_argument("--api-base-url", help="Actual Confluence API base URL")
    parser.add_argument("--cloud-id", help="Atlassian cloud ID for OAuth gateway routing")
    parser.add_argument(
        "--auth-type",
        choices=[mode.value for mode in AuthMode],
        help="Authentication mode",
    )
    parser.add_argument("--email", help="User email for authentication")
    parser.add_argument("--api-token", "--pat", help="API token or PAT for authentication")
    parser.add_argument("--cookie", help="Browser cookie string for authentication")
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
    export_p.add_argument(
        "--no-author-lookup",
        action="store_true",
        help="Skip Confluence user lookups (for tokens without read:user:confluence)",
    )

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
        _cmd_configure(local_dir=args.local)
        return

    # All other commands need a resolved profile + client.
    try:
        profile = load_connection_profile(
            site_url=args.site_url,
            base_url=args.base_url,
            api_base_url=args.api_base_url,
            cloud_id=args.cloud_id,
            auth_type=args.auth_type,
            email=args.email,
            api_token=args.api_token,
            cookie=args.cookie,
            start_dir=_config_start_dir(args),
            interactive=sys.stdin.isatty(),
        )
    except ConnectionProfileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(f"\nRun 'confluence-export configure' to set up credentials.", file=sys.stderr)
        sys.exit(1)

    client = ConfluenceClient(profile, verbose=args.verbose)

    match args.command:
        case "spaces":
            _exit_on_auth_error(lambda: _cmd_spaces(client))
        case "tree":
            _cmd_tree(client, CacheStore(), profile, args.space_key)
        case "find":
            _cmd_find(client, CacheStore(), profile, args.space_key, args.query)
        case "export":
            _cmd_export(
                client,
                profile,
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
                no_author_lookup=args.no_author_lookup,
            )
        case "diff":
            _cmd_diff(
                client,
                CacheStore(),
                profile,
                space_key=args.space_key,
                export_dir=args.export_dir,
                path_filter=args.path,
                include_archived=args.include_archived,
            )
        case "refresh":
            _cmd_refresh(client, CacheStore(), profile, args.space_key)


# -- command implementations -------------------------------------------------


def _mask_secret(value: str) -> str:
    return f"{value[:4]}...{value[-4:]}" if len(value) > 8 else ""


def _auth_label(mode: AuthMode) -> str:
    match mode:
        case AuthMode.BASIC_API_TOKEN:
            return "Basic API token"
        case AuthMode.SCOPED_API_TOKEN:
            return "scoped API token"
        case AuthMode.BEARER_PAT:
            return "Bearer token (PAT)"
        case AuthMode.COOKIE:
            return "cookie session"
        case _:
            raise AssertionError(f"Unhandled auth mode: {mode}")


def _api_mode_label(dialect: ApiDialect) -> str:
    match dialect:
        case ApiDialect.CLOUD_V2:
            return "Confluence REST v2"
        case ApiDialect.GATEWAY_V2:
            return "OAuth gateway"
        case ApiDialect.COOKIE_V1:
            return "Confluence REST v1 compatibility"
        case _:
            raise AssertionError(f"Unhandled API dialect: {dialect}")


def _read_existing_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _existing_config_defaults(path: Path) -> tuple[str, str, str]:
    existing = _read_existing_config(path)
    if existing.get("version") == 2:
        auth = existing.get("auth") or {}
        secret = auth.get("token") or auth.get("cookie_header") or ""
        return (
            str(existing.get("site_url", "") or ""),
            str(auth.get("email", "") or ""),
            str(secret or ""),
        )

    base_url = str(existing.get("base_url", "") or "")
    if is_gateway_url(base_url):
        print(
            "Existing config contains an OAuth gateway URL. Enter the Confluence site URL.",
            file=sys.stderr,
        )
        base_url = ""
    return (
        base_url,
        str(existing.get("email", "") or ""),
        str(existing.get("api_token", "") or ""),
    )


def _looks_like_cookie_header(secret: str) -> bool:
    if "=" not in secret:
        return False
    name, _, value = secret.partition("=")
    if not name.strip() or not value.strip():
        return False
    name_lower = name.strip().lower()
    return ";" in secret or "." in name_lower or name_lower in {
        "session",
        "jsessionid",
        "cloud.session.token",
        "tenant.session.token",
    }


def _cmd_configure(local_dir: str | None = None) -> None:
    """Interactive credential setup."""
    target = local_config_path(local_dir) if local_dir is not None else config_path()
    print("Confluence Export - Configuration")
    print(f"Config file: {target}\n")

    existing_site_url, existing_email, existing_secret = _existing_config_defaults(target)

    site_url = input(f"Site URL [{existing_site_url}]: ").strip()
    if not site_url:
        site_url = existing_site_url

    email = input(
        f"Email (leave empty for PAT Bearer auth or cookie session) [{existing_email}]: "
    ).strip()
    if not email:
        email = existing_email

    masked = _mask_secret(existing_secret)
    secret_label = "API token" if email else "PAT or Cookie header"
    secret = input(f"{secret_label} [{masked}]: ").strip()
    if not secret:
        secret = existing_secret

    if not site_url:
        print("Error: site_url is required.", file=sys.stderr)
        sys.exit(1)
    if is_gateway_url(site_url):
        print(
            "Error: site_url must be the Confluence site URL, not the OAuth gateway URL.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not email and is_scoped_token(secret):
        print("Error: scoped API tokens require an email address.", file=sys.stderr)
        sys.exit(1)
    if not email and _looks_like_cookie_header(secret):
        auth = AuthConfig(type=AuthMode.COOKIE, cookie_header=secret)
    elif not email:
        auth = AuthConfig(type=AuthMode.BEARER_PAT, token=secret)
    elif is_scoped_token(secret):
        auth = AuthConfig(type=AuthMode.SCOPED_API_TOKEN, email=email, token=secret)
    else:
        auth = AuthConfig(type=AuthMode.BASIC_API_TOKEN, email=email, token=secret)

    cloud_id = None
    api_base_url = ""
    if auth.type is AuthMode.SCOPED_API_TOKEN and not is_gateway_url(site_url):
        cloud_id = resolve_cloud_id(site_url)
        if cloud_id:
            api_base_url = gateway_url(cloud_id)

    print(f"\nAuth mode: {_auth_label(auth.type)}")
    path = save_connection_config(
        site_url=site_url.rstrip("/"),
        auth=auth,
        path=target,
        cloud_id=cloud_id,
        api_base_url=api_base_url,
    )
    print(f"\nConfiguration saved to {path}")
    print(
        "\nIf this is a scoped API token, grant these five read scopes:\n"
        "  read:space:confluence\n"
        "  read:page:confluence\n"
        "  read:folder:confluence\n"
        "  read:attachment:confluence\n"
        "  read:user:confluence  (omit and pass --no-author-lookup to skip)\n"
        "Basic Auth and unscoped tokens already have full access."
    )


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


def _cmd_tree(
    client: ConfluenceClient, cache: CacheStore, profile: ConnectionProfile, space_key: str
) -> None:
    """Show page hierarchy."""
    space = _exit_on_auth_error(lambda: _resolve_space(client, space_key))
    cs = cache.ensure_loaded(client, space)

    roots = build_tree(cs.pages)
    tree_str = format_tree(roots)
    print(tree_str)
    print(f"\n{len(cs.pages)} pages (cached {cs.updated_at})")


def _cmd_find(
    client: ConfluenceClient,
    cache: CacheStore,
    profile: ConnectionProfile,
    space_key: str,
    query: str,
) -> None:
    """Search pages by title."""
    space = _exit_on_auth_error(lambda: _resolve_space(client, space_key))
    cs = cache.ensure_loaded(client, space)

    results = find_pages(cs.pages, query)
    if not results:
        print(f"No pages matching '{query}'.")
        return

    id_w = max(len(p.id) for p in results)
    for p in results:
        path = page_path(cs.pages, p.id)
        print(f"{p.id:<{id_w}}  {path}")


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _check_output_writable(output_dir: str) -> None:
    out = Path(output_dir).expanduser().resolve()
    if out.exists() and not out.is_dir():
        raise RuntimeError(f"{out} exists and is not a directory")
    parent = _nearest_existing_parent(out)
    if not parent.exists() or not os.access(parent, os.W_OK | os.X_OK):
        raise RuntimeError(f"{parent} is not writable")


def _preflight_error_message(exc: Exception) -> str:
    if isinstance(exc, AuthenticationError):
        return f"HTTP {exc.status_code} from {exc.url}"
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    return str(exc) or exc.__class__.__name__


def _ensure_gateway_route(profile: ConnectionProfile) -> None:
    if not profile.cloud_id or not is_gateway_url(profile.api_base_url):
        raise RuntimeError("cloud ID or gateway URL is missing")


def _require_space(client: ConfluenceClient, space_key: str) -> Space:
    space = _find_space(client, space_key)
    if space is None:
        raise RuntimeError(f"space '{space_key}' not found")
    return space


def _run_export_preflight(
    client: ConfluenceClient,
    profile: ConnectionProfile,
    *,
    space_key: str,
    output_dir: str,
    no_media: bool,
    no_git: bool,
) -> Space:
    print(f"Using config: {profile.config_source}", file=sys.stderr)
    print(f"Auth: {_auth_label(profile.auth_mode)}", file=sys.stderr)
    print(f"API mode: {_api_mode_label(profile.api_dialect)}", file=sys.stderr)
    print(f"Site: {profile.site_url}", file=sys.stderr)
    if profile.cloud_id:
        print(f"Cloud ID: {profile.cloud_id}", file=sys.stderr)
    print(f"Output: {Path(output_dir).expanduser().resolve()}", file=sys.stderr)
    print("\nPreflight:", file=sys.stderr)

    failures: list[str] = []

    def step(label: str, fn):
        try:
            result = fn()
            print(f"  ✓ {label}", file=sys.stderr)
            return result
        except Exception as exc:
            reason = _preflight_error_message(exc)
            failures.append(f"{label}: {reason}")
            print(f"  ✗ {label}", file=sys.stderr)
            return None

    step("authenticated", client.verify_auth)

    if profile.api_dialect is ApiDialect.GATEWAY_V2:
        step("gateway route resolved", lambda: _ensure_gateway_route(profile))

    space = step(
        "space resolved",
        lambda: _require_space(client, space_key),
    )

    sample_page_id = None
    if space is not None:
        sample_page_id = step("page listing available", lambda: client.probe_page_listing(space))
    else:
        failures.append("page listing available: skipped because space did not resolve")
        print("  ✗ page listing available", file=sys.stderr)

    if not no_media:
        if sample_page_id:
            step("attachment listing available", lambda: client.probe_attachment_listing(sample_page_id))
        else:
            print("  ✓ attachment listing skipped (no pages)", file=sys.stderr)

    step("output directory writable", lambda: _check_output_writable(output_dir))

    if no_git:
        print("  ✓ git skipped", file=sys.stderr)
    else:
        from confluence_export.git import git_available

        if git_available():
            print("  ✓ git available", file=sys.stderr)
        else:
            print("  ! git unavailable; export will continue without git", file=sys.stderr)

    if failures:
        print("\nPreflight failed; export did not start.", file=sys.stderr)
        for failure in failures:
            print(f"Reason: {failure}", file=sys.stderr)
        print(
            "Next step: run `confluence-export configure` or provide --cloud-id / --api-base-url.",
            file=sys.stderr,
        )
        sys.exit(1)

    return space


def _cmd_export(
    client: ConfluenceClient,
    profile: ConnectionProfile,
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
    no_author_lookup: bool = False,
) -> None:
    """Export page tree as LLM-ready markdown."""
    space = _run_export_preflight(
        client,
        profile,
        space_key=space_key,
        output_dir=output_dir,
        no_media=no_media,
        no_git=no_git,
    )

    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cache = CacheStore()

    # TODO(migration): Remove after 2027-01-01
    from confluence_export.media import migrate_media_dirs

    renamed = migrate_media_dirs(out)
    if renamed:
        print(f"Migrated {len(renamed)} media/ directories to .media/", file=sys.stderr)

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
        profile.site_url,
        download_media=not no_media,
        render_drawio=not no_drawio_render,
        debug=debug,
        skip_author_lookup=no_author_lookup,
    )

    result = exporter.export_space(
        space,
        out,
        path_filter=path_filter,
        no_children=no_children,
        force_refresh=force_refresh,
        include_archived=include_archived,
    )

    # Git: post-export. A partial export (subtree / single page / no-children)
    # writes only part of the tree, so it must not prune the rest of the repo:
    # commit only adds in that mode (is_full=False). Shared predicate with the
    # exporter (is_full_export) so the relocation gate and the prune gate agree.
    is_full = is_full_export(path_filter, no_children)
    if use_git and result.written_files:
        from confluence_export.git import commit_export

        commit_export(
            out,
            result.written_files,
            space_key,
            is_full=is_full,
            protected_dirs=result.skipped_paths,
            preserve_media=no_media,
        )

    print(f"\nExported {result.count} page(s) to {out.resolve()}")


def _cmd_diff(
    client: ConfluenceClient,
    cache: CacheStore,
    profile: ConnectionProfile,
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
        space = _exit_on_auth_error(lambda: _resolve_space(client, space_key))
    cs = _exit_on_auth_error(
        lambda: cache.refresh(client, space, include_archived=include_archived)
    )

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


def _cmd_refresh(
    client: ConfluenceClient, cache: CacheStore, profile: ConnectionProfile, space_key: str
) -> None:
    """Force-refresh cache for a space."""
    space = _exit_on_auth_error(lambda: _resolve_space(client, space_key))
    cs = cache.refresh(client, space)
    print(f"Cache refreshed: {len(cs.pages)} pages (updated {cs.updated_at})")


# -- helpers -----------------------------------------------------------------


def _find_space(client: ConfluenceClient, space_key: str, *, announce: bool = True) -> Space | None:
    """Find a space by key (case-insensitive)."""
    if announce:
        print(f"Resolving space '{space_key}'...", file=sys.stderr, flush=True)
    # Try server-side filter first — O(1) round trip vs. paging every space.
    space = client.get_space_by_key(space_key)
    if space is not None:
        return space
    # Fall back to a full list so that callers who typed a different case
    # than the canonical key still resolve.
    key_upper = space_key.upper()
    for s in client.get_spaces():
        if s.key.upper() == key_upper:
            return s
    return None


def _resolve_space(client: ConfluenceClient, space_key: str) -> Space:
    """Find a space by key (case-insensitive), exiting on failure."""
    space = _find_space(client, space_key)
    if space is not None:
        return space
    print(f"Error: space '{space_key}' not found.", file=sys.stderr)
    sys.exit(1)
