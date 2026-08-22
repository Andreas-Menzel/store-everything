<script setup lang="ts">
/**
 * The API, documented and executable, behind this instance's own login
 * ([F-027/FR-9](../../../features/F-027-web-application-shell.md),
 * [08](../../../../specs/08-api-principles.md)).
 *
 * A deliberate deviation from "no docs in production": for a self-hosted, API-first product the
 * schema is a feature. It is never public, and `SE_API_DOCS_ENABLED=false` removes the schema
 * route — which is the failure reported below rather than an empty page.
 *
 * Three things about the viewer, and each one is a decision:
 *
 * - **it is bundled, never fetched.** An instance on a private network with no egress has to be
 *   able to read its own documentation, and the app's security policy would refuse a CDN anyway
 *   (FR-2). `swagger-ui-dist` is a prebuilt bundle with no runtime dependencies of its own, which
 *   is why it was preferred to viewers that bring hundreds of packages for the same page.
 * - **it is loaded on demand.** It is the largest thing in this app; somebody browsing their
 *   files should not download it. The route imports this component lazily and this component
 *   imports the viewer lazily again, so the cost lands only here.
 * - **it is handed the schema we already fetched**, rather than a URL to fetch again. One
 *   authenticated request, through the generated client like everything else (FR-11).
 */
import { openapiSchema } from '@store-everything/api-client';
import { useQuery } from '@tanstack/vue-query';
import { onWatcherCleanup, ref, watch } from 'vue';

import { AppAlert, AppCard, AppSpinner, toFailure } from '@/shared';

const container = ref<HTMLDivElement | null>(null);
const viewerFailed = ref(false);

const schema = useQuery({
  queryKey: ['openapi'],
  queryFn: async () => {
    const { data, error, response } = await openapiSchema();
    if (error !== undefined) throw toFailure(error, response?.status);
    return data as Record<string, unknown>;
  },
  staleTime: Infinity,
});

watch([container, schema.data], async ([element, document]) => {
  if (!element || !document) return;
  // Registered before the first `await`, while the watcher context still exists: the viewer owns
  // this subtree, so leaving it behind on a route change would leak a detached UI.
  onWatcherCleanup(() => element.replaceChildren());
  try {
    const [{ default: SwaggerUI }] = await Promise.all([
      import('swagger-ui-dist/swagger-ui-es-bundle.js'),
      import('swagger-ui-dist/swagger-ui.css'),
    ]);
    SwaggerUI({
      domNode: element,
      spec: document,
      // Try-it-out is authenticated by the session cookie, because the docs and the API are the
      // same origin (F-027/FR-1). Nothing here handles a credential.
      withCredentials: true,
      // The viewer's own network calls are ours to keep honest: no validator badge, which would
      // post the schema to a third-party service.
      validatorUrl: null,
      deepLinking: true,
    });
  } catch {
    viewerFailed.value = true;
  }
});
</script>

<template>
  <h1 class="mb-4 text-lg font-semibold">API</h1>
  <AppSpinner v-if="schema.isPending.value" label="Loading the API schema" />
  <AppAlert
    v-else-if="schema.isError.value"
    :failure="toFailure(schema.error.value)"
    title="The API documentation is not available"
  >
    <p class="mt-1 text-(--color-ink)">
      This instance may have it switched off (<code>SE_API_DOCS_ENABLED=false</code>).
    </p>
  </AppAlert>
  <AppAlert v-else-if="viewerFailed" title="The documentation viewer could not be loaded" />
  <AppCard v-else>
    <!-- The viewer owns this subtree; Vue only provides the node. -->
    <div ref="container" data-testid="api-reference" />
  </AppCard>
</template>
