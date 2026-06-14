"""Versioned output-schema registry.

Schemas are contributed as plain ``.py`` files in the top-level ``schemas/``
directory. The registry discovers them by scanning that directory on disk and
importing each module *by path* (the same ``importlib.util.spec_from_file_location``
mechanism the crawler loader uses), then registers every pydantic ``BaseModel``
subclass under the key ``f"{cls.__name__}@1"``.

Every model is expected to define ``VOLATILE: ClassVar[set[str]]``, which stays
exposed on the returned class. Downstream readers default a missing ``VOLATILE``
to an empty set (``getattr(cls, "VOLATILE", set())``) so a model that omits it
does not crash; the registry itself registers the class untouched.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from pydantic import BaseModel

# Repo root = parent of the crawloop package directory; schemas/ lives there.
_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

_REGISTRY: dict[str, type[BaseModel]] | None = None


class SchemaNotFound(Exception):
    """Raised when a versioned schema ref is not registered."""


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"_schema_{path.stem}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load schema module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_registry() -> dict[str, type[BaseModel]]:
    registry: dict[str, type[BaseModel]] = {}
    for path in sorted(_SCHEMAS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = _load_module(path)
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BaseModel)
                and value is not BaseModel
            ):
                registry[f"{value.__name__}@1"] = value
    return registry


def _registry() -> dict[str, type[BaseModel]]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def get_schema(ref: str) -> type[BaseModel]:
    """Return the model class registered under ``ref`` (e.g. ``"Product@1"``).

    Tolerates a version-less ref: ``"Product"`` resolves to ``"Product@1"``. This
    matters because crawler ``schema_ref`` attributes are LLM-generated and the
    model sometimes drops the ``@1`` suffix — the system should default to v1
    rather than crash on an otherwise-valid schema name.
    """
    reg = _registry()
    if ref in reg:
        return reg[ref]
    if "@" not in ref and f"{ref}@1" in reg:
        return reg[f"{ref}@1"]
    raise SchemaNotFound(ref)


def schema_json(ref: str) -> dict:
    """JSON schema for the model registered under ``ref``."""
    return get_schema(ref).model_json_schema()
