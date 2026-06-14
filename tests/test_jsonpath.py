"""Offline tests for the deterministic value-path crawler generator (Task 1).

Everything here is pure + offline: no network, no LLM, no subprocess except the
real sandbox (which is itself offline — it only ever sees the one stored HTML).
The module under test, :mod:`crawloop.loop.jsonpath`, discovers WHERE each
trusted oracle value lives inside a page's embedded JSON (value -> path) and emits
a tiny crawler that reads those exact paths. The discovery is just pure functions
over parsed JSON, so it is fully testable without ever guessing with a model.

Coverage:

* :func:`iter_leaves` / :func:`read_path` round-trip on a nested dict+list.
* :func:`extract_json_blobs` pulls ``__NEXT_DATA__`` / ``application/ld+json`` /
  ``application/json`` script bodies and skips unparseable ones.
* :func:`discover_paths` on a synthetic 2-sample page: a salary stored in units of
  ten thousand (e.g. ``12``) whose oracle is ``120000`` is found WITH ``scale=10000``;
  a deep string is found; a field whose value sits at a different path in each
  sample is NOT returned (no cross-sample-consistent path).
* :func:`emit_crawler` output passes the AST gate AND, run in the real offline
  sandbox over a synthetic ``__NEXT_DATA__`` page, reproduces the oracle values
  (including the scaled salary).
"""

from __future__ import annotations

from crawloop.loop.jsonpath import (
    Discovery,
    discover_paths,
    emit_crawler,
    extract_json_blobs,
    iter_leaves,
    read_path,
)
from crawloop.loop.sandbox import run_in_sandbox
from crawloop.safety import ast_check

# Markers the production HTML reducer (crawloop.htmlutil) keys on, reused so
# the discovery/emit path stays aligned with what the rest of the system reads.
JSON_MARKERS = ["__NEXT_DATA__", "application/ld+json", "application/json"]


# --------------------------------------------------------------------------- #
# iter_leaves / read_path
# --------------------------------------------------------------------------- #


def test_iter_leaves_walks_dicts_and_lists():
    """Every scalar leaf is yielded with a path of dict-keys / list-indices, and
    that path reads the leaf back via read_path. Containers themselves are not
    leaves; only str/int/float/bool/None are."""
    obj = {
        "a": 1,
        "b": {"c": "deep", "d": [10, 20]},
        "e": [{"f": True}, {"f": None}],
    }
    leaves = dict(iter_leaves(obj))

    assert leaves == {
        ("a",): 1,
        ("b", "c"): "deep",
        ("b", "d", 0): 10,
        ("b", "d", 1): 20,
        ("e", 0, "f"): True,
        ("e", 1, "f"): None,
    }
    # Every discovered path reads its value back.
    for path, value in leaves.items():
        assert read_path(obj, path) == value


def test_read_path_misses_return_none_never_raise():
    """A path that runs off the end of a dict/list, indexes a scalar, or uses the
    wrong key/index type yields None rather than raising."""
    obj = {"a": {"b": [0, 1]}}
    assert read_path(obj, ("a", "b", 1)) == 1
    assert read_path(obj, ("a", "missing")) is None  # absent key
    assert read_path(obj, ("a", "b", 5)) is None  # index past end
    assert read_path(obj, ("a", "b", "x")) is None  # str index into a list
    assert read_path(obj, ("a", "b", 0, "deeper")) is None  # index into a scalar
    assert read_path(obj, ()) is obj  # empty path is the object itself


# --------------------------------------------------------------------------- #
# extract_json_blobs
# --------------------------------------------------------------------------- #


def test_extract_json_blobs_finds_marked_scripts_and_skips_garbage():
    """Only script tags whose OPEN tag carries a JSON marker are parsed; bodies
    that are not valid JSON are skipped, valid ones are returned parsed."""
    html = """
    <html><head>
      <script id="__NEXT_DATA__" type="application/json">{"props": {"x": 1}}</script>
      <script type="application/ld+json">{"@type": "JobPosting", "title": "Z"}</script>
      <script type="application/json">not valid json {</script>
      <script>var ignoreMe = {"not": "json-marked"};</script>
    </head></html>
    """
    blobs = extract_json_blobs(html)
    assert {"props": {"x": 1}} in blobs
    assert {"@type": "JobPosting", "title": "Z"} in blobs
    # The unparseable application/json body is skipped; the plain <script> (no
    # marker) is never considered.
    assert len(blobs) == 2


# --------------------------------------------------------------------------- #
# discover_paths
# --------------------------------------------------------------------------- #


