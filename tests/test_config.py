from pathlib import Path

import pytest

from crawloop.config import (
    AppConfig,
    DomainConfig,
    UnauthorizedDomain,
    load_config,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "authorized_domains.yaml"


@pytest.fixture
def cfg() -> AppConfig:
    return load_config(CONFIG_PATH)


def test_respect_robots_default_from_file(cfg):
    assert cfg.respect_robots is False


def test_is_authorized(cfg):
    assert cfg.is_authorized("books.toscrape.com") is True
    assert cfg.is_authorized("quotes.toscrape.com") is True
    assert cfg.is_authorized("shop.example.com") is True
    assert cfg.is_authorized("evil.com") is False


def test_domain_config_values(cfg):
    dc = cfg.domain_config("books.toscrape.com")
    assert dc.max_rps == 1.0
    assert dc.render_js is False


def test_quotes_render_js_true(cfg):
    assert cfg.domain_config("quotes.toscrape.com").render_js is True


def test_domain_config_missing_raises(cfg):
    with pytest.raises(UnauthorizedDomain):
        cfg.domain_config("evil.com")


def test_default_access_strategies_when_absent(cfg):
    # books has no access_strategies key -> sensible default
    assert cfg.domain_config("books.toscrape.com").access_strategies == [("backoff", {})]


def test_shop_access_strategies_normalized(cfg):
    dc = cfg.domain_config("shop.example.com")
    assert dc.access_strategies == [
        ("backoff", {}),
        ("stealth_browser", {}),
        (
            "session",
            {
                "login_url": "https://shop.example.com/login",
                "creds_env": "EXAMPLE_LOGIN",
            },
        ),
        ("bypass_token", {"header": "x-waf-bypass", "value_env": "EXAMPLE_WAF_TOKEN"}),
    ]
    assert dc.proxy_env == "EXAMPLE_PROXY_URL"
    assert dc.note == "owned by us; authorized 2026-06-12"


def test_assert_authorized_rejects_offlist(cfg):
    with pytest.raises(UnauthorizedDomain):
        cfg.assert_authorized("https://evil.com/x")


def test_assert_authorized_allows_onlist(cfg):
    assert cfg.assert_authorized("https://books.toscrape.com/p") is None


def test_assert_authorized_strips_port(cfg):
    # host with explicit port must still match the bare-domain allowlist
    assert cfg.assert_authorized("https://books.toscrape.com:443/p") is None
    with pytest.raises(UnauthorizedDomain):
        cfg.assert_authorized("https://evil.com:8080/p")


@pytest.mark.parametrize(
    "url",
    [
        "https://books.toscrape.com.evil.com/p",  # suffix spoof
        "https://evilbooks.toscrape.com/p",  # prefix spoof
        "https://sub.books.toscrape.com/p",  # subdomain (exact-host match only)
        "https://user:pass@evil.com/p",  # userinfo spoof
        "not a url",  # unparseable host
    ],
)
def test_assert_authorized_rejects_spoofs(cfg, url):
    with pytest.raises(UnauthorizedDomain):
        cfg.assert_authorized(url)


def test_assert_authorized_host_is_case_insensitive(cfg):
    assert cfg.assert_authorized("https://BOOKS.TOSCRAPE.COM/p") is None


def test_respect_robots_defaults_false_when_key_absent(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("domains:\n  - domain: a.com\n    max_rps: 2.0\n")
    cfg = load_config(p)
    assert cfg.respect_robots is False
    dc = cfg.domain_config("a.com")
    assert dc.max_rps == 2.0
    assert dc.render_js is False
    assert dc.access_strategies == [("backoff", {})]


def test_domain_config_dataclass_defaults():
    dc = DomainConfig(domain="x.com")
    assert dc.max_rps == 1.0
    assert dc.render_js is False
    assert dc.note is None
    assert dc.proxy_env is None
    assert dc.access_strategies == [("backoff", {})]


def test_bad_strategy_item_raises(tmp_path):
    # a strategy list item that is neither a bare string nor a single-key dict
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "domains:\n"
        "  - domain: a.com\n"
        "    access_strategies:\n"
        "      - [not, a, mapping]\n"
    )
    with pytest.raises(ValueError):
        load_config(p)


def test_bad_strategy_multikey_dict_raises(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "domains:\n"
        "  - domain: a.com\n"
        "    access_strategies:\n"
        "      - {one: {}, two: {}}\n"
    )
    with pytest.raises(ValueError):
        load_config(p)


def test_is_authorized_and_domain_config_case_insensitive(cfg):
    # All three auth predicates must agree: is_authorized / domain_config must
    # normalize host the same way assert_authorized does (M1 regression).
    assert cfg.is_authorized("BOOKS.TOSCRAPE.COM") is True
    assert cfg.is_authorized("Books.ToScrape.Com:443") is True
    assert cfg.domain_config("BOOKS.TOSCRAPE.COM").max_rps == 1.0


def test_scalar_access_strategies_rejected(tmp_path):
    # `access_strategies: backoff` (scalar, not a list) must error, not iterate
    # per-character into [('b',{}),('a',{}),...] (M2 regression).
    p = tmp_path / "cfg.yaml"
    p.write_text("domains:\n  - domain: a.com\n    access_strategies: backoff\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_domain_entry_missing_domain_key_raises(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("domains:\n  - max_rps: 2.0\n")
    with pytest.raises(ValueError):
        load_config(p)
