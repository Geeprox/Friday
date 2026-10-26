"""Review-only people metadata. Importing this package never runs a model."""

from .catalog import PeopleCatalog, PeopleError

__all__ = ["PeopleCatalog", "PeopleError"]
