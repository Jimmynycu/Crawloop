"""The validator: the layered correctness gates the loop trusts.

Five gates, applied to an *already-extracted* item list — this module never
fetches anything and never runs crawler code. The M9 gauntlet sandbox-runs a
candidate, then calls these pure functions to judge its output.

The gates, in the order :func:`validate` applies the hard ones:

* **Gate 1 — schema.** Coerce every item through its pydantic model
  (``extra="forbid"``), which simultaneously catches wrong types, extra keys,
  missing required fields, and out-of-range values.
* **Gate 2 — field floors.** Over the *raw* items, the fraction of items where a
  field is present-and-non-None must clear ``min_fill`` for every *required*
  field. This is the partial-break / default-value signal: a crawler that
  silently emits ``None`` for a field it can no longer locate is caught here.
  Because a missing/None *required* field also raises a generic schema error,
  :func:`validate` checks this floor *before* the schema gate so the more
  specific ``fill_rate:<field>`` reason wins; a present-but-wrong-type value
  clears the floor and is left to Gate 1. Optional fields are reported but never
  gate.
* **Item-count floor.** Against a ``baseline`` count, a collapse to far fewer
  items (a broken "next page" link, a selector that now matches one row) fails.
* **Gate 3 — oracle agreement** / **Gate 4 — fixture regression**: see
  :func:`oracle_agreement` / :func:`fixture_regression`; both are the *same*
  comparison engine (:func:`items_agreement`) run against a different reference.
* **Gate 5 — history cross-check**: see :func:`history_crosscheck`; a soft,
  non-fatal drift signal on volatile numeric fields.

:class:`ValidationReport` satisfies the executor's ``ValidationLike`` Protocol
(it exposes ``.ok`` and ``.reason``); :func:`validate` is exactly the callable
the executor injects (extra knobs are keyword-only with defaults).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError

from crawloop.contract import clean_text, parse_money
from crawloop.schemas import get_schema

# Key fields, in preference order, used to align two item lists before comparing
# them position-for-position. The first one that exists on the model is the sort
# key; if none do, the lists are compared by their given order (index).
_ALIGN_KEYS = ("url", "name", "title", "text")

# Guards the denominator in the relative-jump check so an old value of 0 can't
# divide by zero; also the relative tolerance for "effectively exact" volatile
# numeric equality (the gauntlet must not fail on price *format*, only value).
_EPSILON = 1e-9


@dataclass
class ValidationReport:
    """The outcome of :func:`validate`.

    ``ok`` / ``reason`` are the only attributes the M6 executor reads (it depends
    on the structural ``ValidationLike`` Protocol, not on this class), so this
    report drops straight into ``run_family``'s injected ``validate`` slot. The
    rest is for humans and the M8 classifier: ``failures`` always lists the
    concrete per-item schema errors found, even when a later gate is the one that
    decides ``ok``; ``fill_rates`` covers *every* model field (required and
    optional) for reporting though only required fields gate.
    """

    ok: bool
    reason: str
    failures: list[str] = field(default_factory=list)
    fill_rates: dict[str, float] = field(default_factory=dict)
    item_count: int = 0


def _required_fields(model: type[BaseModel]) -> list[str]:
    """Names of ``model``'s required fields (a field is required iff pydantic's
    ``FieldInfo.is_required()`` is True — i.e. no default and not ``X | None``)."""
    return [name for name, fi in model.model_fields.items() if fi.is_required()]


def _fill_rates(items: list[dict], model: type[BaseModel]) -> dict[str, float]:
    """Per-field fraction of *raw* items where the key is present and not None.

    Computed for every declared field so the report can show optional-field
    coverage too; the count is over the raw dicts (pre-coercion) so a
    present-but-None value reads as *unfilled* — that is the signal Gate 2 wants.
    """
    n = len(items)
    rates: dict[str, float] = {}
    for name in model.model_fields:
        filled = sum(1 for item in items if item.get(name) is not None)
        rates[name] = filled / n
    return rates


def validate(
    items: list[dict],
    schema_ref: str,
    *,
    baseline: int | None = None,
    min_fill: float = 0.8,
    min_count_ratio: float = 0.5,
) -> ValidationReport:
    """Run gates 1-2 plus the item-count floor over ``items`` and report.

    Returns a :class:`ValidationReport`. ``ok`` is True (with ``reason == ""``)
    only when every hard gate passes. When more than one gate would fail, the
    *first* in this precedence decides ``reason``:

    ``empty`` -> required-field ``fill_rate`` -> ``schema`` -> ``item_count``.

    Fill is checked before schema on purpose: a required field that is *absent or
    None* is a fill/partial-break signal (and would also raise a generic schema
    error), and the more specific ``fill_rate:<field>`` reason is the useful one.
    A required field that is *present with the wrong type* clears the fill floor
    and is left for the schema gate to report. ``failures`` always carries the
    per-item schema errors that were found, regardless of the deciding reason.

    ``baseline``/``min_fill``/``min_count_ratio`` are keyword-only with defaults
    so this signature still satisfies the executor's ``validate(items,
    schema_ref)`` call.
    """
    item_count = len(items)

    # Empty extraction: nothing to gate, and an empty page is itself a failure.
    if item_count == 0:
        return ValidationReport(ok=False, reason="empty", item_count=0)

    model = get_schema(schema_ref)

    # Gate 1 (schema): coerce every item; collect each ValidationError verbatim.
    # Always computed so `failures` is populated even when another gate decides.
    failures: list[str] = []
    for i, item in enumerate(items):
        try:
            model(**item)
        except ValidationError as err:
            failures.append(f"item[{i}]: {err}")

    # fill_rates are reported for ALL fields; only required ones gate (Gate 2).
    fill_rates = _fill_rates(items, model)

    # The deciding `reason` is the first failing gate in precedence order; the
    # full report (failures + fill_rates) is attached to every outcome via the
    # single return below.
    reason = ""
    low_fields = [n for n in _required_fields(model) if fill_rates[n] < min_fill]
    if low_fields:
        # Gate 2 (field floors): a required field went dark (missing/None).
        reason = f"fill_rate:{low_fields[0]}"
    elif failures:
        # Gate 1 (schema): present-but-wrong (type / range / extra key).
        reason = f"schema: {len(failures)}/{item_count} items invalid"
    elif baseline is not None and item_count < min_count_ratio * baseline:
        # Item-count floor: a collapse relative to a known baseline is a break.
        reason = f"item_count: {item_count} < {min_count_ratio}*{baseline}"

    return ValidationReport(
        ok=reason == "",
        reason=reason,
        failures=failures,
        fill_rates=fill_rates,
        item_count=item_count,
    )


# --------------------------------------------------------------------------- #
# Gates 3-5: pure comparison over already-extracted item lists.
#
# One comparison engine (`items_agreement`) backs both the oracle gate (3) and
# the fixture-regression gate (4); `field_equal` is the single value comparator
# they and the history gate (5) all route through, so normalization rules live
# in exactly one place.
# --------------------------------------------------------------------------- #


def _as_money(value: Any) -> Decimal | None:
    """Best-effort numeric reading of ``value`` as a money amount, or None.

    Bools are explicitly excluded (``True`` is an ``int`` in Python, but a stock
    flag is not a price). ``Decimal`` passes through; ``int``/``float`` and
    money-ish strings ("£51.77", "1,299.00") are parsed via
    :func:`contract.parse_money` so every numeric reading shares one parser.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return parse_money(str(value))
    if isinstance(value, str):
        return parse_money(value)
    return None


