#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from gp_sql_analyzer.benchmarks import fetch_tpcds


def main() -> int:
    parser = argparse.ArgumentParser(description="Download a pinned TPC-DS SQL corpus")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--all", action="store_true", help="download all 99 queries")
    parser.add_argument("--no-schema", action="store_true")
    args = parser.parse_args()
    manifest = fetch_tpcds(
        args.destination,
        full=args.all,
        include_schema=not args.no_schema,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
