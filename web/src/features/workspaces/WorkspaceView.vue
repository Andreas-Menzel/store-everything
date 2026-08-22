<script setup lang="ts">
/**
 * One workspace: where its files are, and what the last scan made of them.
 *
 * The import surface is [F-001/FR-10](../../../features/F-001-upload-and-import.md)'s: a 10 TB
 * import runs for hours, so "how much is left" and "what did it refuse to register" are the two
 * numbers that make it legible. While a scan is running the status re-reads itself; when nothing
 * is running it stops, because polling an idle instance is just noise.
 */
import { importStatus, readWorkspace, rescanWorkspace } from '@store-everything/api-client';
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query';
import { computed } from 'vue';
import { RouterLink, useRoute } from 'vue-router';

import { AppAlert, AppButton, AppCard, AppSpinner, toFailure } from '@/shared';

const route = useRoute();
const cache = useQueryClient();
const id = computed(() => String(route.params.id));

const workspace = useQuery({
  queryKey: computed(() => ['workspace', id.value]),
  queryFn: async () => {
    const { data, error, response } = await readWorkspace({ path: { workspace_id: id.value } });
    if (error !== undefined) throw toFailure(error, response?.status);
    return data;
  },
});

const status = useQuery({
  queryKey: computed(() => ['workspace', id.value, 'import-status']),
  queryFn: async () => {
    const { data, error, response } = await importStatus({ path: { workspace_id: id.value } });
    if (error !== undefined) throw toFailure(error, response?.status);
    return data;
  },
  refetchInterval: (query) => (query.state.data?.active ? 2000 : false),
});

const rescan = useMutation({
  mutationFn: async () => {
    const { error, response } = await rescanWorkspace({
      path: { workspace_id: id.value },
      body: {},
    });
    if (error !== undefined) throw toFailure(error, response?.status);
  },
  onSuccess: async () => {
    await cache.invalidateQueries({ queryKey: ['workspace', id.value, 'import-status'] });
  },
});

const latest = computed(() => status.data.value?.active ?? status.data.value?.recent[0]);
const findings = computed(() => status.data.value?.findings.data ?? []);
</script>

<template>
  <AppSpinner v-if="workspace.isPending.value" label="Loading workspace" />
  <AppAlert
    v-else-if="workspace.isError.value"
    :failure="toFailure(workspace.error.value)"
    title="Could not load this workspace"
  />
  <template v-else-if="workspace.data.value">
    <h1 class="mb-1 text-lg font-semibold">{{ workspace.data.value.name }}</h1>
    <p class="mb-4 font-mono text-xs text-(--color-ink-muted)">
      {{ workspace.data.value.root_path }}
    </p>

    <div class="flex flex-col gap-4">
      <AppCard title="Files">
        <template #actions>
          <AppButton
            variant="quiet"
            :disabled="rescan.isPending.value"
            @click="rescan.mutate(undefined)"
          >
            Rescan
          </AppButton>
        </template>
        <RouterLink
          v-if="workspace.data.value.root_folder"
          :to="{ name: 'folder', params: { id: workspace.data.value.root_folder } }"
          class="text-sm underline-offset-2 hover:underline"
        >
          Browse this workspace
        </RouterLink>
        <p v-else class="text-sm text-(--color-ink-muted)">
          This workspace is still being created; its folder tree does not exist yet.
        </p>
      </AppCard>

      <AppCard title="Import">
        <AppSpinner v-if="status.isPending.value" label="Loading import status" />
        <p v-else-if="!latest" class="text-sm text-(--color-ink-muted)">
          Nothing has been scanned yet.
        </p>
        <dl v-else class="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
          <div>
            <dt class="text-(--color-ink-muted)">State</dt>
            <dd>{{ latest.state }} ({{ latest.trigger }})</dd>
          </div>
          <div>
            <dt class="text-(--color-ink-muted)">Directories left</dt>
            <dd>{{ latest.directories_pending }}</dd>
          </div>
          <div>
            <dt class="text-(--color-ink-muted)">Files registered</dt>
            <dd>{{ latest.files_registered }}</dd>
          </div>
          <div>
            <dt class="text-(--color-ink-muted)">Changed</dt>
            <dd>{{ latest.files_changed }}</dd>
          </div>
          <div>
            <dt class="text-(--color-ink-muted)">Moved</dt>
            <dd>{{ latest.files_moved }}</dd>
          </div>
          <div>
            <dt class="text-(--color-ink-muted)">Trashed</dt>
            <dd>{{ latest.files_trashed }}</dd>
          </div>
        </dl>
        <p v-if="latest?.error" class="mt-3 text-sm text-(--color-critical)">{{ latest.error }}</p>
        <p v-if="status.data.value" class="mt-3 text-xs text-(--color-ink-muted)">
          Watching: {{ status.data.value.watch.state
          }}{{ status.data.value.watch.detail ? ` — ${status.data.value.watch.detail}` : '' }}
        </p>
      </AppCard>

      <AppCard v-if="findings.length > 0" title="Reported, not registered">
        <ul class="flex flex-col gap-2 text-sm">
          <li v-for="(finding, index) in findings" :key="index">
            <span class="font-mono text-xs">{{ finding.path }}</span>
            <span class="text-(--color-ink-muted)"> — {{ finding.detail }}</span>
          </li>
        </ul>
      </AppCard>
    </div>
  </template>
</template>
