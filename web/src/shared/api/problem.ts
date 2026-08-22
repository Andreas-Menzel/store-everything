/**
 * Turning a failed request into something a person can read.
 *
 * The API answers failures as `problem+json` ([08 § errors](../../../../specs/08-api-principles.md)):
 * a `title`, usually a `detail`, the request id in `instance`, and for a rejected form a list of
 * `errors` each carrying a JSON `pointer`. Every one of those is worth showing, and none of them
 * is worth showing raw — so this is the single place that reads the envelope
 * ([F-027/FR-8](../../../../features/F-027-web-application-shell.md)).
 *
 * The important case is the one that is *not* a problem document at all: a proxy's HTML error
 * page, an empty body, a network failure. Those still have to produce a sentence, because an
 * empty screen tells the user nothing and tells us less.
 */

/** One field-level failure, addressed by the pointer the server used. */
export interface FieldProblem {
  detail: string;
  pointer: string;
}

/** A failure, ready to render. */
export interface Failure {
  title: string;
  detail?: string;
  /** The request id, worth quoting when asking for help. */
  instance?: string;
  status?: number;
  fields: FieldProblem[];
}

const UNEXPLAINED: Failure = {
  title: 'Something went wrong',
  detail: 'The server did not explain what failed. Trying again may work.',
  fields: [],
};

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null
    ? (value as Record<string, unknown>)
    : undefined;
}

function asText(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

function asFields(value: unknown): FieldProblem[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => {
    const record = asRecord(entry);
    const detail = asText(record?.detail);
    const pointer = asText(record?.pointer);
    return detail !== undefined && pointer !== undefined ? [{ detail, pointer }] : [];
  });
}

/**
 * Read whatever the failure carried. Never throws, and never returns nothing.
 *
 * `status` is passed separately because a transport-level failure has one when the body does
 * not — and "503" is worth showing even when nothing else could be parsed.
 */
export function toFailure(body: unknown, status?: number): Failure {
  const document = asRecord(body);
  const title = asText(document?.title);
  if (document === undefined || title === undefined) {
    return { ...UNEXPLAINED, status };
  }
  return {
    title,
    detail: asText(document.detail),
    instance: asText(document.instance),
    status: typeof document.status === 'number' ? document.status : status,
    fields: asFields(document.errors),
  };
}

/**
 * The failure for one field, if the server blamed it.
 *
 * Pointers are JSON pointers into the request (`/body/name`), so a form asks by the name it
 * sent rather than by an index into a list it did not choose.
 */
export function fieldFailure(failure: Failure | undefined, pointer: string): string | undefined {
  return failure?.fields.find((field) => field.pointer === pointer)?.detail;
}

/** True when the caller is not (or no longer) signed in. */
export function isUnauthenticated(status: number | undefined): boolean {
  return status === 401;
}
