import { describe, expect, it } from 'vitest';

import { fieldFailure, isUnauthenticated, toFailure } from './problem';

describe('toFailure', { tags: ['@F-027/FR-8'] }, () => {
  it('reads a problem document', () => {
    const failure = toFailure({
      type: 'https://docs.example/errors/conflict',
      title: 'Conflict',
      status: 409,
      detail: 'Something already holds that name.',
      instance: 'req_1',
    });

    expect(failure).toEqual({
      title: 'Conflict',
      detail: 'Something already holds that name.',
      instance: 'req_1',
      status: 409,
      fields: [],
    });
  });

  it('keeps field errors with their pointers', () => {
    const failure = toFailure({
      title: 'Validation failed',
      errors: [
        { detail: 'that name is too long', pointer: '/body/name' },
        { detail: 'not a real thing', missing: 'pointer' },
      ],
    });

    expect(failure.fields).toEqual([{ detail: 'that name is too long', pointer: '/body/name' }]);
    expect(fieldFailure(failure, '/body/name')).toBe('that name is too long');
    expect(fieldFailure(failure, '/body/other')).toBeUndefined();
  });

  it('produces a sentence for a body that is not a problem document at all', () => {
    // A proxy's HTML error page, an empty body, a network failure: an empty screen would tell
    // the user nothing and us less (F-027/FR-8).
    for (const body of ['<html>502</html>', undefined, null, {}, { detail: 'no title' }]) {
      const failure = toFailure(body, 502);
      expect(failure.title).toBe('Something went wrong');
      expect(failure.status).toBe(502);
    }
  });

  it('knows the one status that means the session is over', { tags: ['@F-027/FR-6'] }, () => {
    expect(isUnauthenticated(401)).toBe(true);
    expect(isUnauthenticated(403)).toBe(false);
    expect(isUnauthenticated(undefined)).toBe(false);
  });
});