def _sample(salary_units: int, company: str, city: str, stray: str) -> dict:
    """A synthetic page-JSON shaped like a Next.js payload: the salary is stored in
    units of ten thousand (so the oracle value is x10000 the leaf), the city is
    buried deep, and ``stray`` lives at a per-sample-varying path to exercise the
    structural mismatch case."""
    return {
        "props": {
            "pageProps": {
                "detail": {
                    "salaryK": salary_units,  # units of 10k -> oracle is x10000
                    "company": company,
                    "office": {"city": city},
                }
            }
        }
    }


def test_discover_paths_finds_scaled_number_and_deep_string():
    """Two samples whose oracle salaries are x10000 of the JSON leaf, and whose
    cities sit at the SAME deep path, are discovered: the numeric field gets
    scale=10000, the string field scale 1, and the paths are the literal nested
    paths into each sample's JSON."""
    s0 = _sample(12, "Acme Corp", "Springfield", stray="A")
    s1 = _sample(9, "Globex", "Ogdenville", stray="B")
    # Oracle salaries are the full figure (= leaf x 10000); cities verbatim.
    oracle = [
        {"salary": 120000, "company": "Acme Corp", "city": "Springfield"},
        {"salary": 90000, "company": "Globex", "city": "Ogdenville"},
    ]
    disc = discover_paths(
        [s0, s1],
        oracle,
        numeric_fields={"salary"},
    )
    assert isinstance(disc, Discovery)

    base = ["props", "pageProps", "detail"]
    assert disc.paths["salary"] == [*base, "salaryK"]
    assert disc.paths["company"] == [*base, "company"]
    assert disc.paths["city"] == [*base, "office", "city"]

    # The salary needed x10000 to equal the oracle. scale is populated ONLY for
    # numeric fields, so the string fields carry no scale entry.
    assert disc.scale["salary"] == 10000
    assert "company" not in disc.scale
    assert "city" not in disc.scale

    # The discovered (path, scale) actually reproduces the oracle on each sample.
    for sample, rec in zip([s0, s1], oracle):
        assert read_path(sample, disc.paths["salary"]) * disc.scale["salary"] == (
            rec["salary"]
        )
        assert read_path(sample, disc.paths["city"]) == rec["city"]


def test_discover_paths_drops_field_with_no_cross_sample_path():
    """A field whose value lives at a DIFFERENT structural path in each sample has
    no consistent cross-sample path, so it is not returned — the discovery only
    keeps paths that generalize across every sample."""
    # Same builder, but the "stray" value is parked at different keys per sample.
    s0 = _sample(12, "Acme Corp", "Springfield", stray="A")
    s1 = _sample(9, "Globex", "Ogdenville", stray="B")
    s0["props"]["pageProps"]["detail"]["recruiterNote"] = "REF-XYZ"  # path A
    s1["props"]["pageProps"]["meta"] = {"note": "REF-XYZ"}  # path B (different)

    oracle = [
        {"company": "Acme Corp", "recruiter": "REF-XYZ"},
        {"company": "Globex", "recruiter": "REF-XYZ"},
    ]
    disc = discover_paths([s0, s1], oracle, numeric_fields=set())

    # company is consistent and found; the recruiter note has no shared path -> absent.
    assert "company" in disc.paths
    assert "recruiter" not in disc.paths


def test_discover_paths_skips_null_and_skip_fields():
    """Only fields non-null in EVERY sample's oracle are considered, and
    skip_fields are excluded even when present."""
    s0 = _sample(12, "Acme Corp", "city0", stray="A")
    s1 = _sample(9, "Globex", "city1", stray="B")
    oracle = [
        {"company": "Acme Corp", "title": "Engineer", "employment_type": "full_time"},
        {"company": "Globex", "title": None, "employment_type": "full_time"},  # title null here
    ]
    disc = discover_paths(
        [s0, s1],
        oracle,
        numeric_fields=set(),
        skip_fields={"employment_type"},  # enum-like: caller hands it to LLM codegen
    )
    assert "company" in disc.paths
    # title is null in sample 1's oracle -> never considered.
    assert "title" not in disc.paths
    # employment_type is in skip_fields -> excluded regardless.
    assert "employment_type" not in disc.paths


def test_discover_paths_matches_numbers_stored_as_strings():
    """A JSON leaf that is a numeric STRING still value-matches a numeric oracle
    (the scaling rule parses string leaves too)."""
    s0 = {"d": {"salaryK": "12", "headcount": "24"}}
    s1 = {"d": {"salaryK": "9", "headcount": "30"}}
    oracle = [
        {"salary": 120000, "team_size": 24},
        {"salary": 90000, "team_size": 30},
    ]
    disc = discover_paths(
        [s0, s1], oracle, numeric_fields={"salary", "team_size"}
    )
    assert disc.paths["salary"] == ["d", "salaryK"]
    assert disc.scale["salary"] == 10000
    # team_size matches the leaf 1:1 (scale 1), even though the leaf is a string.
    assert disc.paths["team_size"] == ["d", "headcount"]
    assert disc.scale["team_size"] == 1


