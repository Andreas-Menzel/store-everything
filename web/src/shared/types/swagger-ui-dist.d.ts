/**
 * `swagger-ui-dist` ships a prebuilt bundle and no types, so the surface this app uses is
 * declared here rather than pulled in as another dependency
 * ([F-027/FR-9](../../../../features/F-027-web-application-shell.md)). Narrow on purpose: what
 * is not declared is not used.
 */
declare module 'swagger-ui-dist/swagger-ui-es-bundle.js' {
  interface SwaggerUIOptions {
    domNode: HTMLElement;
    spec: Record<string, unknown>;
    withCredentials?: boolean;
    /** `null` disables the badge that would post the schema to a third-party validator. */
    validatorUrl?: string | null;
    deepLinking?: boolean;
  }

  export default function SwaggerUI(options: SwaggerUIOptions): unknown;
}

declare module 'swagger-ui-dist/swagger-ui.css';
