"""Generate only the owned FamilyMedia*.json schemas."""

import argparse

from homeplane.contracts import Contract
from homeplane.schema_export import write_schemas

from . import contracts, photo_requests


def generate(destination):
    values = {**vars(contracts), **vars(photo_requests)}
    write_schemas(destination, {
        name: value for name, value in values.items()
        if name.startswith("FamilyMedia") and isinstance(value, type) and issubclass(value, Contract)
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="schemas")
    generate(parser.parse_args().output)
