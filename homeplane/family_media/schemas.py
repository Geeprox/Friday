"""Generate only the owned FamilyMedia*.json schemas."""

import argparse
import json
from pathlib import Path

from homeplane.contracts import Contract

from . import contracts


def generate(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for name, value in vars(contracts).items():
        if name.startswith("FamilyMedia") and isinstance(value, type) and issubclass(value, Contract):
            (destination / (name + ".json")).write_text(
                json.dumps(value.model_json_schema(), indent=2) + "\n"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="schemas")
    generate(parser.parse_args().output)
