<script setup lang="ts">
/**
 * One file: what the app knows about it, and its bytes.
 *
 * The download is a plain link to `/api/v1/files/{id}/content`, and deliberately so: the session
 * cookie rides along on a same-origin navigation, so `Range`, the `ETag` and resuming a paused
 * download are the browser's business rather than ours — and the bytes never pass through
 * JavaScript ([F-027/FR-1](../../../features/F-027-web-application-shell.md)).
 */
import { readFile } from '@store-everything/api-client';
import { useQuery } from '@tanstack/vue-query';
import { computed } from 'vue';
import { RouterLink, useRoute } from 'vue-router';

import { AppAlert, AppCard, AppSpinner, toFailure } from '@/shared';

const route = useRoute();
const id = computed(() => String(route.params.id));

const file = useQuery({
  queryKey: computed(() => ['file', id.value]),
  queryFn: async () => {
    const { data, error, response } = await readFile({ path: { file_id: id.value } });
    if (error !== undefined) throw toFailure(error, response?.status);
    return data;
  },
});

const href = computed(() => `/api/v1/files/${id.value}/content`);

/**
 * What the analysis status means to somebody looking at their own file.
 *
 * The API's five words are about jobs; these are about the person's question, which is "can I
 * find this by its content yet?" ([F-001/FR-8](../../../features/F-001-upload-and-import.md)).
 * `none` is deliberately not an error: nothing installed analyses this kind of file.
 */
const EXTRACTION_LABELS: Record<string, string> = {
  pending: 'Analysing…',
  indexed: 'Analysed',
  partial: 'Partly analysed',
  failed: 'Analysis failed',
  none: 'Not analysed',
};

const extraction = computed(() => {
  const status = file.data.value?.extraction_status;
  return status === undefined ? undefined : (EXTRACTION_LABELS[status] ?? status);
});
</script>

<template>
  <AppSpinner v-if="file.isPending.value" label="Loading file" />
  <AppAlert
    v-else-if="file.isError.value"
    :failure="toFailure(file.error.value)"
    title="Could not open this file"
  />
  <template v-else-if="file.data.value">
    <h1 class="mb-1 text-lg font-semibold">{{ file.data.value.name }}</h1>
    <p class="mb-4 font-mono text-xs text-(--color-ink-muted)">{{ file.data.value.path }}</p>

    <AppCard title="File">
      <dl class="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
        <div>
          <dt class="text-(--color-ink-muted)">Size</dt>
          <dd>{{ file.data.value.size }} bytes</dd>
        </div>
        <div>
          <dt class="text-(--color-ink-muted)">Type</dt>
          <dd>{{ file.data.value.media_type }}</dd>
        </div>
        <div>
          <dt class="text-(--color-ink-muted)">State</dt>
          <dd>{{ file.data.value.state }}</dd>
        </div>
        <div>
          <dt class="text-(--color-ink-muted)">Content analysis</dt>
          <dd data-testid="extraction-status">{{ extraction }}</dd>
        </div>
        <div class="col-span-2 sm:col-span-3">
          <dt class="text-(--color-ink-muted)">Content hash</dt>
          <dd class="font-mono text-xs break-all">{{ file.data.value.content_hash }}</dd>
        </div>
      </dl>
      <AppAlert
        v-if="file.data.value.trash"
        class="mt-4"
        tone="caution"
        title="This file is in the trash"
      >
        <p class="mt-1 text-(--color-ink)">
          Removed
          {{ file.data.value.trash.origin === 'detected_on_disk' ? 'outside the app' : '' }}, kept
          until {{ file.data.value.trash.purge_after }}.
        </p>
      </AppAlert>
      <template #actions>
        <a
          :href="href"
          class="text-sm underline-offset-2 hover:underline"
          :download="file.data.value.name"
        >
          Download
        </a>
      </template>
    </AppCard>

    <p class="mt-4 text-xs">
      <RouterLink :to="{ name: 'workspaces' }" class="underline-offset-2 hover:underline">
        Back to workspaces
      </RouterLink>
    </p>
  </template>
</template>
