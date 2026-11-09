"""Probe the existing shared ARL application session, without model or device tasks."""
import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from homeplane.arl import ARLRuntime, DEFAULT_HOME


async def check(home=None):
    selected = Path(home).expanduser() if home is not None else DEFAULT_HOME
    runtime = ARLRuntime(lambda: selected)
    try:
        ping = await runtime.application_smoke()
        report = await runtime.status()
        return {
            'observed_at': datetime.now(UTC).isoformat(),
            'status': 'PASS_APPLICATION_METADATA',
            'runtime_version': report.get('runtime_version'),
            'protocol': report.get('protocol'),
            'application': report['application'],
            'ping': ping,
            'model_tasks_submitted': 0,
            'real_model_inference': False,
            'device_writes': False,
            'hardware_verified': False,
        }
    finally:
        await runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', help='Existing shared ARL home; never starts a host')
    parser.add_argument('--output', default='.artifacts/arl-application-metadata.json')
    args = parser.parse_args()
    report = asyncio.run(check(args.home))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ('status', 'model_tasks_submitted', 'hardware_verified')}))


if __name__ == '__main__':
    main()
