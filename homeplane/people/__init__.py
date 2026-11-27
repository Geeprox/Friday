"""Review-only people metadata. Importing this package never runs a model."""

from .catalog import PeopleCatalog
from .contracts import PeopleError

__all__ = ["PeopleCatalog", "PeopleError"]
