<script setup lang="ts">
/**
 * The vocabulary, and the review queue behind it
 * ([F-003/FR-10, FR-12](../../../features/F-003-tagging.md)).
 *
 * Two audiences on one page, and the page says which one you are. Anyone may read the taxonomy —
 * a shared vocabulary nobody can browse is not usable — while shaping it is administration, so
 * the controls appear only for an administrator and the server refuses anyway
 * ([07](../../../specs/07-identity-permissions-sharing.md)).
 *
 * The review queue is first, deliberately. Suggestions are the only part of this page with a
 * deadline: every one of them is a word a machine put on somebody's file that search cannot find
 * yet, and the whole point of quarantining rather than dropping them is that they get looked at.
 */
import {
  approveTag,
  createTag,
  deleteTag,
  listTags,
  mergeTag,
  rejectTag,
  updateTag,
  type TagSummary,
} from '@store-everything/api-client';
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query';
import { computed, ref } from 'vue';

import TagPicker from './TagPicker.vue';
import { useSession } from '@/features/auth/session';
import {
  AppAlert,
  AppButton,
  AppCard,
  AppEmpty,
  AppField,
  AppSpinner,
  toFailure,
  type Failure,
} from '@/shared';

const cache = useQueryClient();
const { identity } = useSession();
const isAdmin = computed(() => identity.value?.role === 'admin');

async function fetchTags(status: 'active' | 'suggested'): Promise<TagSummary[]> {
  const { data, error, response } = await listTags({ query: { status, limit: 200 } });
  if (error !== undefined) throw toFailure(error, response?.status);
  return data?.data ?? [];
}

const vocabulary = useQuery({ queryKey: ['tags', 'active'], queryFn: () => fetchTags('active') });
const suggestions = useQuery({
  queryKey: ['tags', 'suggested'],
  // Only an administrator may ask; for anyone else the queue is not theirs to see.
  enabled: isAdmin,
  queryFn: () => fetchTags('suggested'),
});

const failure = ref<Failure | undefined>(undefined);

async function refresh(): Promise<void> {
  failure.value = undefined;
  await cache.invalidateQueries({ queryKey: ['tags'] });
}

function failed(error: unknown): void {
  failure.value = toFailure(error);
}

const name = ref('');
const creation = useMutation({
  mutationFn: async (tagName: string) => {
    const { data, error, response } = await createTag({ body: { name: tagName } });
    if (error !== undefined) throw toFailure(error, response?.status);
    return data;
  },
  onSuccess: async () => {
    name.value = '';
    await refresh();
  },
  onError: failed,
});

const review = useMutation({
  mutationFn: async (decision: { tag: string; approved: boolean }) => {
    const decide = decision.approved ? approveTag : rejectTag;
    const { data, error, response } = await decide({ path: { tag_id: decision.tag } });
    if (error !== undefined) throw toFailure(error, response?.status);
    return data;
  },
  onSuccess: refresh,
  onError: failed,
});

const renaming = ref<{ id: string; name: string } | undefined>(undefined);
const rename = useMutation({
  mutationFn: async (change: { tag: string; name: string }) => {
    const { data, error, response } = await updateTag({
      path: { tag_id: change.tag },
      body: { name: change.name },
    });
    if (error !== undefined) throw toFailure(error, response?.status);
    return data;
  },
  onSuccess: async () => {
    renaming.value = undefined;
    await refresh();
  },
  onError: failed,
});

const merging = ref<string | undefined>(undefined);
const merge = useMutation({
  mutationFn: async (change: { tag: string; into: string }) => {
    const { data, error, response } = await mergeTag({
      path: { tag_id: change.tag },
      body: { into: change.into },
    });
    if (error !== undefined) throw toFailure(error, response?.status);
    return data;
  },
  onSuccess: async () => {
    merging.value = undefined;
    await refresh();
  },
  onError: failed,
});

const erase = useMutation({
  mutationFn: async (tagId: string) => {
    const { error, response } = await deleteTag({ path: { tag_id: tagId } });
    if (error !== undefined) throw toFailure(error, response?.status);
  },
  onSuccess: refresh,
  onError: failed,
});

const isEmpty = computed(
  () => !vocabulary.isPending.value && (vocabulary.data.value ?? []).length === 0,
);
const pending = computed(() => suggestions.data.value ?? []);
</script>

