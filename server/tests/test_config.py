"""Configuration: driver normalisation, secret handling, proxy-trust policy."""

from __future__ import annotations

import pytest

from store_everything.config import load_settings
from tests.conftest import make_settings


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
        ("postgres://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
        ("postgresql+psycopg://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
        # Anything that isn't a PostgreSQL URL is passed through untouched rather than
        # rewritten into something the operator never asked for.
        ("mysql://u:p@host/db", "mysql://u:p@host/db"),
        ("not-a-url", "not-a-url"),
    ],
)
def test_dsn_is_normalised_to_the_configured_driver(given: str, expected: str) -> None:
    assert make_settings(database_url=given).sqlalchemy_url == expected


def test_password_is_not_exposed_by_repr() -> None:
    settings = make_settings(database_url="postgresql://user:hunter2@host/db")

    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings)
    # ...but the application can still reach it deliberately.
    assert "hunter2" in settings.sqlalchemy_url


def test_proxy_headers_are_distrusted_by_default() -> None:
    assert make_settings().trust_proxy_headers is False


def test_proxy_headers_are_trusted_only_when_addresses_are_configured() -> None:
    assert make_settings(forwarded_allow_ips="10.0.0.1").trust_proxy_headers is True
    assert make_settings(forwarded_allow_ips="   ").trust_proxy_headers is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        ("   ", ()),
        ("https://a.example", ("https://a.example",)),
        ("https://a.example, https://b.example", ("https://a.example", "https://b.example")),
    ],
)
def test_origin_lists_are_read_as_comma_separated_env_values(
    raw: str, expected: tuple[str, ...]
) -> None:
    """An operator writes `a,b` in `.env`, not a JSON array — and empty must mean none."""
    assert make_settings(cors_allow_origins=raw).cors_allow_origins == expected


def test_defaults_are_the_safe_ones() -> None:
    settings = make_settings()

    assert settings.host == "127.0.0.1"
    assert settings.cors_allow_origins == ()


# Settings constructed directly take a different code path from settings read out of the
# environment: pydantic-settings decodes raw values before any validator runs. Only the
# environment path is what a deployed container actually exercises.


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("SE_DATABASE_URL", "postgresql://user:secret@db:5432/store")
    for name in ("SE_CORS_ALLOW_ORIGINS", "SE_FORWARDED_ALLOW_IPS", "SE_APP_ENV"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_an_empty_origin_list_in_the_environment_means_none(
    environment: pytest.MonkeyPatch,
) -> None:
    """`SE_CORS_ALLOW_ORIGINS=` is how an operator writes "no origins"; it must not
    crash the service at start-up."""
    environment.setenv("SE_CORS_ALLOW_ORIGINS", "")

    assert load_settings().cors_allow_origins == ()


def test_origins_are_read_from_the_environment_as_a_list(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("SE_CORS_ALLOW_ORIGINS", "https://a.example, https://b.example")

    assert load_settings().cors_allow_origins == ("https://a.example", "https://b.example")


def test_the_environment_supplies_the_required_database_url(
    environment: pytest.MonkeyPatch,
) -> None:
    assert load_settings().sqlalchemy_url.startswith("postgresql+psycopg://")
