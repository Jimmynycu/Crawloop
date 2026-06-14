"""Config loader and the hard domain allowlist.

``authorized_domains.yaml`` is the operator's explicit list of sites this POC is
permitted to crawl, plus per-domain crawl policy (rate limit, JS rendering, and
the ordered access strategies tried when a block is hit). The allowlist is a
hard gate: every fetch must pass through :meth:`AppConfig.assert_authorized`, so
a URL whose host is not on the list can never be requested. ``respect_robots``
defaults OFF for the POC (targets are owned/authorized) and is a one-line flip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml


class UnauthorizedDomain(Exception):
    """Raised when a domain/URL is not on the authorized allowlist."""


def _norm_host(host: str) -> str:
    """Canonical host key: lowercased, port stripped. Used everywhere a domain is
    matched against the allowlist so all three auth predicates agree."""
    return (host or "").strip().lower().partition(":")[0]


def _normalize_strategies(raw: object) -> list[tuple[str, dict]]:
    """Normalize the YAML ``access_strategies`` list into ``(kind, params)`` pairs.

    Each item is either a bare string ``s`` -> ``(s, {})`` or a single-key
    mapping ``{kind: params}`` -> ``(kind, params or {})``. Anything else (a
    multi-key mapping, a number, ...) is a configuration error. ``raw`` itself
    must be a list: a scalar like ``access_strategies: backoff`` would otherwise
    iterate per-character and silently corrupt the strategy list.
    """
    if not isinstance(raw, list):
        raise ValueError(
            f"access_strategies must be a list, got {type(raw).__name__}: {raw!r}"
        )
    strategies: list[tuple[str, dict]] = []
    for item in raw:
        if isinstance(item, str):
            strategies.append((item, {}))
        elif isinstance(item, dict) and len(item) == 1:
            kind, params = next(iter(item.items()))
            strategies.append((kind, params or {}))
        else:
            raise ValueError(
                f"invalid access_strategies entry {item!r}: expected a string or a "
                "single-key mapping {kind: params}"
            )
    return strategies


@dataclass
class DomainConfig:
    domain: str
    max_rps: float = 1.0
    render_js: bool = False
    note: str | None = None
    access_strategies: list[tuple[str, dict]] = field(
        default_factory=lambda: [("backoff", {})]
    )
    proxy_env: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> DomainConfig:
        if "domain" not in raw:
            raise ValueError(f"domain entry missing required 'domain' key: {raw!r}")
        strategies_raw = raw.get("access_strategies")
        kwargs: dict = {
            "domain": _norm_host(raw["domain"]),
            "max_rps": raw.get("max_rps", 1.0),
            "render_js": raw.get("render_js", False),
            "note": raw.get("note"),
            "proxy_env": raw.get("proxy_env"),
        }
        # Only override the default factory when the key is actually present, so
        # a domain with no access_strategies keeps the sensible [("backoff", {})].
        if strategies_raw is not None:
            kwargs["access_strategies"] = _normalize_strategies(strategies_raw)
        return cls(**kwargs)


@dataclass
class AppConfig:
    respect_robots: bool
    domains: dict[str, DomainConfig]

    def is_authorized(self, domain: str) -> bool:
        return _norm_host(domain) in self.domains

    def domain_config(self, domain: str) -> DomainConfig:
        try:
            return self.domains[_norm_host(domain)]
        except KeyError:
            raise UnauthorizedDomain(domain) from None

    def assert_authorized(self, url: str) -> None:
        host = _norm_host(urlparse(url).hostname or "")
        if not self.is_authorized(host):
            raise UnauthorizedDomain(f"{host!r} (from {url!r}) is not authorized")


def load_config(path: str | Path) -> AppConfig:
    data = yaml.safe_load(Path(path).read_text()) or {}
    domains = {
        dc.domain: dc
        for dc in (DomainConfig.from_dict(d) for d in data.get("domains", []))
    }
    return AppConfig(respect_robots=bool(data.get("respect_robots", False)), domains=domains)
