"""The process entrypoint, in particular its proxy-trust policy."""

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

    __main__.main()

    assert captured_uvicorn["app"] == "store_everything.app:create_app"
    assert captured_uvicorn["factory"] is True
    # Our own structured access log replaces uvicorn's.
    assert captured_uvicorn["access_log"] is False


def test_forwarded_headers_are_distrusted_by_default(
    captured_uvicorn: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SE_DATABASE_URL", "postgresql://u:p@db/store")
    monkeypatch.delenv("SE_FORWARDED_ALLOW_IPS", raising=False)

    __main__.main()

    assert captured_uvicorn["proxy_headers"] is False
    assert captured_uvicorn["forwarded_allow_ips"] is None


def test_forwarded_headers_are_trusted_only_from_configured_proxies(
    captured_uvicorn: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SE_DATABASE_URL", "postgresql://u:p@db/store")
    monkeypatch.setenv("SE_FORWARDED_ALLOW_IPS", "172.18.0.2")

    __main__.main()

    assert captured_uvicorn["proxy_headers"] is True
    assert captured_uvicorn["forwarded_allow_ips"] == "172.18.0.2"
