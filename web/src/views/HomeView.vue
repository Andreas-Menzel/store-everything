<script setup lang="ts">
import InstanceStatus from '@/features/instance/InstanceStatus.vue';
import { useInstanceReadiness } from '@/features/instance/readiness';
import { AppButton } from '@/shared';

const { state, refresh } = useInstanceReadiness();
</script>

<template>
  <section
    class="rounded-(--radius-control) bg-(--color-surface) p-6 ring-1 ring-(--color-border-subtle)"
  >
    <h1 class="text-lg font-semibold">Store Everything</h1>
    <p class="mt-1 text-sm text-(--color-ink-muted)">
      No features are implemented yet. This shell exists so the client, the tests and the lint rules
      are in place before the first one is.
    </p>

    <div class="mt-4 flex items-center gap-4">
      <InstanceStatus :state="state" />
      <AppButton variant="quiet" @click="refresh">Check again</AppButton>
    </div>
  </section>
</template>
