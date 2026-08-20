import { client } from '@store-everything/api-client';

/**
 * The single place API transport is configured.
 *
 * Requests are relative: the web UI is served same-origin with the API, so no CORS entry
 * is needed in production (08-api-principles.md) and Vite proxies the same paths in
 * development.
 */
export function configureApiClient(): void {
  client.setConfig({
    baseUrl: '',
    credentials: 'same-origin',
  });
}
