<script setup lang="ts">
/**
 * Browsing a folder ([F-015/FR-5](../../../features/F-015-folders.md)).
 *
 * Three things the server decides and this must not re-decide:
 *
 * - **the order** — subfolders first by name, then files by the requested key, as one stream with
 *   one cursor. Sorting client-side would break at the first page boundary;
 * - **the paths** — every path in the response is already rendered for this caller from their
 *   visibility root (FR-12), so they are shown as given, never assembled here;
 * - **the totals** — `pending` says the folder's recursive numbers are still catching up (FR-8),
 *   which is worth showing rather than hiding behind a number that will change.
 */
import { listChildren, readFolder, type Child } from '@store-everything/api-client';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/vue-query';
import { computed, ref } from 'vue';
import { RouterLink, useRoute } from 'vue-router';

import UploadPanel from '@/features/upload/UploadPanel.vue';
import { AppAlert, AppButton, AppCard, AppEmpty, AppSpinner, toFailure } from '@/shared';

type Ordering = 'name' | 'size' | 'modified';

const route = useRoute();
const cache = useQueryClient();
const id = computed(() => String(route.params.id));
const sort = ref<Ordering>('name');

const folder = useQuery({
  queryKey: computed(() => ['folder', id.value]),
  queryFn: async () => {
    const { data, error, response } = await readFolder({ path: { folder_id: id.value } });
    if (error !== undefined) throw toFailure(error, response?.status);
    return data;
  },
});

const children = useInfiniteQuery({
  queryKey: computed(() => ['folder', id.value, 'children', sort.value]),
  initialPageParam: undefined as string | undefined,
  queryFn: async ({ pageParam }) => {
    const { data, error, response } = await listChildren({
      path: { folder_id: id.value },
      query: { sort: sort.value, cursor: pageParam },
    });
    if (error !== undefined) throw toFailure(error, response?.status);
    return data;
  },
  // The cursor carries the ordering it was made under, so a page is only ever asked for with the
  // sort it belongs to — which is why `sort` is part of the query key.
  getNextPageParam: (last) => last?.next_cursor ?? undefined,
});

const rows = computed<Child[]>(() =>
  (children.data.value?.pages ?? []).flatMap((page) => page?.data ?? []),
);

function bytes(count: number): string {
  const units = ['B', 'kB', 'MB', 'GB', 'TB'];
  let value = count;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`;
}

async function refresh(): Promise<void> {
  await cache.invalidateQueries({ queryKey: ['folder', id.value] });
}
</script>

<template>
  <AppSpinner v-if="folder.isPending.value" label="Loading folder" />
  <AppAlert
    v-else-if="folder.isError.value"
    :failure="toFailure(folder.error.value)"
    title="Could not open this folder"
  />
  <template v-else-if="folder.data.value">
    <h1 class="mb-1 text-lg font-semibold">
      {{ folder.data.value.path === '' ? 'All files' : folder.data.value.name }}
    </h1>
    <p class="mb-4 text-xs text-(--color-ink-muted)">
      <RouterLink
        v-if="folder.data.value.parent"
        :to="{ name: 'folder', params: { id: folder.data.value.parent } }"
        class="underline-offset-2 hover:underline"
      >
        Up one level
      </RouterLink>
      <span v-if="folder.data.value.parent"> · </span>
      {{ folder.data.value.aggregates.direct_files }} here ·
      {{ folder.data.value.aggregates.total_files }} in total,
      {{ bytes(folder.data.value.aggregates.total_bytes) }}
      <span v-if="folder.data.value.aggregates.pending"> (still counting)</span>
    </p>

    <div class="flex flex-col gap-4">
      <AppCard title="Contents">
        <template #actions>
          <label class="flex items-center gap-2 text-xs text-(--color-ink-muted)">
            Sort files by
            <select
              v-model="sort"
              class="rounded-(--radius-control) bg-(--color-surface) px-2 py-1 text-xs ring-1 ring-(--color-border-subtle)"
            >
              <option value="name">name</option>
              <option value="size">size</option>
              <option value="modified">modified</option>
            </select>
          </label>
        </template>

        <AppSpinner v-if="children.isPending.value" label="Loading contents" />
        <AppAlert
          v-else-if="children.isError.value"
          :failure="toFailure(children.error.value)"
          title="Could not list this folder"
        />
        <AppEmpty
          v-else-if="rows.length === 0"
          title="This folder is empty"
          detail="Upload something into it, or put files in the directory on the storage."
        />
        <ul v-else class="divide-y divide-(--color-border-subtle)">
          <li v-for="row in rows" :key="row.id" class="flex items-center gap-4 py-2">
            <RouterLink
              v-if="row.kind === 'folder'"
              :to="{ name: 'folder', params: { id: row.id } }"
              class="text-sm underline-offset-2 hover:underline"
            >
              {{ row.name }}/
            </RouterLink>
            <template v-if="row.kind === 'file'">
              <RouterLink
                :to="{ name: 'file', params: { id: row.id } }"
                class="grow truncate text-sm underline-offset-2 hover:underline"
              >
                {{ row.name }}
              </RouterLink>
              <span class="text-xs text-(--color-ink-muted)">{{ row.media_type }}</span>
              <span class="w-20 text-right text-xs text-(--color-ink-muted)">
                {{ bytes(row.size) }}
              </span>
            </template>
          </li>
        </ul>
        <div v-if="children.hasNextPage.value" class="mt-4">
          <AppButton
            variant="quiet"
            :disabled="children.isFetchingNextPage.value"
            @click="children.fetchNextPage()"
          >
            Show more
          </AppButton>
        </div>
      </AppCard>

      <UploadPanel
        :workspace-id="folder.data.value.workspace"
        :folder-path="folder.data.value.path"
        @uploaded="refresh"
      />
    </div>
  </template>
</template>
