<script setup lang="ts">
/**
 * The front door: the workspaces this account owns, and a way to make one
 * ([F-001](../../../features/F-001-upload-and-import.md)).
 *
 * A workspace being created is a real state rather than a spinner: provisioning is a leased
 * operation that builds a directory tree, so `provisioning` is shown as itself and the list
 * refreshes while any workspace is still in it.
 */
import {
  createWorkspace,
  listWorkspaces,
  type WorkspaceSummary,
} from '@store-everything/api-client';
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query';
import { computed, ref } from 'vue';
import { RouterLink } from 'vue-router';

import {
  AppAlert,
  AppButton,
  AppCard,
  AppEmpty,
  AppField,
  AppSpinner,
  fieldFailure,
  toFailure,
  type Failure,
} from '@/shared';

const cache = useQueryClient();

const workspaces = useQuery({
  queryKey: ['workspaces'],
  queryFn: async (): Promise<WorkspaceSummary[]> => {
    const { data, error, response } = await listWorkspaces();
    if (error !== undefined) throw toFailure(error, response?.status);
    return data?.data ?? [];
  },
  // A workspace is built by a background operation, so the list is worth re-reading while one
  // is still being provisioned — and worth leaving alone once none is.
  refetchInterval: (query) =>
    (query.state.data ?? []).some((workspace) => workspace.state === 'provisioning') ? 2000 : false,
});

const name = ref('');
const failure = ref<Failure | undefined>(undefined);

const creation = useMutation({
  mutationFn: async (workspaceName: string) => {
    const { data, error, response } = await createWorkspace({ body: { name: workspaceName } });
    if (error !== undefined) throw toFailure(error, response?.status);
    return data;
  },
  onSuccess: async () => {
    name.value = '';
    failure.value = undefined;
    await cache.invalidateQueries({ queryKey: ['workspaces'] });
  },
  onError: (error: unknown) => {
    failure.value = error as Failure;
  },
});

const isEmpty = computed(
  () => !workspaces.isPending.value && (workspaces.data.value ?? []).length === 0,
);
</script>

<template>
  <h1 class="mb-4 text-lg font-semibold">Workspaces</h1>

  <div class="flex flex-col gap-4">
    <AppCard title="New workspace">
      <form class="flex items-end gap-3" @submit.prevent="creation.mutate(name)">
        <div class="grow">
          <AppField
            v-model="name"
            label="Name"
            required
            hint="Becomes a directory on the storage, so the naming rules apply."
            :error="fieldFailure(failure, '/body/name')"
          />
        </div>
        <AppButton type="submit" :disabled="creation.isPending.value || name.length === 0">
          Create
        </AppButton>
      </form>
      <AppAlert v-if="failure" class="mt-3" :failure="failure" />
    </AppCard>

    <AppSpinner v-if="workspaces.isPending.value" label="Loading workspaces" />
    <AppAlert
      v-else-if="workspaces.isError.value"
      :failure="toFailure(workspaces.error.value)"
      title="Could not load your workspaces"
    />
    <AppEmpty
      v-else-if="isEmpty"
      title="No workspaces yet"
      detail="A workspace is a tree of your files. Create one to start putting things in it."
    />
    <AppCard v-else title="Your workspaces">
      <ul class="divide-y divide-(--color-border-subtle)">
        <li
          v-for="workspace in workspaces.data.value"
          :key="workspace.id"
          class="flex items-center justify-between gap-4 py-3 first:pt-0 last:pb-0"
        >
          <div>
            <RouterLink
              :to="{ name: 'workspace', params: { id: workspace.id } }"
              class="text-sm font-medium underline-offset-2 hover:underline"
            >
              {{ workspace.name }}
            </RouterLink>
            <p class="font-mono text-xs text-(--color-ink-muted)">{{ workspace.root_path }}</p>
          </div>
          <p class="text-xs text-(--color-ink-muted)">
            {{ workspace.state === 'provisioning' ? 'Being created…' : workspace.placement }}
          </p>
        </li>
      </ul>
    </AppCard>
  </div>
</template>
