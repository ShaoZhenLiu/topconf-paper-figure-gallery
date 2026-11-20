#!/usr/bin/env python3
"""Maintain the local extension while using the original gallery frontend."""
import argparse
import json
from pathlib import Path

from gallery_extension.catalog import CACHE, Catalog, VENUES


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    diagram = commands.add_parser("diagram-bank", help="import the DiagramBank release")
    diagram.add_argument("--metadata", type=Path, help="local copy of the release data.jsonl")
    diagram.add_argument("--archives", type=Path, default=CACHE / "archives")
    diagram.add_argument("--hub", default="https://huggingface.co", help="Hugging Face origin")
    diagram.add_argument("--workers", type=positive_int, default=4)
    diagram.add_argument("--max-archives", type=positive_int)
    diagram.add_argument("--keep-archives", action="store_true")
    diagram.add_argument("--local-only", action="store_true", help="only use archives already on disk")
    diagram.add_argument("--plan", action="store_true", help="list required archives")

    conferences = commands.add_parser("conferences", help="extract from accepted-paper rosters")
    conferences.add_argument("--venues", default=",".join(VENUES[:-1]), help="comma-separated venues")
    conferences.add_argument("--years", required=True, help="year, comma-separated years, or inclusive start:end")
    conferences.add_argument("--limit-per-edition", type=positive_int)
    conferences.add_argument("--workers", type=positive_int, default=6)
    conferences.add_argument("--arxiv-metadata", type=Path, help="arXiv metadata JSONL, gzip, or zip snapshot")
    conferences.add_argument("--arxiv-lookup", action=argparse.BooleanOptionalAction, default=True,
                             help="look up remaining titles via the arXiv API (default: on)")
    conferences.add_argument("--browser", type=Path, help="Chrome or Chromium executable for multi-panel HTML")
    conferences.add_argument("--refresh-rosters", action="store_true")
    conferences.add_argument("--plan", action="store_true", help="fetch and count rosters only")
    commands.add_parser("status", help="report stored images and pending attempts")

    args = parser.parse_args()
    catalog = Catalog()
    try:
        if args.command == "diagram-bank":
            from gallery_extension import diagrambank
            return diagrambank.run(args, catalog)
        if args.command == "conferences":
            from gallery_extension import conferences
            return conferences.run(args, catalog)
        print(json.dumps(catalog.status(), indent=2))
        return 0
    finally:
        catalog.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
