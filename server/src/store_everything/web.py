"""Serving the built web client from the API's own origin.

[10 § topology](../../../specs/10-deployment-and-operations.md#topology): one image, one origin.
That is not a packaging convenience — it is why the session cookie is same-site by construction
and why the client needs no CORS entry, so the app and the API cannot drift onto different hosts
without someone deciding to make them
([F-027/FR-1](../../../features/F-027-web-application-shell.md)).

Three rules, and each one is a decision rather than a default:

1. **API paths answer for themselves.** This is a fallback under everything `/api/v1` does not
   claim, and an unknown API path still gets the API's `404` in `problem+json` — not an HTML
   document that a client would try to parse as data.
2. **The entry document is never cached; the assets always are.** Vite fingerprints every asset,
   so `immutable` is the truth for those and would be a trap for `index.html` — a deploy has to
   be visible on the next reload, without asking anyone to purge anything.
3. **The documents carry the app's security policy** (FR-2). It lives here rather than in the
   global middleware because it is about *this* origin's documents: the API's JSON needs no
   policy, and the file-content endpoint has its own, stricter one. Three policies, three
   purposes, none of them overlapping.

A missing build directory is an ordinary state. The development container runs the client from
Vite, so the API says so once and serves itself.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse

from store_everything import filestore
from store_everything.problems import ProblemException

_logger = logging.getLogger(__name__)

#: Where Vite writes fingerprinted files. Everything under here is immutable by construction.
ASSETS = "assets"

ENTRY_DOCUMENT = "index.html"

#: Paths this service answers itself. Listed rather than derived: "which prefixes are the API's"
#: is a fact about the contract (08 § endpoint map), and a fallback that guessed it wrong would
#: answer an API call with an HTML page.
API_PREFIXES = ("api/", "healthz", "readyz")

#: A year, which is what `immutable` means in practice for a fingerprinted file.
IMMUTABLE = "public, max-age=31536000, immutable"

#: The app origin's policy (F-027/FR-2). Scripts from this origin only — no `unsafe-eval`, which
#: is the line a documentation viewer does not get to move — nothing from a third-party host, so
#: an instance with no egress at all works, and no framing, plugins or cross-origin form posts.
#:
#: `style-src` allows inline styles and only inline styles: Vue writes `style` attributes for
#: bound styles and transitions, and every component library injects a stylesheet at runtime.
#: Refusing that would break the app in ways nothing reports.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "media-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)


def _is_api_path(relative: str) -> bool:
    return any(relative == prefix or relative.startswith(prefix) for prefix in API_PREFIXES)


def _headers(*, cache: str) -> dict[str, str]:
    return {"cache-control": cache, "content-security-policy": CONTENT_SECURITY_POLICY}


def install(app: FastAPI, root: Path) -> bool:
    """Serve the client under every path the API does not claim. Whether there was one to serve.

    Registered last, so route matching reaches this only after every API route has declined.
    """
    entry = root / ENTRY_DOCUMENT
    if not entry.is_file():
        _logger.info(
            "no web client to serve; the API is serving itself only",
            extra={"web_root": str(root)},
        )
        return False

    async def client(request: Request, path: str) -> Response:
        """A file if the build has one at that path, otherwise the app's entry document.

        The fallback is what makes a deep link survive a reload: the router that understands
        `/folders/{id}` lives in the document, so the server's job is to hand it over.
        """
        if _is_api_path(path):
            # Only reachable when no API route matched, and the answer to that is the API's.
            raise ProblemException(status=404, slug="not-found", title="Not found")

        if path and not path.endswith("/"):
            try:
                # The same containment rule the file store uses, for the same reason: a path
                # from a request is never trusted to stay inside the directory it names.
                candidate = filestore.resolve_within(root, Path(path))
            except (filestore.ContainmentError, ValueError):
                raise ProblemException(status=404, slug="not-found", title="Not found") from None
            if candidate.is_file():
                cache = IMMUTABLE if path.startswith(f"{ASSETS}/") else "no-store"
                return FileResponse(candidate, headers=_headers(cache=cache))

        return FileResponse(entry, headers=_headers(cache="no-store"))

    # Registered by hand rather than by decorator: the fallback must be the last route added,
    # and `include_in_schema=False` keeps it out of the contract the client is generated from.
    app.add_api_route(
        "/{path:path}", client, methods=["GET"], include_in_schema=False, response_model=None
    )
    _logger.info("serving the web client", extra={"web_root": str(root)})
    return True
