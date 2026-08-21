"""The process entrypoint: its subcommands, and its proxy-trust policy."""

from __future__ import annotations

from typing import Any

import pytest

from store_everything import __main__


@pytest.fixture
def captured_uvicorn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_run(app: str, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(__main__.uvicorn, "run", fake_run)
    return captured


def test_serves_the_app_factory(
    captured_uvicorn: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SE_DATABASE_URL", "postgresql://u:p@db/store")

    __main__.main([])

    assert captured_uvicorn["app"] == "store_everything.app:create_app"
    assert captured_uvicorn["factory"] is True
    # Our own structured access log replaces uvicorn's.
    assert captured_uvicorn["access_log"] is False


def test_forwarded_headers_are_distrusted_by_default(
    captured_uvicorn: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SE_DATABASE_URL", "postgresql://u:p@db/store")
    monkeypatch.delenv("SE_FORWARDED_ALLOW_IPS", raising=False)

    __main__.main([])

    assert captured_uvicorn["proxy_headers"] is False
    assert captured_uvicorn["forwarded_allow_ips"] is None


def test_forwarded_headers_are_trusted_only_from_configured_proxies(
    captured_uvicorn: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SE_DATABASE_URL", "postgresql://u:p@db/store")
    monkeypatch.setenv("SE_FORWARDED_ALLOW_IPS", "172.18.0.2")

    __main__.main([])

    assert captured_uvicorn["proxy_headers"] is True
    assert captured_uvicorn["forwarded_allow_ips"] == "172.18.0.2"


class _Prompts:
    """Stands in for `getpass`, which cannot be typed into from a test."""

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)

    def __call__(self, prompt: str = "") -> str:
        return self._answers.pop(0)


@pytest.mark.integration
def test_create_admin_creates_the_first_administrator(
    identity_database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SE_DATABASE_URL", identity_database)
    monkeypatch.setattr(
        __main__.getpass, "getpass", _Prompts("a-long-enough-password", "a-long-enough-password")
    )

    status = __main__.main(["create-admin", "Boss@Example.com"])

    assert status == 0
    assert "boss@example.com" in capsys.readouterr().out


@pytest.mark.integration
def test_create_admin_refuses_when_accounts_already_exist(
    identity_database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SE_DATABASE_URL", identity_database)
    monkeypatch.setattr(
        __main__.getpass, "getpass", _Prompts("a-long-enough-password", "a-long-enough-password")
    )
    assert __main__.main(["create-admin", "first@example.com"]) == 0
    capsys.readouterr()

    monkeypatch.setattr(
        __main__.getpass, "getpass", _Prompts("a-long-enough-password", "a-long-enough-password")
    )
    status = __main__.main(["create-admin", "second@example.com"])

    assert status == 1
    assert "already has accounts" in capsys.readouterr().err


def test_create_admin_refuses_mismatched_passwords(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SE_DATABASE_URL", "postgresql://u:p@db/store")
    monkeypatch.setattr(__main__.getpass, "getpass", _Prompts("one-long-password", "another-one"))

    status = __main__.main(["create-admin", "boss@example.com"])

    assert status == 1
    assert "do not match" in capsys.readouterr().err


def test_create_admin_refuses_a_weak_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Checked before the database is touched, so a typo costs nothing."""
    monkeypatch.setenv("SE_DATABASE_URL", "postgresql://u:p@db/store")
    monkeypatch.setattr(__main__.getpass, "getpass", _Prompts("short", "short"))

    status = __main__.main(["create-admin", "boss@example.com"])

    assert status == 1
    assert "at least" in capsys.readouterr().err
