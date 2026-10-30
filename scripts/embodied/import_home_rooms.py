"""Preview/import room membership from an operator-local Mi Home room inventory.

The inventory contains home_id and rooms [{id, name, devices: [did]}].
Names are retained by the existing layout; only imported room/device IDs join.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from homeplane.home_layout import LayoutStore
from homeplane.home_spatial import from_mijia


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory',type=Path,required=True)
    parser.add_argument('--layout',type=Path,required=True)
    parser.add_argument('--region',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--preview',action='store_true')
    args=parser.parse_args()
    if args.inventory.is_symlink() or args.inventory.stat().st_size>262144:
        parser.error('bounded local inventory required')
    layout=LayoutStore(args.layout).current()
    if layout is None:parser.error('import a layout first')
    value=from_mijia(json.loads(args.inventory.read_bytes()),layout,args.region)
    print(json.dumps({'rooms':len(set(value.device_rooms.values())),
                      'device_memberships':len(value.device_rooms),'preview':args.preview}))
    if args.preview:return
    if args.output.exists() or args.output.is_symlink():
        parser.error('use a new output file; keep the current membership for rollback')
    args.output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:stream.write(value.model_dump_json())


if __name__=='__main__':main()
