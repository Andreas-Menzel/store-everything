import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CHUNK_BYTES, INTEROP_VERSION, upload, UploadFailed } from './resumable';

/**
 * The upload client, chunk by chunk
 * ([F-001/FR-2, FR-14](../../../../features/F-001-upload-and-import.md)).
 *
 * This module had no tests, and the one bug that mattered was invisible without them: it sent
 * `content-type: application/octet-stream` on appends, which the server answers `415`, so **every
 * upload larger than one chunk failed**. Nothing caught it because no test anywhere uploaded more
 * than one chunk — a file at or below `CHUNK_BYTES` goes in the creating request and never
 * appends — and the browser suite stubs the API. So these assert the wire: which requests are
 * made, with which headers, in which order.
 *
 * The bytes are faked rather than allocated. A real multi-chunk `File` is 16 MiB of memory per
 * test for no gain: what is under test is the slicing and the headers, and `slice` is the
 * browser's.
 */

const { appendToUpload, createUpload, uploadOffset } = vi.hoisted(() => ({
  appendToUpload: vi.fn(),
  createUpload: vi.fn(),
  uploadOffset: vi.fn(),
}));

vi.mock('@store-everything/api-client', () => ({ appendToUpload, createUpload, uploadOffset }));

const UPLOAD_ID = '01a02b3c-4d5e-7000-8000-0123456789ab';

/** A file of `size` bytes whose slices report their own size — enough for the protocol. */
function fileOf(size: number, name = 'video.mp4'): File {
  const slice = (start = 0, end = size): Blob => ({ size: Math.max(0, end - start) }) as Blob;
  return { name, size, slice } as unknown as File;
}

function created(location = `/api/v1/uploads/${UPLOAD_ID}`): unknown {
  return { error: undefined, response: { headers: new Headers({ location }) } };
}

function accepted(offset?: number): unknown {
  const headers =
    offset === undefined ? new Headers() : new Headers({ 'upload-offset': `${offset}` });
  return { error: undefined, response: { headers } };
}

/** The headers of the nth call, narrowed: `noUncheckedIndexedAccess` is on, and a missing call
 * is a test bug worth an error rather than a chain of optional chaining. */
function headersOf(
  calls: { headers: Record<string, string> }[][],
  nth = 0,
): Record<string, string> {
  const call = calls[nth]?.[0];
  if (call === undefined) throw new Error(`no call ${nth}`);
  return call.headers;
}

