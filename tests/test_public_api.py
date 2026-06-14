"""The package exposes a small public API so crawloop can be used as a library,
not only via the CLI. ``crawloop/__init__.py`` was empty (0 bytes), so
``from crawloop import Engine`` failed — competitors all ship an importable API.
"""

from __future__ import annotations


def test_public_api_exports_the_engine_and_loop():
    import crawloop
    from crawloop import Engine, LoopResult, RequestResult, run_loop

    assert Engine is not None
    assert RequestResult is not None
    assert LoopResult is not None
    assert callable(run_loop)
    for name in ("Engine", "RequestResult", "LoopResult", "run_loop"):
        assert name in crawloop.__all__
