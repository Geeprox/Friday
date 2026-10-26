#!/bin/sh
# Coordinator must allocate a native slot before running. No package resolution or cache copies.
set -eu
cd "$(dirname "$0")/.."
mkdir -p .build/module-cache evidence
xcrun swiftc -module-cache-path .build/module-cache native/PDFKitVisionPageBridge.swift \
    -o .build/apple-document-bridge.next > evidence/bridge-compile-final.log 2>&1
mv .build/apple-document-bridge.next .build/apple-document-bridge
/usr/bin/python3 - <<'PY'
import hashlib,json,subprocess
from pathlib import Path
files=['native/PDFKitVisionPageBridge.swift','.build/apple-document-bridge']
record={'command':'xcrun swiftc -module-cache-path .build/module-cache native/PDFKitVisionPageBridge.swift -o .build/apple-document-bridge.next',
        'swift':subprocess.check_output(['xcrun','swiftc','--version'],text=True).strip(),
        'compiled':True,'files':[{ 'path':f,'sha256':hashlib.sha256(Path(f).read_bytes()).hexdigest()} for f in files]}
Path('evidence/build.json').write_text(json.dumps(record,indent=2)+'\n')
PY
