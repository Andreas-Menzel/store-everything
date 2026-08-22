import { client } from '@store-everything/api-client';

import { isUnauthenticated } from './problem';

/**
 * The single place API transport is configured.
 *
 * Requests are relative: the web UI is served same-origin with the API, so no CORS entry
 * is needed in production ([F-027/FR-1](../../../../features/F-027-web-application-shell.md))
 * and Vite proxies the same paths in development.
 *
 * Credentials are the session cookie and nothing else. It is `HttpOnly`, so this code cannot
 * read it even to check — which is the point (F-027/FR-3): there is nothing here to leak into
 * `localStorage`, a URL, or an error report.
 */

type SignOutListener = () => void;

let onSignedOut: SignOutListener | undefined;

/**
 * Say what to do when the server stops recognising us.
 *
 * A `401` can arrive from *any* request, at any time, because a session expires on a clock the
 * client does not hold. Handling it per call site would mean every surface growing a branch for
 * it, and the ones that forgot would show a wall of failures instead of a login form
 * (F-027/FR-6).
 */
export function onSessionEnded(listener: SignOutListener): void {
  onSignedOut = listener;
}

export function configureApiClient(): void {
  client.setConfig({
    baseUrl: '',
    credentials: 'same-origin',
  });

  client.interceptors.response.use((response) => {
    if (isUnauthenticated(response.status)) {
      onSignedOut?.();
    }
    return response;
  });
}