<template>
  <h1 class="mb-1 text-lg font-semibold">Tags</h1>
  <p class="mb-4 text-sm text-(--color-ink-muted)">
    One vocabulary for the whole instance. Anyone can use these words; an administrator decides what
    they are.
  </p>

  <AppAlert v-if="failure" class="mb-4" :failure="failure" title="That did not work" />

  <AppCard v-if="isAdmin" class="mb-4" title="Awaiting review">
    <p class="mb-3 text-sm text-(--color-ink-muted)">
      Words an extractor proposed because nothing in the vocabulary fitted. Until one is approved it
      stays off search and out of every tag picker — but it is already on the files that earned it.
    </p>
    <AppSpinner v-if="suggestions.isPending.value" label="Loading suggestions" />
    <AppEmpty
      v-else-if="pending.length === 0"
      title="Nothing is waiting"
      detail="Extractors have not proposed any word this vocabulary does not already have."
    />
    <ul v-else class="flex flex-col gap-2" data-testid="suggestions">
      <li
        v-for="suggestion in pending"
        :key="suggestion.id"
        class="flex items-center justify-between gap-3 rounded-md border border-(--color-border-subtle) px-3 py-2 text-sm"
      >
        <span>
          <span class="font-medium">{{ suggestion.name }}</span>
          <span class="ml-2 text-xs text-(--color-ink-muted)">
            on {{ suggestion.usage.files }} of your file(s)
          </span>
        </span>
        <span class="flex gap-2">
          <AppButton
            :disabled="review.isPending.value"
            :aria-label="`Approve ${suggestion.name}`"
            @click="review.mutate({ tag: suggestion.id, approved: true })"
          >
            Approve
          </AppButton>
          <AppButton
            variant="quiet"
            :disabled="review.isPending.value"
            :aria-label="`Reject ${suggestion.name}`"
            @click="review.mutate({ tag: suggestion.id, approved: false })"
          >
            Reject
          </AppButton>
        </span>
      </li>
    </ul>
  </AppCard>

  <AppCard v-if="isAdmin" class="mb-4" title="Add a word">
    <form class="flex items-end gap-3" @submit.prevent="creation.mutate(name)">
      <AppField v-model="name" class="grow" label="Name" :disabled="creation.isPending.value" />
      <AppButton type="submit" :disabled="creation.isPending.value || name.trim().length === 0">
        Create
      </AppButton>
    </form>
  </AppCard>

  <AppCard title="The vocabulary">
    <AppSpinner v-if="vocabulary.isPending.value" label="Loading tags" />
    <AppAlert
      v-else-if="vocabulary.isError.value"
      :failure="toFailure(vocabulary.error.value)"
      title="Could not read the vocabulary"
    />
    <AppEmpty
      v-else-if="isEmpty"
      title="No tags yet"
      detail="An administrator adds the words; everyone else applies them."
    />
    <ul v-else class="flex flex-col divide-y divide-(--color-border-subtle)">
      <li v-for="tag in vocabulary.data.value" :key="tag.id" class="py-2 text-sm">
        <div class="flex items-center justify-between gap-3">
          <span>
            <span class="font-medium" :data-testid="`vocabulary-${tag.name}`">{{ tag.name }}</span>
            <span class="ml-2 text-xs text-(--color-ink-muted)">
              {{ tag.usage.files }} file(s), {{ tag.usage.folders }} folder(s)
            </span>
          </span>
          <span v-if="isAdmin" class="flex gap-2">
            <AppButton
              variant="quiet"
              :aria-label="`Rename ${tag.name}`"
              @click="renaming = { id: tag.id, name: tag.name }"
            >
              Rename
            </AppButton>
            <AppButton variant="quiet" :aria-label="`Merge ${tag.name}`" @click="merging = tag.id">
              Merge
            </AppButton>
            <AppButton
              variant="quiet"
              :aria-label="`Delete ${tag.name}`"
              :disabled="erase.isPending.value"
              @click="erase.mutate(tag.id)"
            >
              Delete
            </AppButton>
          </span>
        </div>

        <form
          v-if="renaming?.id === tag.id"
          class="mt-2 flex items-end gap-3"
          @submit.prevent="rename.mutate({ tag: tag.id, name: renaming?.name ?? '' })"
        >
          <AppField
            v-model="renaming.name"
            class="grow"
            :label="`New name for ${tag.name}`"
            :disabled="rename.isPending.value"
          />
          <AppButton type="submit" :disabled="rename.isPending.value">Save</AppButton>
          <AppButton variant="quiet" @click="renaming = undefined">Cancel</AppButton>
        </form>

        <div v-if="merging === tag.id" class="mt-2 flex items-end gap-3">
          <TagPicker
            class="grow"
            :label="`Merge “${tag.name}” into`"
            :applied="[tag.id]"
            :disabled="merge.isPending.value"
            @picked="(into: TagSummary) => merge.mutate({ tag: tag.id, into: into.id })"
          />
          <AppButton variant="quiet" @click="merging = undefined">Cancel</AppButton>
        </div>
      </li>
    </ul>
  </AppCard>
</template>
