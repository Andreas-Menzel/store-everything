"""RFC 9457 `application/problem+json` — one error envelope for the whole API.

Rules from 08-api-principles.md § errors:

- field-level validation returns *all* problems at once, each with an RFC 6901 pointer
  whose first segment names the location (`body` / `query` / `path` / `header`);
- the violated rule is echoed, **never the submitted value**;
- nothing internal leaks — no stack traces, no SQL, no dependency error strings;
- `instance` is the request id, the only bridge to the server-side log line.

Unexpected exceptions are turned into `500` problems by `RequestContextMiddleware`
rather than by a handler registered here: Starlette's server-error layer sits *outside*
the middleware stack, where the request-id context has already been torn down.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

from store_everything.log import request_id_var

PROBLEM_TYPE_BASE = "https://docs.store-everything.example/errors/"
PROBLEM_MEDIA_TYPE = "application/problem+json"

# Status codes we name explicitly; anything else falls back to a generic type.
_STATUS_TYPES: Mapping[int, tuple[str, str]] = {
    400: ("malformed-request", "Malformed request"),
    401: ("authentication-required", "Authentication required"),
    403: ("forbidden", "Forbidden"),
    404: ("not-found", "Not found"),
    405: ("method-not-allowed", "Method not allowed"),
    409: ("conflict", "Conflict"),
    410: ("gone", "Gone"),
    422: ("validation", "Validation failed"),
    503: ("service-not-ready", "Service not ready"),
}


def problem_type(slug: str) -> str:
    return f"{PROBLEM_TYPE_BASE}{slug}"


@dataclass(frozen=True, slots=True)
class FieldProblem:
    """One field-level validation failure."""

    detail: str
    pointer: str


class ProblemException(Exception):  # noqa: N818 - named for the envelope it produces
    """Raise to return a problem response with an explicit type."""

    def __init__(
        self,
        *,
        status: int,
        slug: str,
        title: str,
        detail: str | None = None,
        errors: Sequence[FieldProblem] = (),
        headers: Mapping[str, str] | None = None,
        type_uri: str | None = None,
    ) -> None:
        super().__init__(f"{status} {title}")
        self.status = status
        self.slug = slug
        self.title = title
        self.detail = detail
        self.errors = tuple(errors)
        self.headers = dict(headers) if headers else None
        self.type_uri = type_uri
        """A registered type URI to use instead of ours.

        The one case is a wire protocol whose own problem types are registered with IANA
        (ADR-0017): a client that recognises `mismatching-upload-offset` recognises *that*
        URI, and interoperating with clients we did not write outranks keeping one namespace
        tidy. The envelope's shape is unchanged either way (08 § errors).
        """


def problem_response(
    *,
    status: int,
    slug: str,
    title: str,
    detail: str | None = None,
    errors: Sequence[FieldProblem] = (),
    headers: Mapping[str, str] | None = None,
    type_uri: str | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": type_uri if type_uri is not None else problem_type(slug),
        "title": title,
        "status": status,
    }
    if detail is not None:
        body["detail"] = detail

    request_id = request_id_var.get()
    if request_id is not None:
        body["instance"] = request_id

    if errors:
        body["errors"] = [{"detail": e.detail, "pointer": e.pointer} for e in errors]

    return JSONResponse(
        body,
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=dict(headers) if headers else None,
    )


def internal_error_response() -> JSONResponse:
    return problem_response(
        status=500,
        slug="internal",
        title="Internal server error",
        detail="The request could not be completed. Quote the instance id when reporting it.",
    )


def _pointer(location: Sequence[str | int]) -> str:
    """Build an RFC 6901 pointer from a Pydantic error location."""
    if not location:
        return "/body"
    return "/" + "/".join(str(part) for part in location)


async def _handle_problem(_request: Request, exc: Exception) -> Response:
    problem = cast(ProblemException, exc)
    return problem_response(
        status=problem.status,
        slug=problem.slug,
        title=problem.title,
        detail=problem.detail,
        errors=problem.errors,
        headers=problem.headers,
        type_uri=problem.type_uri,
    )


async def _handle_http_exception(_request: Request, exc: Exception) -> Response:
    http_exc = cast(StarletteHTTPException, exc)
    slug, title = _STATUS_TYPES.get(http_exc.status_code, ("request-failed", "Request failed"))
    # FastAPI's HTTPException widens `detail` to Any at runtime; RFC 9457 requires a string,
    # so anything else is dropped rather than serialised into a malformed envelope.
    raw_detail = cast(object, http_exc.detail)
    detail = raw_detail if isinstance(raw_detail, str) and raw_detail != title else None
    return problem_response(
        status=http_exc.status_code,
        slug=slug,
        title=title,
        detail=detail,
        headers=http_exc.headers,
    )


async def _handle_validation_error(_request: Request, exc: Exception) -> Response:
    validation_exc = cast(RequestValidationError, exc)
    # Deliberately drops each error's `input`: the submitted value is never reflected back.
    errors = [
        FieldProblem(detail=str(item.get("msg", "invalid")), pointer=_pointer(item.get("loc", ())))
        for item in validation_exc.errors()
    ]
    slug, title = _STATUS_TYPES[422]
    return problem_response(
        status=422,
        slug=slug,
        title=title,
        detail=f"{len(errors)} request field(s) are invalid.",
        errors=errors,
    )


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProblemException, _handle_problem)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
