"""Run the real export CLI against an in-memory Confluence space, offline."""

import argparse
from unittest.mock import patch

from conex.cli import main
from conex.config import Dialect, ResolvedConfig
from tests_v2.fake_api import FakeConfluenceAPI


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--fail-download", action="store_true", help="Simulate an attachment outage")
    parser.add_argument("export_args", nargs=argparse.REMAINDER, metavar="SPACE [EXPORT OPTIONS]")
    args = parser.parse_args()

    api = FakeConfluenceAPI(space_key="DEMO", space_name="Demo")
    api.add_page("p1", "Runbook", body="<p>Fixture runbook</p>")
    api.add_attachment("a1", "checklist.txt", "p1", content=b"Check the export report.\n")
    if args.fail_download:
        api.fail_download_for("a1")

    config = ResolvedConfig(site_url="https://fixture.invalid", api_base_url="",
                            auth_headers={}, dialect=Dialect.CLOUD_V2, source_description="fixture")
    with patch("conex.cli.resolve_config", return_value=config), patch("conex.api.make_api", return_value=api):
        main(["export", *args.export_args, "--no-git"])


if __name__ == "__main__":
    run()
