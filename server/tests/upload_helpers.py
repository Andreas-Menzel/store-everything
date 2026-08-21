"""Driving the resumable-upload protocol from a test, one header at a time.

Deliberately explicit about headers: the point of these tests is the wire format, so a helper
that hid `Upload-Complete` or `Upload-Offset` would hide the thing under test.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from store_everything import resumable
from store_everything.api.v1.router import API_V1_PREFIX
from tests.identity_helpers import SAME_ORIGIN

#: The interop version this implementation is written against (ADR-0017).
CURRENT_INTEROP = 9


def files_url(workspace_id: UUID) -> str:
    return f"{API_V1_PREFIX}/workspaces/{workspace_id}/files"


def upload_url(upload_id: UUID | str) -> str:
    return f"{API_V1_PREFIX}/uploads/{upload_id}"


async def create_upload(
    client: httpx.AsyncClient,
    workspace_id: UUID,
    path: str,
    *,
    body: bytes = b"",
    complete: bool | None = True,
    length: int | None = None,
    interop: int | None = CURRENT_INTEROP,
    content_hash: str | None = None,
    content_type: str | None = None,
) -> httpx.Response:
    """Create an upload. `complete=None` omits the header entirely — a plain upload."""
    headers: dict[str, str] = dict(SAME_ORIGIN)
    if complete is not None:
        headers[resumable.COMPLETE_HEADER] = resumable.boolean(complete)
    if interop is not None:
        headers[resumable.INTEROP_VERSION_HEADER] = str(interop)
    if length is not None:
        headers[resumable.LENGTH_HEADER] = str(length)
    if content_type is not None:
        headers["Content-Type"] = content_type

    params: dict[str, Any] = {"path": path}
    if content_hash is not None:
        params["content_hash"] = content_hash
    return await client.post(files_url(workspace_id), params=params, content=body, headers=headers)


async def append(
    client: httpx.AsyncClient,
    upload_id: UUID | str,
    offset: int | None,
    body: bytes,
    *,
    complete: bool = False,
    content_type: str | None = resumable.MEDIA_TYPE,
) -> httpx.Response:
    headers: dict[str, str] = {
        **SAME_ORIGIN,
        resumable.COMPLETE_HEADER: resumable.boolean(complete),
    }
    if content_type is not None:
        headers["Content-Type"] = content_type
    if offset is not None:
        headers[resumable.OFFSET_HEADER] = str(offset)
    return await client.patch(upload_url(upload_id), content=body, headers=headers)


async def offset_of(client: httpx.AsyncClient, upload_id: UUID | str) -> httpx.Response:
    return await client.head(upload_url(upload_id))


async def cancel(client: httpx.AsyncClient, upload_id: UUID | str) -> httpx.Response:
    return await client.delete(upload_url(upload_id), headers=SAME_ORIGIN)
