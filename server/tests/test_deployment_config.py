"""Every setting must be documented *and* reachable inside the container.

This exists because of a real failure: identity settings were added, tested, and documented
in `.env.example`, and the whole suite passed — but `compose.yaml` maps variables into the
container one by one, so a deployed instance never saw them and the first administrator was
silently never created. No unit test can catch that; the gap is between two files.

Three rules, checked against the files themselves:

1. every setting appears in `.env.example` — otherwise operators cannot know it exists;
2. every `SE_*` variable the compose files mention is a real setting — catches typos and
   settings that were renamed or removed;
3. every operator-configurable setting is passed into the API container.
"""

from __future__ import annotations

import re
from pathlib import Path

from store_everything.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
COMPOSE = REPO_ROOT / "compose.yaml"
COMPOSE_DEV = REPO_ROOT / "compose.dev.yaml"

#: Settings the container deliberately does not take from `.env`, with the reason.
NOT_OPERATOR_CONFIGURABLE = {
    # The port is fixed inside the image: the healthcheck and the Traefik labels address
    # it directly, so making it configurable would only let the three disagree.
    "SE_PORT": "fixed by the image's healthcheck and proxy labels",
}

_ENV_REFERENCE = re.compile(r"\bSE_[A-Z0-9_]+\b")


def _setting_names() -> set[str]:
    return {f"SE_{name.upper()}" for name in Settings.model_fields}


def _documented() -> set[str]:
    return set(_ENV_REFERENCE.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))


def _mentioned_in_compose() -> set[str]:
    text = COMPOSE.read_text(encoding="utf-8") + COMPOSE_DEV.read_text(encoding="utf-8")
    return set(_ENV_REFERENCE.findall(text))


def test_every_setting_is_documented_in_the_example_environment() -> None:
    missing = _setting_names() - _documented()

    assert missing == set(), f"undocumented settings: {sorted(missing)}"


def test_the_compose_files_reference_only_real_settings() -> None:
    unknown = _mentioned_in_compose() - _setting_names()

    assert unknown == set(), f"compose references settings that do not exist: {sorted(unknown)}"


def test_every_operator_configurable_setting_reaches_the_container() -> None:
    """The rule the bootstrap bug broke: documented in `.env` but never passed through."""
    expected = _setting_names() - set(NOT_OPERATOR_CONFIGURABLE)

    unreachable = expected - _mentioned_in_compose()

    assert unreachable == set(), (
        f"these settings are documented but never passed into the container: {sorted(unreachable)}"
    )


def test_the_exception_list_stays_honest() -> None:
    """An exception for a setting that no longer exists hides the next real gap."""
    stale = set(NOT_OPERATOR_CONFIGURABLE) - _setting_names()

    assert stale == set(), f"stale exceptions: {sorted(stale)}"
