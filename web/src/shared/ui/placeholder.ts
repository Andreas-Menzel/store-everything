/**
 * Reading the placeholder a listing row carries
 * ([F-028/FR-5](../../../../features/F-028-thumbnails-and-previews.md)).
 *
 * The format is the instance's own and deliberately tiny — 43 bytes, base64url, no padding:
 *
 * | bytes | meaning |
 * |---|---|
 * | 0 | format version (`1`) |
 * | 1–2 | grid columns, grid rows |
 * | 3–6 | the source's width and height, big-endian |
 * | 7… | one RGB triple per cell, row-major |
 *
 * The dimensions are in there so a cell can reserve the *right* space before any image arrives:
 * a grid that reflows when thumbnails land is worse than one that starts grey. Everything else is
 * colour, which is all a blurred placeholder needs to be.
 *
 * Unknown version, wrong length, malformed base64: `undefined`. A placeholder is a nicety, and a
 * client that threw on one would turn a cosmetic surprise into a broken page.
 */

/** A decoded placeholder, ready to paint. */
export interface Placeholder {
  columns: number;
  rows: number;
  width: number;
  height: number;
  /** `rgb(r g b)` per cell, row-major — as many as `columns * rows`. */
  cells: string[];
}

const VERSION = 1;
const HEADER_BYTES = 7;

function bytesOf(encoded: string): Uint8Array | undefined {
  // base64url, and the padding the encoder dropped.
  const padded = encoded.replaceAll('-', '+').replaceAll('_', '/');
  try {
    const binary = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4));
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return undefined;
  }
}

export function decodePlaceholder(encoded: string | null | undefined): Placeholder | undefined {
  if (!encoded) return undefined;
  const bytes = bytesOf(encoded);
  if (bytes === undefined || bytes.length < HEADER_BYTES) return undefined;

  const version = bytes[0];
  const columns = bytes[1] ?? 0;
  const rows = bytes[2] ?? 0;
  if (version !== VERSION || !columns || !rows) return undefined;

  const expected = HEADER_BYTES + columns * rows * 3;
  if (bytes.length < expected) return undefined;

  const width = ((bytes[3] ?? 0) << 8) | (bytes[4] ?? 0);
  const height = ((bytes[5] ?? 0) << 8) | (bytes[6] ?? 0);
  const cells: string[] = [];
  for (let index = 0; index < columns * rows; index += 1) {
    const at = HEADER_BYTES + index * 3;
    cells.push(`rgb(${bytes[at]} ${bytes[at + 1]} ${bytes[at + 2]})`);
  }
  return { columns, rows, width, height, cells };
}