def field_equal(a: Any, b: Any, *, volatile: bool) -> bool:
    """Whether two field values are equal under the validator's normalization.

    Normalization order, applied to both sides:

    * **None** — equal iff both are None.
    * **bool** — exact identity (no numeric coercion; a flag is not a number).
    * **money/numeric** — if both read as money (:func:`_as_money`), compare by
      value. A *stable* field must match exactly after normalization (so
      "£51.77" == "51.77" == ``Decimal("51.77")``); a *volatile* field is
      compared with a tiny relative tolerance (``_EPSILON``) — still effectively
      exact, but routed through the numeric path so price *format* never trips
      the gauntlet, only a genuine value change would.
    * **string** — collapse/strip whitespace (:func:`contract.clean_text`) then
      compare.
    * **fallback** — plain ``==``.
    """
    if a is None or b is None:
        return a is None and b is None

    if isinstance(a, bool) or isinstance(b, bool):
        return a == b

    ma, mb = _as_money(a), _as_money(b)
    if ma is not None and mb is not None:
        if not volatile:
            return ma == mb
        denom = max(abs(ma), Decimal(str(_EPSILON)))
        return abs(ma - mb) / denom <= Decimal(str(_EPSILON))

    if isinstance(a, str) and isinstance(b, str):
        return clean_text(a) == clean_text(b)

    return a == b


