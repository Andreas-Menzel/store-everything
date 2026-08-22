<script setup lang="ts">
/**
 * The frame ([F-027/FR-10](../../features/F-027-web-application-shell.md)): which instance, who
 * is signed in, where you are, and a way out — on every authenticated surface.
 *
 * Login renders without it, because a frame that named a user before there was one would be
 * decoration. The navigation lists only what this phase has: a surface advertising a capability
 * the API does not offer is worse than an absent one.
 *
 * It also watches for the session ending (FR-6). The route guard decides *before* a surface
 * renders, which cannot help a session that expires while someone is reading it — the `401` that
 * discovers it may arrive from any request on any surface. Leaving that to each surface would
 * mean every one of them growing a branch for it, and the ones that forgot would show a wall of
 * failures where a login form belongs.
 */
import { watch } from 'vue';
import { RouterView, useRoute, useRouter } from 'vue-router';

import { useSession, useSessionStore } from '@/features/auth/session';
import AppHeader from '@/features/shell/AppHeader.vue';

const route = useRoute();
const router = useRouter();
const store = useSessionStore();
const { isSignedIn, isResolved } = useSession();

watch([isResolved, isSignedIn], async ([resolved, signedIn]) => {
  if (!resolved || signedIn || route.meta.public === true) return;
  store.intended = route.fullPath;
  await router.replace({ name: 'login' });
});
</script>

<template>
  <AppHeader v-if="isSignedIn && route.meta.public !== true" />
  <main class="mx-auto max-w-5xl p-6">
    <RouterView />
  </main>
</template>
