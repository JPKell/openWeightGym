#!/usr/bin/env python3
"""Print the same coverage report enforced by tests/test_docs_coverage.py."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weightroom.docs_coverage import report

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='Print machine-readable counts and gap identities')
    args = parser.parse_args()
    found = report()
    if args.json:
        print(json.dumps(found, indent=2))
    else:
        print(f"Pages: {found['covered_pages']}/{found['pages']}; endpoints: {found['endpoints']}; abilities: {found['abilities']}; settings: {found['settings']}")
        print(f"Gaps: {len(found['gaps'])}")
        print('\n'.join(found['gaps']))
    sys.exit(bool(found['gaps']))
