<script setup lang="ts">
/**
 * Signing in ([F-027/FR-4](../../../../features/F-027-web-application-shell.md)).
 *
 * The server's own message is shown verbatim on a rejected credential, and deliberately: it does
 * not distinguish "no such account" from "wrong password", and neither may this
 * ([07 § abuse protection](../../../../specs/07-identity-permissions-sharing.md)). Improving the
 * wording here would be a disclosure.
 */
import { useRouter } from 'vue-router';
import { ref } from 'vue';

import { useSessionStore, useSignIn } from './session';
import InstanceStatus from '@/features/instance/InstanceStatus.vue';
import { useInstanceReadiness } from '@/features/instance/readiness';
import { AppAlert, AppButton, AppCard, AppField, fieldFailure } from '@/shared';

const router = useRouter();
const store = useSessionStore();
const { submit, failure, isPending } = useSignIn();

const { state: readiness } = useInstanceReadiness();

const email = ref('');
const password = ref('');

async function onSubmit(): Promise<void> {
  if (!(await submit(email.value, password.value))) return;
  const destination = store.intended ?? '/';
  store.intended = undefined;
  await router.replace(destination);
}
</script>

<template>
  <div class="mx-auto mt-16 max-w-sm">
    <h1 class="mb-6 text-center text-xl font-semibold">Store Everything</h1>
    <AppCard>
      <form class="flex flex-col gap-4" @submit.prevent="onSubmit">
        <AppField
          v-model="email"
          label="Email"
          type="email"
          autocomplete="username"
          required
          :error="fieldFailure(failure, '/body/email')"
        />
        <AppField
          v-model="password"
          label="Password"
          type="password"
          autocomplete="current-password"
          required
          :error="fieldFailure(failure, '/body/password')"
        />
        <AppAlert v-if="failure" :failure="failure" />
        <AppButton type="submit" :disabled="isPending">
          {{ isPending ? 'Signing in…' : 'Sign in' }}
        </AppButton>
      </form>
    </AppCard>
    <!-- Why a sign-in might be failing for reasons that are not the password: `/readyz` answers
         503 while the database is unreachable or migrations are pending. -->
    <div class="mt-4 flex justify-center">
      <InstanceStatus :state="readiness" />
    </div>
  </div>
</template>
