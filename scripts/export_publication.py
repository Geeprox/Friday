#!/usr/bin/env python3
"""Export only files passing the publication audit into a new directory.

The result has no Git history, credentials, runtime data or ignored artifacts.
Scan it independently before initializing a new public repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from audit_publication import scan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--private-patterns', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        parser.error('export requires a new directory')
    literals = json.loads(args.private_patterns.read_text()) if args.private_patterns else []
    if not isinstance(literals, list) or any(not isinstance(item, str) for item in literals):
        parser.error('private patterns must be a JSON list of strings')
    result = scan(root, literals)
    if result['findings']:
        print(json.dumps({'exported': False, 'findings': result['findings']}, indent=2))
        raise SystemExit(1)
    output.mkdir(parents=True)
    for relative in result['records']:
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != result['records'][relative]:
            raise SystemExit('Source changed during export; discard this export and review a new snapshot')
    print(json.dumps({'exported': True, 'files': result['files'], 'source_sha256': result['source_sha256'],
                      'git_history_included': False}, indent=2))


if __name__ == '__main__':
    main()
