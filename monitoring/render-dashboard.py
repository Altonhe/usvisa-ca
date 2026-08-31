#!/usr/bin/env python3
"""Stamp your New Relic account ID into the dashboard template.

The importer does *not* rewrite ``accountIds`` for you -- widgets keep whatever
account the JSON names, and if that is not yours every chart shows
"This widget was added from an account you don't have access to."

Usage::

    python monitoring/render-dashboard.py 1234567            # write ...-1234567.json
    python monitoring/render-dashboard.py 1234567 --stdout   # print, to pipe or copy

Find your account ID in the New Relic URL (``.../accounts/<id>/...`` or
``?account=<id>``), or in the account switcher, which shows it beside the name.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "newrelic-dashboard.json"
PLACEHOLDER = 1234567


def stamp(document: dict, account_id: int) -> int:
    """Replace every accountIds entry in place. Returns how many were changed."""
    changed = 0
    for page in document.get("pages", []):
        for widget in page.get("widgets", []):
            for query in widget.get("rawConfiguration", {}).get("nrqlQueries", []):
                if query.get("accountIds") != [account_id]:
                    query["accountIds"] = [account_id]
                    changed += 1
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("account_id", type=int, help="your numeric New Relic account ID")
    parser.add_argument("--stdout", action="store_true",
                        help="print the result instead of writing a file")
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    args = parser.parse_args(argv)

    if args.account_id == PLACEHOLDER:
        print(f"{PLACEHOLDER} is the placeholder, not a real account ID.",
              file=sys.stderr)
        return 2
    if not args.template.exists():
        print(f"template not found: {args.template}", file=sys.stderr)
        return 2

    document = json.loads(args.template.read_text(encoding="utf-8"))
    changed = stamp(document, args.account_id)
    rendered = json.dumps(document, indent=2, ensure_ascii=False) + "\n"

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    out = args.template.with_name(f"newrelic-dashboard-{args.account_id}.json")
    out.write_text(rendered, encoding="utf-8")
    print(f"wrote {out} ({changed} widget queries pointed at account {args.account_id})")
    print("Now: Dashboards -> Import dashboard -> paste the contents of that file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
