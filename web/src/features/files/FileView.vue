<script setup lang="ts">
/**
 * One file: what the app knows about it, and its bytes.
 *
 * The download is a plain link to `/api/v1/files/{id}/content`, and deliberately so: the session
 * cookie rides along on a same-origin navigation, so `Range`, the `ETag` and resuming a paused
 * download are the browser's business rather than ours — and the bytes never pass through
 * JavaScript ([F-027/FR-1](../../../features/F-027-web-application-shell.md)).
 */
import { readFile, readFilePreview } from '@store-everything/api-client';
import { useQuery } from '@tanstack/vue-query';
import { computed } from 'vue';
import { RouterLink, useRoute } from 'vue-router';

import PageViewer from '@/features/previews/PageViewer.vue';
import TagList from '@/features/tags/TagList.vue';
import { AppAlert, AppCard, AppSpinner, AppThumbnail, toFailure } from '@/shared';

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
 * What can be shown for this file, as the server describes it
 * ([F-028/FR-6](../../../features/F-028-thumbnails-and-previews.md)).
 *
 * Asked once per file rather than guessed from the media type: a document's page count and the
 * URL pattern for one page both come from here, so a viewer for a kind that did not exist when
 * this shipped needs no change on this side.
 */
const preview = useQuery({
  queryKey: computed(() => ['file', id.value, 'preview']),
  queryFn: async () => {
    const { data, error, response } = await readFilePreview({ path: { file_id: id.value } });
    if (error !== undefined) throw toFailure(error, response?.status);
    return data;
  },
});

const pages = computed(() => preview.data.value?.pages ?? 0);

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

    <AppCard v-if="file.data.value.has_thumbnail" class="mb-4" title="Preview">
      <!-- Pinned to this version, so the browser may cache it permanently (F-028/FR-4). The card
           appears only when the server said there is something to show: a file with nothing to
           render gets no broken image and no empty frame (FR-3). -->
      <div class="h-64 w-full max-w-md overflow-hidden rounded-(--radius-control)">
        <AppThumbnail
          :src="`/api/v1/files/${id}/thumbnail?size=1024&v=${file.data.value.version}`"
          :placeholder="file.data.value.placeholder_hash"
          :alt="`Preview of ${file.data.value.name}`"
        />
      </div>
    </AppCard>

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

    <AppCard v-if="pages > 0" class="mb-4" title="Pages">
      <PageViewer :file-id="id" :version="file.data.value.version" :pages="pages" />
    </AppCard>

    <AppCard class="mt-4" title="Tags">
      <TagList :subject="{ kind: 'file', id: id }" />
    </AppCard>

    <p class="mt-4 text-xs">
      <RouterLink :to="{ name: 'workspaces' }" class="underline-offset-2 hover:underline">
        Back to workspaces
      </RouterLink>
    </p>
  </template>
</template>