def test_discover_paths_prefers_lexicographically_smallest_path():
    """When the oracle value matches more than one consistent path across samples,
    the lexicographically-smallest path is recorded so the choice is stable."""
    # "Acme" appears at two keys in BOTH samples; "aaa" sorts before "zzz".
    s0 = {"aaa": "Acme", "zzz": "Acme", "other": "x0"}
    s1 = {"aaa": "Acme", "zzz": "Acme", "other": "x1"}
    oracle = [{"company": "Acme"}, {"company": "Acme"}]
    disc = discover_paths([s0, s1], oracle, numeric_fields=set())
    assert disc.paths["company"] == ["aaa"]


# --------------------------------------------------------------------------- #
# emit_crawler: gate-clean + reproduces the oracle in the real sandbox
# --------------------------------------------------------------------------- #

# A synthetic detail page carrying the full record as a Next.js JSON blob. The
# salary is in units of 10k (12 -> oracle 120000); the title/company/city are strings.
_PAGE_HTML = """<!doctype html>
<html><head><title>x</title></head>
<body>
  <h1>some heading</h1>
  <script id="__NEXT_DATA__" type="application/json">
  {"props":{"pageProps":{"detail":{
     "salaryK":12,
     "title":"Senior Engineer",
     "company":"Acme Corp",
     "office":{"city":"Springfield"}
  }}}}
  </script>
</body></html>
"""


def _job_discovery() -> Discovery:
    """A Discovery hand-built to the synthetic page above, targeting JobPosting@1
    fields: a scaled numeric salary plus three strings. Built directly (not via
    discover_paths) so this test isolates emit_crawler + the sandbox."""
    base = ["props", "pageProps", "detail"]
    return Discovery(
        paths={
            "salary": [*base, "salaryK"],
            "title": [*base, "title"],
            "company": [*base, "company"],
            "location": [*base, "office", "city"],
        },
        # scale carries ONLY numeric fields; the three strings are stored verbatim.
        scale={"salary": 10000},
    )


def test_emit_crawler_passes_ast_gate():
    """The emitted module source clears the static AST allowlist (imports re/json
    + crawloop.contract only, no dunders, no banned calls) — so it is safe to
    register and to run in the sandbox. raise_on_violation must not raise."""
    source = emit_crawler(
        family="jobs.example.com/posting",
        schema_ref="JobPosting@1",
        discovery=_job_discovery(),
        json_markers=JSON_MARKERS,
    )
    # Both the boolean and the raising form agree it is clean.
    assert ast_check(source) == []
    ast_check(source, raise_on_violation=True)
    # Sanity: it really only imports the allowed trio.
    assert "import re" in source
    assert "import json" in source
    assert "from crawloop.contract import" in source


def test_emit_crawler_reproduces_oracle_in_sandbox():
    """Running the emitted crawler in the REAL offline sandbox over the synthetic
    __NEXT_DATA__ page reproduces the oracle values, including the x10000 salary
    (12 -> 120000) and the deep city string."""
    source = emit_crawler(
        family="jobs.example.com/posting",
        schema_ref="JobPosting@1",
        discovery=_job_discovery(),
        json_markers=JSON_MARKERS,
    )
    items = run_in_sandbox(source, _PAGE_HTML, url="https://jobs.example.com/posting/X")
    assert len(items) == 1
    item = items[0]
    assert item["salary"] == 120000  # 12 * 10000, coerced to int
    assert item["title"] == "Senior Engineer"
    assert item["company"] == "Acme Corp"
    assert item["location"] == "Springfield"


def test_emit_crawler_returns_empty_when_no_blob():
    """A page with NO JSON blob matching the markers yields CrawlResult(items=[])
    rather than crashing — the guard the spec requires."""
    source = emit_crawler(
        family="jobs.example.com/posting",
        schema_ref="JobPosting@1",
        discovery=_job_discovery(),
        json_markers=JSON_MARKERS,
    )
    items = run_in_sandbox(source, "<html><body>no json here</body></html>")
    assert items == []


def test_emit_crawler_omits_missing_paths_from_item():
    """When a discovered path is absent on a given page, that field is simply not
    put in the item (its read is None and None values are dropped), so the
    remaining fields still extract."""
    source = emit_crawler(
        family="jobs.example.com/posting",
        schema_ref="JobPosting@1",
        discovery=_job_discovery(),
        json_markers=JSON_MARKERS,
    )
    # A blob missing the salary + city keys, but carrying title + company.
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"detail":{"title":"Title Only","company":"Globex"}}}}'
        "</script></body></html>"
    )
    items = run_in_sandbox(source, html)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Title Only"
    assert item["company"] == "Globex"
    # Absent leaves -> field omitted entirely (not present as None).
    assert "salary" not in item
    assert "location" not in item
