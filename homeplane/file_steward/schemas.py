"""Generate the owned preview schemas without source/model IO."""

from homeplane.contracts import Contract
from homeplane.schema_export import write_schemas

from . import agent_loop, contracts, extraction_queue, pdf_text, policy, settings_contracts


def generate(destination):
    modules = (contracts, policy, agent_loop, extraction_queue, pdf_text, settings_contracts)
    write_schemas(destination, {
        name: value
        for module in modules
        for name, value in vars(module).items()
        if name.startswith("Steward") and isinstance(value, type) and issubclass(value, Contract)
    })


if __name__ == "__main__":
    generate("schemas")
