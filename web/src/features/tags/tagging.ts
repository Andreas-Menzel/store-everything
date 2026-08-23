/**
 * Reading and changing what a file or folder is tagged with
 * ([F-003](../../../../features/F-003-tagging.md)).
 *
 * One module for both subjects, because the vocabulary is one vocabulary: a folder's tags come
 * from the same taxonomy and render with the same chips. What differs is small and stated once
 * here — a folder tag is always a person's word, so there is no machine claim to confirm
 * ([F-015/FR-9](../../../../features/F-015-folders.md)).
 *
 * Every mutation invalidates the *file* query as well as the tag list: the file summary embeds
 * its tags, and a page showing both must not show them disagreeing.
 */
import {
  confirmFileTag,
  readFileTags,
  readFolderTags,
  tagFile,
  tagFolder,
  untagFile,
  untagFolder,
  type AppliedTag,
} from '@store-everything/api-client';
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query';
import { computed, type ComputedRef, type Ref } from 'vue';

import { toFailure } from '@/shared';

/** What is being tagged. The API paths differ; nothing else about the surface does. */
export type Subject = { kind: 'file'; id: string } | { kind: 'folder'; id: string };

export interface TagChange {
  /** Either a tag chosen from a completion, or a name a person typed. */
  tag?: string;
  name?: string;
}

export function tagsQueryKey(subject: Subject): string[] {
  return [subject.kind, subject.id, 'tags'];
}

async function read(subject: Subject): Promise<AppliedTag[]> {
  const request =
    subject.kind === 'file'
      ? readFileTags({ path: { file_id: subject.id } })
      : readFolderTags({ path: { folder_id: subject.id } });
  const { data, error, response } = await request;
  if (error !== undefined) throw toFailure(error, response?.status);
  return data ?? [];
}

export function useTags(subject: ComputedRef<Subject> | Ref<Subject>) {
  return useQuery({
    queryKey: computed(() => tagsQueryKey(subject.value)),
    queryFn: () => read(subject.value),
  });
}

/**
 * Apply, remove and confirm, as three mutations over one invalidation.
 *
 * They are returned together rather than as separate composables because a surface that can add
 * a tag can always take it off again, and keeping the cache handling in one place is what stops
 * one of the three forgetting to refresh the file it changed.
 */
export function useTagging(subject: ComputedRef<Subject> | Ref<Subject>) {
  const cache = useQueryClient();

  async function refresh(): Promise<void> {
    await Promise.all([
      cache.invalidateQueries({ queryKey: tagsQueryKey(subject.value) }),
      cache.invalidateQueries({ queryKey: [subject.value.kind, subject.value.id] }),
    ]);
  }

  const apply = useMutation({
    mutationFn: async (change: TagChange) => {
      const body = change.tag !== undefined ? { tag: change.tag } : { name: change.name ?? '' };
      const request =
        subject.value.kind === 'file'
          ? tagFile({ path: { file_id: subject.value.id }, body })
          : tagFolder({ path: { folder_id: subject.value.id }, body });
      const { data, error, response } = await request;
      if (error !== undefined) throw toFailure(error, response?.status);
      return data;
    },
    onSuccess: refresh,
  });

  const remove = useMutation({
    mutationFn: async (tagId: string) => {
      const request =
        subject.value.kind === 'file'
          ? untagFile({ path: { file_id: subject.value.id, tag_id: tagId } })
          : untagFolder({ path: { folder_id: subject.value.id, tag_id: tagId } });
      const { error, response } = await request;
      if (error !== undefined) throw toFailure(error, response?.status);
    },
    onSuccess: refresh,
  });

  const confirm = useMutation({
    mutationFn: async (tagId: string) => {
      if (subject.value.kind !== 'file') return undefined;
      const { data, error, response } = await confirmFileTag({
        path: { file_id: subject.value.id, tag_id: tagId },
      });
      if (error !== undefined) throw toFailure(error, response?.status);
      return data;
    },
    onSuccess: refresh,
  });

  return { apply, remove, confirm };
}

/** How a chip reads. The words are the person's question, not the table's column. */
export const PROVENANCE_LABELS: Record<string, string> = {
  manual: 'Added by hand',
  confirmed: 'Confirmed',
  auto: 'Detected',
};

/**
 * What a tag's confidence says as a percentage, or nothing at all.
 *
 * A claim without a confidence is common — plenty of extractors know a thing without scoring it —
 * and inventing a number for it would be worse than showing none (F-003/FR-3).
 */
export function confidenceLabel(applied: AppliedTag): string | undefined {
  const confidence = applied.source?.confidence;
  if (confidence === null || confidence === undefined) return undefined;
  return `${Math.round(confidence * 100)}%`;
}
