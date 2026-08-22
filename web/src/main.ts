import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query';
import { createPinia } from 'pinia';
import { createApp } from 'vue';

import App from './App.vue';
import { createAppRouter } from './router';
import { endSession, useSessionStore } from '@/features/auth/session';
import { configureApiClient, onSessionEnded } from '@/shared';
import './styles/tokens.css';

/**
 * Composition root: the only place that wires transport, cache, state and routing together.
 *
 * The query client is created here rather than left to the plugin because the router's guard
 * needs it too — deciding whether someone may see a surface has to ask the same cached answer the
 * frame shows, not a second copy of it
 * ([F-027/FR-5](../../features/F-027-web-application-shell.md), FR-11).
 */
configureApiClient();

const cache = new QueryClient({
  defaultOptions: {
    // A failed request is usually an answer here — `401` means "not signed in", `404` means "not
    // yours" — and retrying an answer just delays showing it.
    queries: { retry: false, refetchOnWindowFocus: false },
  },
});

const pinia = createPinia();
const app = createApp(App).use(pinia).use(VueQueryPlugin, { queryClient: cache });

const store = useSessionStore(pinia);
// Any request may be the one that discovers the session is over (FR-6).
onSessionEnded(() =>
  endSession(
    cache,
    () => store.epoch,
    () => {
      store.epoch += 1;
    },
  ),
);

app.use(createAppRouter(cache)).mount('#app');
