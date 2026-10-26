"""Silent AirPlay discovery. Does not pair, play or change speaker settings."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from homeplane.embodied.airplay import discover, probe, select, status


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--select-id", help="Select one discovered HomePod identifier; never pairs or plays")
    parser.add_argument("--host", help="Check the HomePod's private IPv4 address when multicast discovery is missing")
    parser.add_argument("--check", action="store_true", help="Verify the saved receiver connection without playing audio")
    args = parser.parse_args()
    devices = await discover(12, host=args.host)
    if args.select_id:
        await select(args.select_id, host=args.host)
    print(json.dumps(dict(output=await probe() if args.check else status(), devices=devices), ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
