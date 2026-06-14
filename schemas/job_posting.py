"""A small, generic example output schema: a job posting.

This is a domain-neutral schema used by the example tests and as a template for
contributors. It deliberately exercises every transform the deterministic
strategies support, and it mirrors the shape real schemas have: a small REQUIRED
core the deterministic crawler can read straight from the page's embedded JSON,
plus a few OPTIONAL, NORMALIZED fields it cannot reach (an enum code, a derived
string) that the hybrid's one-call LLM tail fills in — the family's "residual set".

* ``title`` / ``company`` — required plain strings stored verbatim.
* ``salary`` — a required numeric field listed in :attr:`JobPosting.VOLATILE`, so
  the value-path discoverer treats it as a quantity and handles unit scaling: a
  feed that stores the salary in units of ten thousand (JSON ``12``) maps to the
  record value ``120000``.
* ``employment_type`` — an OPTIONAL :class:`enum.Enum` field, so a source LABEL on
  the page (e.g. ``"Full-time"``) is mapped to the schema's CODE (``"full_time"``).
  Enum codes never appear verbatim in the source JSON, so this is filled by the
  path-map ``{map}`` transform / LLM codegen rather than value-path discovery.
* ``location`` — an OPTIONAL DERIVED string assembled by concatenating ``city`` and
  ``region`` (``"Springfield, IL"``); no single JSON leaf holds it, so it is built
  with the path-map ``{concat}`` transform.
* ``url`` — an optional :class:`~pydantic.HttpUrl`.
* ``remote`` — an optional boolean.
"""

import enum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class EmploymentType(str, enum.Enum):
    """The kind of engagement a posting offers (the schema's enum CODES)."""

    full_time = "full_time"
    part_time = "part_time"
    contract = "contract"


class JobPosting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required core — read straight from the page JSON by the deterministic crawler.
    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    # Numeric field: source feeds store the figure in units of ten thousand, so the
    # value-path discoverer scales the leaf up to the record value (12 -> 120000).
    salary: int = Field(gt=0)

    # Optional normalized / derived tail — the hybrid's residual set. The
    # deterministic crawler systematically cannot reach these, so the one-call LLM
    # tail fills them when present.
    # Enum field: a source label ("Full-time") maps to the schema code ("full_time").
    employment_type: EmploymentType | None = None
    # Derived field: city + ", " + region, assembled from two JSON leaves.
    location: str | None = Field(default=None, min_length=1)
    url: HttpUrl | None = None
    remote: bool | None = None

    # The volatile / frequently-changing fields. The loop treats these as the numeric
    # quantities to unit-scale during value-path discovery.
    VOLATILE: ClassVar[set[str]] = {"salary"}