describe('upload', () => {
  beforeEach(() => {
    createUpload.mockReset();
    appendToUpload.mockReset();
    uploadOffset.mockReset();
  });

  it('sends a small file in one request', { tags: ['@F-001/FR-2'] }, async () => {
    createUpload.mockResolvedValue(created());

    await upload('workspace-1', 'notes.txt', fileOf(12));

    expect(createUpload).toHaveBeenCalledTimes(1);
    expect(appendToUpload).not.toHaveBeenCalled();
    expect(headersOf(createUpload.mock.calls)['upload-complete']).toBe('?1');
  });

  it(
    'appends the rest of a large file without overriding the content type',
    { tags: ['@F-001/FR-2', '@F-001/FR-14'] },
    async () => {
      // The regression: an append carries `application/partial-upload`, which the generated
      // client sets per operation. A `content-type` here — of any value — replaced it, and the
      // server answered 415 to every upload over one chunk.
      createUpload.mockResolvedValue(created());
      appendToUpload.mockResolvedValue(accepted());

      await upload('workspace-1', 'video.mp4', fileOf(CHUNK_BYTES * 2 + 10));

      expect(appendToUpload).toHaveBeenCalledTimes(2);
      for (const calls of [createUpload.mock.calls, appendToUpload.mock.calls]) {
        for (let nth = 0; nth < calls.length; nth += 1) {
          expect(
            Object.keys(headersOf(calls, nth)).map((name) => name.toLowerCase()),
          ).not.toContain('content-type');
        }
      }
    },
  );

  it('names the offset each append starts at, and completes on the last', async () => {
    createUpload.mockResolvedValue(created());
    appendToUpload.mockResolvedValue(accepted());

    await upload('workspace-1', 'video.mp4', fileOf(CHUNK_BYTES * 2 + 10));

    const first = headersOf(appendToUpload.mock.calls);
    const second = headersOf(appendToUpload.mock.calls, 1);
    expect(first['upload-offset']).toBe(String(CHUNK_BYTES));
    expect(first['upload-complete']).toBe('?0');
    expect(second['upload-offset']).toBe(String(CHUNK_BYTES * 2));
    expect(second['upload-complete']).toBe('?1');
    expect(first['upload-draft-interop-version']).toBe(String(INTEROP_VERSION));
  });

  it('reports what the server has committed, not what has been handed to the network', async () => {
    createUpload.mockResolvedValue(created());
    appendToUpload.mockResolvedValue(accepted());
    const seen: number[] = [];

    await upload('workspace-1', 'video.mp4', fileOf(CHUNK_BYTES * 2), {
      onProgress: ({ sent }) => seen.push(sent),
    });

    expect(seen).toEqual([0, CHUNK_BYTES, CHUNK_BYTES * 2]);
  });

  it('resumes from the offset the server reports rather than from zero', async () => {
    // After a reload the server's committed offset is the only copy of it, so the client asks.
    uploadOffset.mockResolvedValue(accepted(CHUNK_BYTES));
    appendToUpload.mockResolvedValue(accepted());

    await upload('workspace-1', 'video.mp4', fileOf(CHUNK_BYTES * 2), {
      handle: { id: UPLOAD_ID },
    });

    expect(createUpload).not.toHaveBeenCalled();
    expect(appendToUpload).toHaveBeenCalledTimes(1);
    expect(headersOf(appendToUpload.mock.calls)['upload-offset']).toBe(String(CHUNK_BYTES));
  });

  it('keeps the upload id, so an interrupted transfer can be resumed', async () => {
    createUpload.mockResolvedValue(created());
    appendToUpload.mockResolvedValue({
      error: { title: 'Conflict' },
      response: { status: 409, headers: new Headers({ 'upload-offset': '0' }) },
    });
    const handle: { id?: string } = {};

    await expect(
      upload('workspace-1', 'video.mp4', fileOf(CHUNK_BYTES * 2), { handle }),
    ).rejects.toBeInstanceOf(UploadFailed);

    expect(handle.id).toBe(UPLOAD_ID);
  });

  it(
    'lets the generated client set the media type each operation needs',
    { tags: ['@F-001/FR-14'] },
    async () => {
      // The other half of the fix, against the *real* SDK rather than the mock: the media type
      // this client relies on is the one the generator wrote from the contract. A regeneration
      // that changed it would put every append back to 415, silently, so the effective header is
      // asserted here — merged with the protocol headers, because header names are
      // case-insensitive and that is precisely how the old override won.
      const actual = await vi.importActual<typeof import('@store-everything/api-client')>(
        '@store-everything/api-client',
      );
      const sent: Record<string, string>[] = [];
      const record = vi.fn((options: { headers: Record<string, string> }) => {
        sent.push(options.headers);
        return Promise.resolve({});
      });

      await actual.appendToUpload({
        client: { patch: record } as never,
        path: { upload_id: UPLOAD_ID },
        body: new Blob(['a chunk']),
        headers: { 'upload-offset': '0', 'upload-complete': '?1' },
      });
      await actual.createUpload({
        client: { post: record } as never,
        path: { workspace_id: 'workspace-1' },
        query: { path: 'video.mp4' },
        body: new Blob(['the first chunk']),
        headers: { 'upload-complete': '?0' },
      });

      expect(sent.map((headers) => headers['Content-Type'])).toEqual([
        'application/partial-upload',
        'application/octet-stream',
      ]);
    },
  );

  it('fails loudly when the server does not say where to continue', async () => {
    createUpload.mockResolvedValue(created(''));

    await expect(
      upload('workspace-1', 'video.mp4', fileOf(CHUNK_BYTES * 2)),
    ).rejects.toBeInstanceOf(UploadFailed);
    expect(appendToUpload).not.toHaveBeenCalled();
  });
});