def _align_key(model: type[BaseModel]) -> str | None:
    """The field both lists are sorted by before pairwise comparison, or None.

    The first of :data:`_ALIGN_KEYS` that the model declares (``url`` before
    ``name`` before ``title`` before ``text``); None means "no stable key — keep
    each list's given order and pair by index".
    """
    for key in _ALIGN_KEYS:
        if key in model.model_fields:
            return key
    return None


def _aligned_pairs(
    a_items: list[dict], b_items: list[dict], key: str | None
) -> list[tuple[dict, dict]]:
    """Pair the two lists up position-for-position, padding the shorter with
    ``{}``.

    With a stable ``key`` both lists are first sorted by ``str(item.get(key))``
    so the same logical row lines up across runs even if order shifted; without
    one they are paired in given order. A padded ``{}`` makes every field read as
    missing, so an unmatched row disagrees on everything — exactly the
    "missing-on-one-side counts as disagreement" rule.
    """
    if key is not None:
        a_items = sorted(a_items, key=lambda it: str(it.get(key, "")))
        b_items = sorted(b_items, key=lambda it: str(it.get(key, "")))
    n = max(len(a_items), len(b_items))
    a_padded = a_items + [{}] * (n - len(a_items))
    b_padded = b_items + [{}] * (n - len(b_items))
    return list(zip(a_padded, b_padded))


def _pair_agreement(
    a: dict, b: dict, fields: list[str], volatile: set[str]
) -> tuple[int, int]:
    """Field-agreement ``(agree, total)`` for ONE aligned pair of items.

    Compares every model field of the pair with :func:`field_equal` (volatile
    fields via the tolerant numeric path); a field present on one side but
    missing/None on the other is a disagreement, both-missing agrees. The single
    per-pair counter both :func:`items_agreement` (which pools it across all
    pairs) and :func:`agreement_detail` (which keeps it per item) route through,
    so the comparison rule lives in exactly one place.
    """
    agree = 0
    for name in fields:
        if field_equal(a.get(name), b.get(name), volatile=name in volatile):
            agree += 1
    return agree, len(fields)


def items_agreement(a_items: list[dict], b_items: list[dict], schema_ref: str) -> float:
    """Fraction of field comparisons on which two item lists agree.

    The one comparison engine behind Gate 3 (oracle) and Gate 4 (fixture). The
    lists are aligned (sorted by a stable key if the schema has one, else by
    index) and padded to equal length; then every model field of every aligned
    pair is compared with :func:`field_equal` (volatile fields via the tolerant
    numeric path). A field present on one side but missing/None on the other is a
    disagreement; both-missing agrees. Returns agreeing / total comparisons.

    Both lists empty -> 1.0 (nothing extracted, nothing to disagree about); one
    empty while the other is not -> 0.0.

    This is the MEAN view (agreeing / total over all fields of all pairs); it is
    kept for reporting and back-compat. The gauntlet's promotion GATE uses
    :func:`agreement_detail`, whose per-item minimum a single wrong row cannot
    average away.
    """
    if not a_items and not b_items:
        return 1.0
    if not a_items or not b_items:
        return 0.0

    model = get_schema(schema_ref)
    volatile = getattr(model, "VOLATILE", set())
    fields = list(model.model_fields)
    key = _align_key(model)

    agree = 0
    total = 0
    for a, b in _aligned_pairs(a_items, b_items, key):
        pair_agree, pair_total = _pair_agreement(a, b, fields, volatile)
        agree += pair_agree
        total += pair_total
    return agree / total if total else 1.0


@dataclass
class AgreementDetail:
    """The per-item view of one extraction-vs-reference comparison.

    Where :func:`items_agreement` returns a single pooled mean (which a single
    wrong row can average away), this breaks the comparison out so the gauntlet
    can gate on the WORST row and on a matching item count:

    * ``mean`` — mean per-item agreement (the per-item average of the field
      agreements), the same number the reporting :func:`items_agreement` yields;
    * ``min_item`` — the LOWEST per-item agreement; 1.0 when both lists are empty,
      0.0 when exactly one side is empty (an extracted-vs-nothing mismatch);
    * ``count_match`` — whether the two lists have the same length (a dropped or
      duplicated row is invisible to ``mean`` once padded, but shows up here);
    * ``n_items`` — the reference (oracle/fixture) item count.
    """

    mean: float
    min_item: float
    count_match: bool
    n_items: int


