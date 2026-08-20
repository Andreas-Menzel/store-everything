import { readyz } from '@store-everything/api-client';
import { useQuery } from '@tanstack/vue-query';
import { computed, type ComputedRef } from 'vue';

export type ReadinessState = 'pending' | 'ready' | 'unavailable';

/**
 * Instance readiness, as the server reports it.
 *
 * `/readyz` answers 503 while the database is unreachable or migrations are pending, so
 * a failed request is a legitimate answer here, not an exception to swallow.
 */
export function useInstanceReadiness(): {
  state: ComputedRef<ReadinessState>;
  refresh: () => void;
} {
  const query = useQuery({
    queryKey: ['instance', 'readiness'],
    queryFn: async () => {
      const { data, error } = await readyz();
      if (error !== undefined || data === undefined) {
        throw new Error('instance not ready');
      }
      return data;
    },
    retry: false,
  });

  const state = computed<ReadinessState>(() => {
    if (query.isPending.value) return 'pending';
    return query.isSuccess.value ? 'ready' : 'unavailable';
  });

  return {
    state,
    refresh: () => {
      void query.refetch();
    },
  };
}
