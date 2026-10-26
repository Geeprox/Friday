"""Generate the owned preview schemas without source/model IO."""

import json
from pathlib import Path

from homeplane.contracts import Contract

from . import agent_loop, contracts, extraction_queue, pdf_text, policy, settings_contracts


def generate(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for module in (contracts, policy, agent_loop, extraction_queue, pdf_text, settings_contracts):
        for name, value in vars(module).items():
            if name.startswith("Steward") and isinstance(value, type) and issubclass(value, Contract):
                (destination / (name + ".json")).write_text(
                    json.dumps(value.model_json_schema(), indent=2) + "\n"
                )


if __name__ == "__main__":
    generate("schemas")