def agreement_detail(
    actual: list[dict], oracle: list[dict], schema_ref: str
) -> AgreementDetail:
    """Per-item agreement of ``actual`` against the trusted ``oracle``.

    Aligns the two lists with the SAME alignment + :func:`field_equal` logic as
    :func:`items_agreement` (one comparison engine — no duplication), then,
    instead of pooling, keeps each aligned pair's agreement (fraction of the
    model's fields that agree for that pair, volatile fields via the tolerant
    numeric path). Returns an :class:`AgreementDetail` carrying the mean, the
    minimum per-item agreement, whether the item counts match, and the oracle's
    item count.

    Empty handling mirrors :func:`items_agreement`: both empty -> a perfect,
    count-matching detail (nothing extracted, nothing to disagree about); exactly
    one side empty -> ``min_item``/``mean`` 0.0 (and the counts do not match).
    """
    n_actual, n_oracle = len(actual), len(oracle)
    count_match = n_actual == n_oracle

    if not actual and not oracle:
        return AgreementDetail(mean=1.0, min_item=1.0, count_match=True, n_items=0)
    if not actual or not oracle:
        return AgreementDetail(
            mean=0.0, min_item=0.0, count_match=count_match, n_items=n_oracle
        )

    model = get_schema(schema_ref)
    volatile = getattr(model, "VOLATILE", set())
    fields = list(model.model_fields)
    key = _align_key(model)

    per_item: list[float] = []
    for a, b in _aligned_pairs(actual, oracle, key):
        pair_agree, pair_total = _pair_agreement(a, b, fields, volatile)
        per_item.append(pair_agree / pair_total if pair_total else 1.0)

    return AgreementDetail(
        mean=sum(per_item) / len(per_item),
        min_item=min(per_item),
        count_match=count_match,
        n_items=n_oracle,
    )


def oracle_agreement(
    candidate_items: list[dict], oracle_items: list[dict], schema_ref: str
) -> float:
    """Gate 3: agreement of a candidate's extraction with the oracle's.

    A named alias of :func:`items_agreement` (candidate vs oracle) — one
    comparison engine, this name documents the M9 gauntlet's intent. A score
    below 1.0 with all items schema-valid is the wrong-element-right-type signal
    (the candidate read a plausible value from the wrong DOM node).
    """
    return items_agreement(candidate_items, oracle_items, schema_ref)


def fixture_regression(
    actual_items: list[dict], expected_items: list[dict], schema_ref: str
) -> float:
    """Gate 4: agreement of a fresh extraction with a stored golden list.

    The same comparison as Gate 3, run against expected/golden JSON instead of a
    live oracle. The sandbox run that produces ``actual_items`` belongs to M9;
    here it is pure comparison.
    """
    return items_agreement(actual_items, expected_items, schema_ref)


def history_crosscheck(
    items: list[dict],
    history_rows: list[dict],
    schema_ref: str,
    *,
    jump_ratio: float = 0.5,
) -> list[str]:
    """Gate 5: soft drift check of ``items`` against the most-recent prior run.

    ``history_rows`` are registry ``recent_history`` rows (newest-first, each
    carrying an ``items`` list). Only the newest prior extraction is used. After
    aligning by the schema's stable key, each *volatile numeric* field is checked
    for a relative move greater than ``jump_ratio`` (``abs(new-old)/max(|old|,
    eps)``); each breach yields ``"<key>:<field> jumped <old>-><new>"``.

    Non-volatile fields and non-numeric volatile fields (e.g. a boolean stock
    flag) are never jump-checked. The result is advisory — the caller (the M9
    gauntlet) decides what a large jump means. No prior history -> ``[]``.
    """
    if not history_rows or not items:
        return []
    prior = history_rows[0].get("items") or []
    if not prior:
        return []

    model = get_schema(schema_ref)
    volatile = getattr(model, "VOLATILE", set())
    key = _align_key(model)

    warnings: list[str] = []
    for new_item, old_item in _aligned_pairs(items, prior, key):
        label = str(new_item.get(key)) if key else "?"
        for fieldname in volatile:
            new_money = _as_money(new_item.get(fieldname))
            old_money = _as_money(old_item.get(fieldname))
            if new_money is None or old_money is None:
                continue
            denom = max(abs(old_money), Decimal(str(_EPSILON)))
            if abs(new_money - old_money) / denom > Decimal(str(jump_ratio)):
                warnings.append(f"{label}:{fieldname} jumped {old_money}->{new_money}")
    return warnings
