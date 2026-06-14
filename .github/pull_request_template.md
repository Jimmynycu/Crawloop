## Summary

<!-- What does this change and why? One short paragraph. -->

## Related

<!-- Closes #123 / part of #123, or links to a design.html section. -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor (no behavior change)
- [ ] Docs / tooling
- [ ] Schema (new or changed output schema in `schemas/`)

## Checklist

- [ ] `ruff check .` passes
- [ ] `pytest -q` passes locally
- [ ] New behavior is covered by tests (no network / no real LLM — use the
      fixture server and `FakeCompleter`)
- [ ] Public behavior changes are reflected in `docs/` (and `README.md` if relevant)

## Safety invariants (confirm any that this PR touches)

- [ ] No fetch path can reach a domain outside the allowlist
- [ ] No loosening of the AST gate (`crawloop/safety.py`) without explicit
      justification below
- [ ] Generated crawlers still go through registration gating + on-disk integrity
      checks; nothing executes ungated source
- [ ] No credentials, API keys, or proxy URLs added to tracked files

## Notes for reviewers

<!-- Anything non-obvious: tradeoffs, follow-ups, areas you want a close look at. -->
