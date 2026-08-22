import { currentIdentity, login, logout } from '@store-everything/api-client';
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/vue-query';
import { defineStore } from 'pinia';
import { computed, ref, type ComputedRef, type Ref } from 'vue';

import { toFailure, type Failure } from '@/shared';

/**
 * Who is signed in — the one answer every guarded surface waits for.
 *
 * The session lives in an `HttpOnly` cookie, so the client cannot read it and does not try
 * ([F-027/FR-3](../../../../features/F-027-web-application-shell.md)). "Am I signed in?" is
 * therefore a question for the server, asked through `GET /auth/me` and cached — not a flag this
 * code keeps, which could disagree with the cookie.
 *
 * A `401` is an *answer* here, not an exception: it means nobody is signed in, which the app
 * renders as the login form rather than reporting as a failure.
 */

export interface Identity {
  id: string;
  email: string;
  display_name: string;
  role: 'admin' | 'member';
}

export const useSessionStore = defineStore('session', () => {
  /** Where to return after signing in. Set when a guard turns someone away. */
  const intended = ref<string | undefined>(undefined);
  /**
   * Which session epoch we are in. Bumped by a sign-in, a sign-out, or the server ceasing to
   * recognise us — each of which makes every cached answer suspect, and the key change is what
   * makes the identity be asked again.
   */
  const epoch = ref(0);

  return { intended, epoch };
});

/** The identity query, in one place: the guard and the frame must not ask differently. */
export function sessionQuery(epoch: number) {
  return {
    queryKey: ['session', epoch] as const,
    queryFn: async (): Promise<Identity | null> => {
      const { data } = await currentIdentity();
      return (data as Identity | undefined) ?? null;
    },
    retry: false,
    staleTime: Infinity,
  };
}

export function useSession(): {
  identity: ComputedRef<Identity | undefined>;
  isResolved: ComputedRef<boolean>;
  isSignedIn: ComputedRef<boolean>;
} {
  const store = useSessionStore();
  const query = useQuery(computed(() => sessionQuery(store.epoch)));

  return {
    identity: computed(() => query.data.value ?? undefined),
    isResolved: computed(() => !query.isPending.value),
    isSignedIn: computed(() => Boolean(query.data.value)),
  };
}

/**
 * What a `401` from *any* request means: this session is over
 * ([F-027/FR-6](../../../../features/F-027-web-application-shell.md)).
 *
 * Handling it per call site would mean every surface growing a branch for it, and the ones that
 * forgot would show a wall of failures where a login form belongs.
 *
 * The guard on "did we think we were signed in" is what keeps this from looping: the identity
 * request itself answers `401` when nobody is, and reacting to *that* would change the query key,
 * refetch, and arrive here again forever.
 */
export function endSession(cache: QueryClient, epoch: () => number, bump: () => void): void {
  const believed = cache.getQueryData(sessionQuery(epoch()).queryKey);
  if (!believed) return;
  cache.clear();
  bump();
}

export function useSignIn(): {
  submit: (email: string, password: string) => Promise<boolean>;
  failure: Ref<Failure | undefined>;
  isPending: Ref<boolean>;
} {
  const store = useSessionStore();
  const cache = useQueryClient();
  const failure = ref<Failure | undefined>(undefined);
  const isPending = ref(false);

  async function submit(email: string, password: string): Promise<boolean> {
    // Guarded here rather than only disabled in the template: a keyboard can submit a form twice
    // before a disabled attribute lands (F-027/FR-4).
    if (isPending.value) return false;
    isPending.value = true;
    failure.value = undefined;
    try {
      const { error, response } = await login({ body: { email, password } });
      if (error !== undefined) {
        failure.value = toFailure(error, response?.status);
        return false;
      }
      // A new session is a new cache: nothing the previous visitor loaded may survive it (FR-7).
      cache.clear();
      store.epoch += 1;
      return true;
    } finally {
      isPending.value = false;
    }
  }

  return { submit, failure, isPending };
}

export function useSignOut(): { submit: () => Promise<void> } {
  const store = useSessionStore();
  const cache = useQueryClient();

  return {
    submit: async () => {
      await logout();
      cache.clear();
      store.epoch += 1;
    },
  };
}
