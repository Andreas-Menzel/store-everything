<script setup lang="ts">
/**
 * Putting files into a folder ([F-001/FR-1](../../../features/F-001-upload-and-import.md)).
 *
 * Progress is reported per file from the *server's* committed offset, which is what makes the
 * number honest: a browser can hand megabytes to the network and lose them. A failed file keeps
 * its place in the list with the server's reason attached, because the useful thing after
 * uploading forty files is knowing which one the storage refused and why.
 */
import { ref } from 'vue';

import { upload, UploadFailed, type Progress } from './resumable';
import { AppAlert, AppButton, AppCard, type Failure } from '@/shared';

const props = defineProps<{ workspaceId: string; folderPath: string }>();
const emit = defineEmits<{ uploaded: [] }>();

interface Item {
  name: string;
  progress: Progress;
  state: 'sending' | 'done' | 'failed';
  failure?: Failure;
}

const items = ref<Item[]>([]);
const isSending = ref(false);
const input = ref<HTMLInputElement | null>(null);

function targetPath(name: string): string {
  return props.folderPath ? `${props.folderPath}/${name}` : name;
}

async function send(files: FileList): Promise<void> {
  isSending.value = true;
  try {
    for (const file of Array.from(files)) {
      const item: Item = {
        name: file.name,
        progress: { sent: 0, total: file.size },
        state: 'sending',
      };
      items.value = [...items.value, item];
      try {
        await upload(props.workspaceId, targetPath(file.name), file, {
          onProgress: (progress) => {
            item.progress = progress;
          },
        });
        item.state = 'done';
      } catch (error) {
        item.state = 'failed';
        item.failure = error instanceof UploadFailed ? error.failure : undefined;
      }
    }
    emit('uploaded');
  } finally {
    isSending.value = false;
    if (input.value) input.value.value = '';
  }
}

function onPicked(event: Event): void {
  const picked = (event.target as HTMLInputElement).files;
  if (picked && picked.length > 0) void send(picked);
}

function onDropped(event: DragEvent): void {
  const dropped = event.dataTransfer?.files;
  if (dropped && dropped.length > 0) void send(dropped);
}

function percent(item: Item): number {
  return item.progress.total === 0
    ? 100
    : Math.round((item.progress.sent / item.progress.total) * 100);
}
</script>

<template>
  <AppCard title="Upload">
    <div
      class="rounded-(--radius-control) border border-dashed border-(--color-border-subtle) p-6 text-center"
      @dragover.prevent
      @drop.prevent="onDropped"
    >
      <p class="text-sm text-(--color-ink-muted)">Drop files here, or choose them.</p>
      <!-- eslint-disable-next-line vue/no-restricted-html-elements -->
      <label class="mt-3 inline-block">
        <span class="sr-only">Choose files to upload</span>
        <input ref="input" type="file" multiple class="text-sm" @change="onPicked" />
      </label>
    </div>

    <ul v-if="items.length > 0" class="mt-4 flex flex-col gap-2 text-sm">
      <li v-for="(item, index) in items" :key="`${item.name}-${index}`">
        <div class="flex items-center justify-between gap-4">
          <span class="truncate">{{ item.name }}</span>
          <span class="text-xs text-(--color-ink-muted)">
            {{ item.state === 'failed' ? 'failed' : `${percent(item)}%` }}
          </span>
        </div>
        <AppAlert
          v-if="item.state === 'failed'"
          class="mt-1"
          :failure="item.failure"
          :title="item.failure ? undefined : 'The upload did not finish'"
        />
      </li>
    </ul>
    <p v-if="isSending" class="mt-3 text-xs text-(--color-ink-muted)">Uploading…</p>
    <template #actions>
      <AppButton v-if="items.length > 0 && !isSending" variant="quiet" @click="items = []">
        Clear
      </AppButton>
    </template>
  </AppCard>
</template>
