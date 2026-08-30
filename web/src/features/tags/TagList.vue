<script setup lang="ts">
/**
 * What a file or folder is tagged with, and what a person can do about it
 * ([F-003](../../../features/F-003-tagging.md)).
 *
 * The chips say where each tag came from, because that is the difference between a word somebody
 * typed and a guess a model made (FR-3). A detected tag shows its confidence and offers the two
 * answers ADR-0004 gives a person: **confirm** it, which makes it survive every reprocessing, or
 * **remove** it, which records a rejection so no later model puts it back (FR-4, FR-5).
 *
 * A tag still in review is labelled as a suggestion rather than hidden (FR-12): it is on this
 * file, and pretending otherwise would leave the person wondering why search cannot find it.
 */
import type { AppliedTag, TagSummary } from '@store-everything/api-client';
import { computed } from 'vue';

import TagPicker from './TagPicker.vue';
import { confidenceLabel, PROVENANCE_LABELS, useTagging, useTags, type Subject } from './tagging';
import { AppAlert, AppButton, AppSpinner, toFailure, type Failure } from '@/shared';

const props = defineProps<{ subject: Subject }>();

const subject = computed(() => props.subject);
const tags = useTags(subject);
const { apply, remove, confirm } = useTagging(subject);

const appliedIds = computed(() => (tags.data.value ?? []).map((applied) => applied.id));
const isBusy = computed(
  () => apply.isPending.value || remove.isPending.value || confirm.isPending.value,
);

/** The last refusal, whichever action earned it — one place to look after a failed edit. */
const failure = computed<Failure | undefined>(() => {
  const error = apply.error.value ?? remove.error.value ?? confirm.error.value;
  return error === null || error === undefined ? undefined : toFailure(error);
});

function describe(applied: AppliedTag): string {
  const provenance = PROVENANCE_LABELS[applied.provenance] ?? applied.provenance;
  const confidence = confidenceLabel(applied);
  const detail = [confidence, applied.source?.extractor].filter(Boolean).join(', ');
  return detail ? `${provenance} (${detail})` : provenance;
}
</script>

<template>
  <div class="flex flex-col gap-3">
    <AppSpinner v-if="tags.isPending.value" label="Loading tags" />
    <AppAlert
      v-else-if="tags.isError.value"
      :failure="toFailure(tags.error.value)"
      title="Could not read the tags"
    />
    <template v-else>
      <ul v-if="(tags.data.value ?? []).length > 0" class="flex flex-wrap gap-2">
        <li
          v-for="applied in tags.data.value"
          :key="applied.id"
          class="flex items-center gap-2 rounded-full border border-(--color-border-subtle) px-3 py-1 text-sm"
          :data-testid="`tag-${applied.name}`"
        >
          <span class="font-medium">{{ applied.name }}</span>
          <span class="text-xs text-(--color-ink-muted)">{{ describe(applied) }}</span>
          <span
            v-if="applied.status === 'suggested'"
            class="rounded-full bg-(--color-surface-sunken) px-2 py-0.5 text-xs text-(--color-caution)"
            data-testid="suggested-badge"
          >
            Awaiting review
          </span>
          <AppButton
            v-if="applied.provenance === 'auto' && subject.kind === 'file'"
            variant="quiet"
            :disabled="isBusy"
            :aria-label="`Confirm ${applied.name}`"
            @click="confirm.mutate(applied.id)"
          >
            Confirm
          </AppButton>
          <AppButton
            variant="quiet"
            :disabled="isBusy"
            :aria-label="`Remove ${applied.name}`"
            @click="remove.mutate(applied.id)"
          >
            Remove
          </AppButton>
        </li>
      </ul>
      <p v-else class="text-sm text-(--color-ink-muted)">No tags yet.</p>

      <TagPicker
        :applied="appliedIds"
        :disabled="isBusy"
        @picked="(tag: TagSummary) => apply.mutate({ tag: tag.id })"
        @typed="(name: string) => apply.mutate({ name })"
      />
      <AppAlert v-if="failure" :failure="failure" title="That did not work">
        <!-- The refusals worth reading here are field-level: an unknown word, a quarantined
             suggestion. The alert shows the server's own sentence rather than a paraphrase. -->
        <p v-for="field in failure.fields" :key="field.pointer" class="mt-1 text-(--color-ink)">
          {{ field.detail }}
        </p>
      </AppAlert>
    </template>
  </div>
</template>
