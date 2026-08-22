<script setup lang="ts">
/**
 * A client route that does not exist. The server answered with the app rather than a `404`
 * because it cannot know the app's routes (F-027/FR-1) — so saying so is the app's job.
 */
import { AppEmpty } from '@/shared';
</script>

<template>
  <h1 class="mb-4 text-lg font-semibold">Not found</h1>
  <AppEmpty title="There is nothing at this address" detail="The link may be out of date." />
</template>
