/**
 * The only sanctioned way to reach the API.
 *
 * Everything here is generated from `openapi.json`; hand-rolled HTTP calls are a lint
 * error in the web app (11-engineering-standards.md § code reuse, rule 8).
 */
export * from './generated';
export { client } from './generated/client.gen';
