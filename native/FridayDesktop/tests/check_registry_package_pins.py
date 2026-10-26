"""Check a local source-built App against current public source hashes.

Usage: python check_registry_package_pins.py /path/to/Friday.app
This verifies packaged inputs; it does not establish runtime/device acceptance.
"""
import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[3]
APP = Path(sys.argv[1]).resolve()
binding = json.loads((APP / 'Contents/Resources/FRIDAY-SOURCE-INTEGRATION.json').read_text())
assert binding['schema'] == 2 and binding['records'] and binding['native_inputs']
for name, expected in binding['records'].items():
    relative = Path(name)
    assert not relative.is_absolute() and '..' not in relative.parts
    source, packaged = REPO / relative, APP / 'Contents/Resources/product/app' / relative
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected
    assert hashlib.sha256(packaged.read_bytes()).hexdigest() == expected
for name, expected in binding['native_inputs'].items():
    relative = Path(name)
    assert not relative.is_absolute() and '..' not in relative.parts
    assert hashlib.sha256((REPO / relative).read_bytes()).hexdigest() == expected
print(json.dumps({'source_binding_verified': True, 'packaged_files': len(binding['records']),
                  'native_inputs': len(binding['native_inputs']), 'runtime_or_devices_tested': False}))
