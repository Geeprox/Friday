#!/usr/bin/env python3
"""Fail closed on private publication inputs. Prints locations, never matches.

Scan the Git file inventory (including untracked source), or an exported directory.
An optional operator-local JSON list supplies additional literal private strings.
This complements a secret scanner; a passing report is not a proof of anonymity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

EXCLUDED = {'.git', '.artifacts', '.venv', 'node_modules', '__pycache__', '.pytest_cache',
            '.ruff_cache', '.build', 'build', 'dist', '.DS_Store'}
PRIVATE_ROOTS = {'data', 'evidence', '.private', 'credentials'}
PRIVATE_DOCS = {'evidence', 'context'}
BINARY_PREFIXES = ('native/FridayDesktop/Resources/AppIcon/', 'native/FridayTV/android/res/drawable/')
FORBIDDEN_SUFFIXES = {'.sqlite', '.sqlite3', '.db', '.pem', '.key', '.p12', '.mp4', '.mov', '.wav',
                      '.m4a', '.aiff', '.zip', '.tar', '.gz', '.bundle', '.patch', '.ses', '.onnx', '.safetensors'}
SYNTHETIC_MEDIA = {'fixtures/homeplane/archive-simulated.mp4':
                   'a1f318cbd2d02ce033bc05256938d48edf26cd440abd4df01bb6b2a57999f038'}
RULES = {
    'personal_home_path': re.compile(r'(?<![\w/])/(?:Users|home)/[A-Za-z0-9_.-]+/'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'github_token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    'cloud_access_key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'provider_key': re.compile(r'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}'),
    'jwt': re.compile(r'\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}'),
    'mac_address': re.compile(r'(?i)\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b'),
    'email': re.compile(r'\b[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
}
# Public upstream attribution is not household data. No real email is required.
SAFE_EMAIL_DOMAINS = {'example.com', 'example.org', 'example.invalid', 'friday.invalid'}


def inventory(root):
    if (root / '.git').exists():
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '-co', '--exclude-standard', '-z'],
                                check=True, capture_output=True)
        return sorted({name for name in result.stdout.decode().split('\0') if name})
    return sorted(p.relative_to(root).as_posix() for p in root.rglob('*')
                  if p.is_file() and not any(part in EXCLUDED for part in p.relative_to(root).parts))


def scan(root, private=()):
    findings, records = [], {}
    for name in inventory(root):
        path, parts = root / name, Path(name).parts
        if not path.exists():
            continue
        if path.is_symlink():
            findings.append({'path': name, 'rule': 'symlink'})
            continue
        if name in SYNTHETIC_MEDIA:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != SYNTHETIC_MEDIA[name]:
                findings.append({'path': name, 'rule': 'synthetic_fixture_changed'})
            else:
                records[name] = digest
            continue
        if (parts[0] in PRIVATE_ROOTS or any(part in EXCLUDED for part in parts)
                or parts[:1] == ('docs',) and len(parts) > 1 and parts[1] in PRIVATE_DOCS
                or path.name.startswith('.env') and path.name != '.env.example'
                or path.name.endswith(('.local.json', '.home.json', '.local.yaml'))
                or path.suffix in FORBIDDEN_SUFFIXES):
            findings.append({'path': name, 'rule': 'private_file'})
            continue
        raw = path.read_bytes()
        records[name] = hashlib.sha256(raw).hexdigest()
        try:
            value = raw.decode('utf-8')
        except UnicodeDecodeError:
            if not name.startswith(BINARY_PREFIXES):
                findings.append({'path': name, 'rule': 'unreviewed_binary'})
            continue
        for rule, pattern in RULES.items():
            for match in pattern.finditer(value):
                if rule == 'email' and match[0].rsplit('@', 1)[1].lower() in SAFE_EMAIL_DOMAINS:
                    continue
                if rule == 'email' and match[0].endswith('@2x.png'):
                    continue
                # Generated/vendor attribution retains the upstream license.
                if rule == 'email' and ('/vendor/' in name or path.name in {'LICENSE', 'THREE-LICENSE.txt'}):
                    continue
                findings.append({'path': name, 'line': value.count('\n', 0, match.start()) + 1, 'rule': rule})
        for index, literal in enumerate(private):
            if literal and literal in value:
                findings.append({'path': name, 'rule': 'operator_private_literal', 'pattern_index': index})
    return {'schema_version': 1, 'files': len(records), 'findings': findings,
            'source_sha256': hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
            'records': records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--private-patterns', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    private = json.loads(args.private_patterns.read_text()) if args.private_patterns else []
    if not isinstance(private, list) or any(not isinstance(x, str) for x in private):
        parser.error('private patterns must be a local JSON list of strings')
    result = scan(args.root.resolve(), private)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        args.output.chmod(0o600)
    print(json.dumps({k: v for k, v in result.items() if k != 'records'}, indent=2))
    raise SystemExit(bool(result['findings']))


if __name__ == '__main__':
    main()
