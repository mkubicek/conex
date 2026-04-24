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

# Example: notes for a Claude Code session preparing content
output/Space-Name/Page-A/.workspace/draft-notes.md
```

Both `.workspace/` and `.media/` use a dot-prefix to avoid collisions with Confluence pages that might be titled "workspace" or "media".

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

## Commands

```bash
confluence-export spaces                            # list accessible spaces
confluence-export tree SPACEKEY                     # show page hierarchy
confluence-export find SPACEKEY "query"             # search pages by title
confluence-export export SPACEKEY -o ./output       # export full space
confluence-export export SPACEKEY --path /Sub/Tree  # export a subtree
confluence-export export SPACEKEY --no-media        # skip attachments
confluence-export export SPACEKEY --no-git          # skip git versioning
confluence-export diff SPACEKEY ./output            # compare export vs. live
confluence-export refresh SPACEKEY                  # force-refresh cache
```

## Git Versioning

Exports are automatically versioned with git. After each export, only Confluence-sourced files are committed. If you've made local edits to previously exported files, those are captured in a separate commit first. Locally created files are never auto-committed.

Git versioning is enabled by default and requires git to be installed. If git is not available, the export proceeds normally with a warning. Disable with `--no-git`.
