# confluence-export

[![CI](https://github.com/mkubicek/conex/actions/workflows/ci.yml/badge.svg)](https://github.com/mkubicek/conex/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/mkubicek/conex/branch/main/graph/badge.svg)](https://codecov.io/gh/mkubicek/conex)

Export Confluence Cloud spaces as LLM-ready markdown.

```bash
confluence-export export SPACEKEY -o ./output
```

```
output/
└── Space-Name/
    ├── Space-Name.md
    ├── .workspace/
    ├── Page-A/
    │   ├── Page-A.md
    │   ├── .workspace/
    │   ├── Child-Page/
    │   │   ├── Child-Page.md
    │   │   ├── .workspace/
    │   │   └── .media/
    │   │       ├── diagram.drawio
    │   │       ├── diagram.drawio.png
    │   │       └── screenshot.png
    │   └── Another-Child/
    │       └── Another-Child.md
    └── Page-B/
        ├── Page-B.md
        └── .media/
            └── report.pdf
```

Pages become markdown files. Attachments land in `.media/` folders next to their page. The folder hierarchy mirrors the Confluence page tree.

## Workspace

Each page directory includes a `.workspace/` folder where you can store preparation files (scripts, notes, aggregation data) that are useful when working with the exported content but should not go to Confluence. Workspace files persist across re-exports.

```bash
# Example: a script that summarizes a page's attachments
output/Space-Name/Page-A/.workspace/summarize.py

# Example: notes for an AI coding agent session preparing content
output/Space-Name/Page-A/.workspace/draft-notes.md
```

Both `.workspace/` and `.media/` use a dot-prefix to avoid name collisions with Confluence pages. Since each page title becomes a directory name, a page titled "workspace" or "media" would clash with a non-prefixed directory. The dot-prefix is safe because page titles are sanitized to only contain word characters, spaces, and hyphens, so no page can produce a directory name starting with a dot.

## Install

```bash
uv pip install -e .
```

## Setup

```bash
confluence-export configure
```

Stores your base URL and API token to `~/.config/confluence-export/config.json`. You can also use env vars (`CONFLUENCE_BASE_URL`, `CONFLUENCE_EMAIL`, `CONFLUENCE_API_TOKEN`) or CLI flags (`--base-url`, `--email`, `--api-token`).

If you don't have an API token, you can authenticate with a browser cookie instead. Copy the `Cookie` header from DevTools (F12 > Network tab > any `/wiki/` request) and pass it with `--cookie`:

```bash
confluence-export --base-url https://example.atlassian.net --cookie 'tenant.session.token=...' export SPACEKEY -o ./output
```

If no token or cookie is provided, the tool will prompt interactively. Browser credentials are used for the current run only and never saved.

## Required permissions

Basic Auth (email + API token) and unscoped Personal Access Tokens use the underlying user's full permissions — no further setup needed. If you provision a [scoped API token](https://developer.atlassian.com/cloud/confluence/scopes-for-oauth-2-3LO-and-forge-apps/) or use OAuth 2.0 / Forge, grant these five granular read scopes:

```
read:space:confluence
read:page:confluence
read:folder:confluence
read:attachment:confluence
read:user:confluence
```

`read:user:confluence` is only needed to render `@mentions` and `profile` macros with display names instead of opaque Atlassian account IDs in the exported markdown. If your token can't grant it, pass `--no-author-lookup` to skip user resolution; mentions fall back to the raw account ID. The other four scopes are required.

For classic OAuth 2.0 (3LO) tokens, the equivalent set is `read:confluence-space.summary`, `read:confluence-content.all`, `readonly:content.attachment:confluence`, and `read:confluence-user`.

Scoped tokens must be addressed via the OAuth gateway URL (`https://api.atlassian.com/ex/confluence/{cloudId}/...`) rather than the site URL. The tool detects this automatically: on first use, it looks up your site's cloud ID from the unauthenticated `/_edge/tenant_info` endpoint, rewrites `base_url` in the saved config, and routes all subsequent requests through the gateway. No manual configuration needed — keep `base_url` set to your site URL.

## Commands

```bash
confluence-export spaces                            # list accessible spaces
confluence-export tree SPACEKEY                     # show page hierarchy
confluence-export find SPACEKEY "query"             # search pages by title
confluence-export export SPACEKEY -o ./output       # export full space
confluence-export export SPACEKEY --path /Sub/Tree  # export a subtree
confluence-export export SPACEKEY --no-media        # skip attachments
confluence-export export SPACEKEY --no-git          # skip git versioning
confluence-export export SPACEKEY --no-author-lookup # skip Confluence user lookup
confluence-export diff SPACEKEY ./output            # compare export vs. live
confluence-export refresh SPACEKEY                  # force-refresh cache
```

## Git Versioning

Exports are automatically versioned with git. After each export, only Confluence-sourced files are committed. If you've made local edits to previously exported files, those are captured in a separate commit first. Locally created files are never auto-committed.

Git versioning is enabled by default and requires git to be installed. If git is not available, the export proceeds normally with a warning. Disable with `--no-git`.
