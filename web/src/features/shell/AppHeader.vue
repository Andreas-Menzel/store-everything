<script setup lang="ts">
/**
 * The bar across the top: the instance, where you are, who you are, and a way out.
 *
 * `aria-current` is what tells assistive technology which link is the page you are on — colour
 * alone says it only to people who can see it
 * ([F-027/FR-12](../../../features/F-027-web-application-shell.md)).
 */
import { RouterLink, useRouter } from 'vue-router';

import { useSession, useSignOut } from '@/features/auth/session';
import { AppButton } from '@/shared';

const router = useRouter();
const { identity } = useSession();
const { submit } = useSignOut();

async function signOut(): Promise<void> {
  await submit();
  await router.replace({ name: 'login' });
}
</script>

<template>
  <header class="border-b border-(--color-border-subtle) bg-(--color-surface)">
    <div class="mx-auto flex max-w-5xl items-center gap-6 px-6 py-3">
      <RouterLink :to="{ name: 'workspaces' }" class="text-sm font-semibold">
        Store Everything
      </RouterLink>
      <nav aria-label="Sections" class="flex items-center gap-4 text-sm">
        <RouterLink
          :to="{ name: 'workspaces' }"
          class="text-(--color-ink-muted) hover:text-(--color-ink) aria-[current=page]:text-(--color-ink) aria-[current=page]:underline"
        >
          Workspaces
        </RouterLink>
        <RouterLink
          :to="{ name: 'docs' }"
          class="text-(--color-ink-muted) hover:text-(--color-ink) aria-[current=page]:text-(--color-ink) aria-[current=page]:underline"
        >
          API
        </RouterLink>
      </nav>
      <div class="ml-auto flex items-center gap-3 text-sm">
        <span class="text-(--color-ink-muted)">{{ identity?.email }}</span>
        <AppButton variant="quiet" @click="signOut">Sign out</AppButton>
      </div>
    </div>
  </header>
</template>
