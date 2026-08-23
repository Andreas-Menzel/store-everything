import { appendToUpload, createUpload, uploadOffset } from '@store-everything/api-client';

import { toFailure, type Failure } from '@/shared';

/**
 * The upload protocol, from the browser's side
 * ([ADR-0017](../../../../decisions/ADR-0017-resumable-upload-protocol.md),
 * [F-001/FR-2](../../../../features/F-001-upload-and-import.md)).
 *
 * Two shapes, and the difference is not a nicety. A file that fits in one request is one request:
 * `POST` with `Upload-Complete: ?1` and it is done. A larger one is created **incomplete**, then
 * appended to in chunks — each `PATCH` naming the offset it starts at, so a connection that dies
 * mid-transfer costs one chunk rather than the whole file. Resuming asks the server where it got
 * to (`HEAD`) rather than trusting anything this side remembers: the committed offset is the
 * server's fact, and after a browser reload it is the only copy of it.
 *
 * The interop version header is part of the draft's handshake and is sent on every request that
 * carries body bytes.
 */

/** The draft revision this client is written against — ADR-0017 pins it deliberately. */
export const INTEROP_VERSION = 9;

/** Bytes per append. Well inside the server's default ceiling (`max-append-size`, 64 MiB), and
 * small enough that a dropped connection costs little. Fixed, not negotiated: the server
 * publishes its own limits through `OPTIONS`, and reading them before starting is still to do. */
export const CHUNK_BYTES = 8 * 1024 * 1024;

/** Anything smaller goes in the creating request: a second round trip buys nothing. */
export const SINGLE_REQUEST_LIMIT = CHUNK_BYTES;

export interface Progress {
  /** Bytes the server has committed. */
  sent: number;
  total: number;
}

export interface UploadHandle {
  /** Set once the upload exists server-side, so an interrupted transfer can be resumed. */
  id?: string;
}

export class UploadFailed extends Error {
  constructor(readonly failure: Failure) {
    super(failure.title);
  }
}

function boolean(value: boolean): string {
  // Structured-field booleans: `?1` and `?0`, not `true` and `false`.
  return value ? '?1' : '?0';
}

/**
 * The protocol headers this client adds — and deliberately **not** `content-type`.
 *
 * The generated SDK carries the right one per operation: `application/octet-stream` when
 * creating an upload, `application/partial-upload` on an append, which the server requires and
 * answers `415` without. Setting one here overrode both, because these headers are spread over
 * the operation's defaults and header names are case-insensitive — so every upload larger than
 * one chunk failed, while every upload smaller than one chunk (the only size any test used)
 * went through the creating request and passed.
 */
function protocolHeaders(complete: boolean, offset?: number): Record<string, string> {
  const headers: Record<string, string> = {
    'upload-draft-interop-version': String(INTEROP_VERSION),
    'upload-complete': boolean(complete),
  };
  if (offset !== undefined) headers['upload-offset'] = String(offset);
  return headers;
}

/**
 * Send a file to a workspace path, resuming an interrupted attempt if one is handed in.
 *
 * `onProgress` is called with what the *server* has committed rather than what this code has
 * handed to the network — the difference is exactly what a resume relies on.
 */
export async function upload(
  workspaceId: string,
  path: string,
  file: File,
  options: { handle?: UploadHandle; onProgress?: (progress: Progress) => void } = {},
): Promise<void> {
  const handle = options.handle ?? {};
  const report = (sent: number): void => options.onProgress?.({ sent, total: file.size });

  let offset = handle.id ? await committedOffset(handle.id) : 0;
  report(offset);

  if (handle.id === undefined) {
    const single = file.size <= SINGLE_REQUEST_LIMIT;
    const first = file.slice(0, single ? file.size : CHUNK_BYTES);
    const { error, response } = await createUpload({
      path: { workspace_id: workspaceId },
      query: { path },
      body: first,
      headers: protocolHeaders(single),
      bodySerializer: null,
    });
    if (error !== undefined) throw new UploadFailed(toFailure(error, response?.status));
    offset = first.size;
    report(offset);
    if (single) return;
    // The protocol announces the upload resource in `Location`, which is why it is read from
    // there rather than from the body: an incomplete creation's body is not part of the
    // generated contract, and the header is.
    handle.id = uploadIdFrom(response?.headers.get('location'));
  }

  const id = handle.id;
  while (offset < file.size) {
    const chunk = file.slice(offset, Math.min(offset + CHUNK_BYTES, file.size));
    const last = offset + chunk.size >= file.size;
    const { error, response } = await appendToUpload({
      path: { upload_id: id },
      body: chunk,
      headers: protocolHeaders(last, offset),
      bodySerializer: null,
    });
    if (error !== undefined) throw new UploadFailed(toFailure(error, response?.status));
    offset += chunk.size;
    report(offset);
  }
}

function uploadIdFrom(location: string | null | undefined): string {
  const id = location?.split('/').pop();
  if (!id) {
    throw new UploadFailed({
      title: 'The server did not say where to continue this upload',
      fields: [],
    });
  }
  return id;
}

/** Where the server got to. The only trustworthy answer after an interruption. */
export async function committedOffset(uploadId: string): Promise<number> {
  const { error, response } = await uploadOffset({ path: { upload_id: uploadId } });
  if (error !== undefined) throw new UploadFailed(toFailure(error, response?.status));
  const reported = response?.headers.get('upload-offset');
  return reported === null || reported === undefined ? 0 : Number(reported);
}
