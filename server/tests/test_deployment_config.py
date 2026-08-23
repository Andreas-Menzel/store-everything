"""Every setting must be documented *and* reachable inside the container.

This exists because of a real failure: identity settings were added, tested, and documented
in `.env.example`, and the whole suite passed — but `compose.yaml` maps variables into the
container one by one, so a deployed instance never saw them and the first administrator was
silently never created. No unit test can catch that; the gap is between two files.

Three rules, checked against the files themselves:

1. every setting appears in `.env.example` — otherwise operators cannot know it exists;
2. every `SE_*` variable the compose files pass to a **core** service is a real setting —
   catches typos and settings that were renamed or removed;
3. every operator-configurable setting is passed into the API container.

Rule 2 is about the core's own services. An extractor container is a different program with a
different environment (`SE_CORE_URL`, `SE_EXTRACTOR_TOKEN` — ADR-0020), so the check reads the
service blocks rather than the file's raw text: an extractor's variables are not the core's
settings, and pretending otherwise would either fail on every extractor or stop catching typos.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

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

#: Which services run the core's image, and therefore read the core's settings. Named rather
#: than inferred: a new service that belongs on this list is a decision, and a new *extractor*
#: service must not silently start being held to the core's vocabulary.
CORE_SERVICES = ("api", "orchestrator", "migrations")


def _setting_names() -> set[str]:
    return {f"SE_{name.upper()}" for name in Settings.model_fields}


def _documented() -> set[str]:
    return set(_ENV_REFERENCE.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))


def _core_environments() -> list[tuple[str, object]]:
    """Every environment entry of every core service, across both compose files.

    Accumulated rather than merged per service: the development file *overrides* the `api`
    service's environment block, so merging service dictionaries would drop the production
    variables and make this check pass for the wrong reason.
    """
    entries: list[tuple[str, object]] = []
    for path in (COMPOSE, COMPOSE_DEV):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, service in (document.get("services") or {}).items():
            if name not in CORE_SERVICES:
                continue
            environment = (service or {}).get("environment") or {}
            if isinstance(environment, dict):
                entries.extend(environment.items())
    return entries


def _mentioned_in_compose() -> set[str]:
    """Every `SE_*` name the core's services carry — keys and interpolated values alike.

    Both sides matter: the key is what the process reads, and the value is what the operator's
    `.env` has to provide, so a typo in either is the same class of silent gap.
    """
    found: set[str] = set()
    for key, value in _core_environments():
        found.update(_ENV_REFERENCE.findall(f"{key} {value}"))
    return found


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


CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github/workflows/release.yml"

#: Everywhere the image is built. Three files, one context — and nothing but a test keeps them
#: agreeing, which is how a build that works locally reaches CI broken.
IMAGE_BUILDS = (COMPOSE, CI_WORKFLOW, RELEASE_WORKFLOW)

#: A build context pointing at the service directory. That was right until the image started
#: carrying the web client too (10 § topology): the client lives outside `server/`, so such a
#: context cannot see it and the build fails on a `COPY` — in CI for the first one, and at the
#: next tag for the release.
_SERVER_AS_CONTEXT = re.compile(r"context:\s*\.?/?server\b|docker build[^\n]*\sserver/?\s*$", re.M)


def test_every_place_that_builds_the_image_uses_the_repository_root() -> None:
    # The detector first, against the line this test exists because of: a gate that cannot
    # recognise the failure it is named for is decoration.
    assert _SERVER_AS_CONTEXT.search("        run: docker build -t core:ci server/")
    assert _SERVER_AS_CONTEXT.search("          context: ./server")

    for path in IMAGE_BUILDS:
        text = path.read_text(encoding="utf-8")
        assert "server/Dockerfile" in text, f"{path.name} does not name the Dockerfile explicitly"
        offending = _SERVER_AS_CONTEXT.search(text)
        assert offending is None, (
            f"{path.name} builds from the service directory ({offending.group(0).strip()!r}), "
            "which cannot see the web client"
        )


#: The mounts that hold a user's own bytes, and the variable each one is switched with. Read by
#: Docker rather than by the service, so they carry no `SE_` prefix — and so no other test here
#: covers them.
SWITCHABLE_MOUNTS = {
    "/srv/store-everything": "WORKSPACE_DATA",
    "/var/lib/store-everything": "APP_DATA",
}


def test_the_areas_holding_files_can_be_put_on_the_operators_own_storage() -> None:
    """Files nobody can find without the app are not really theirs (ADR-0003).

    The default is a named volume so the stack works unconfigured, but every mount of an area
    that holds bytes has to be switchable to a host path — and a mount that hard-codes the volume
    would take that away silently. Checked as text because it is the compose file's literal
    contents that decide, not a rendering of it.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    for path, variable in SWITCHABLE_MOUNTS.items():
        mounts = [line.strip() for line in text.splitlines() if line.strip().endswith(f":{path}")]
        assert mounts, f"nothing mounts {path} any more"
        for mount in mounts:
            assert f"${{{variable}:-" in mount, (
                f"{mount} pins its source; it should default through ${variable}"
            )
