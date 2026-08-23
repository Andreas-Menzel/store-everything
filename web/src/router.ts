import type { QueryClient } from '@tanstack/vue-query';
import { createRouter, createWebHistory, type Router } from 'vue-router';

import { sessionQuery, useSessionStore } from '@/features/auth/session';

/**
 * Routes, and the guard that decides before a surface renders rather than after it fails
 * ([F-027/FR-5](../../features/F-027-web-application-shell.md)).
 *
 * The guard asks the *cache* for the identity, so the question costs one request per session and
 * the answer is the same one the frame shows. Deciding up front is what keeps an unauthenticated
 * deep link from rendering a page full of `401`s before redirecting.
 *
 * The documentation route is loaded on demand: its viewer is the largest dependency in the app,
 * and someone browsing their files should not download it (FR-9).
 */
export function createAppRouter(cache: QueryClient): Router {
  const router = createRouter({
    history: createWebHistory(),
    routes: [
      {
        path: '/login',
        name: 'login',
        component: () => import('@/features/auth/LoginView.vue'),
        meta: { public: true },
      },
      {
        path: '/',
        name: 'workspaces',
        component: () => import('@/features/workspaces/WorkspacesView.vue'),
      },
      {
        path: '/workspaces/:id',
        name: 'workspace',
        component: () => import('@/features/workspaces/WorkspaceView.vue'),
      },
      {
        path: '/folders/:id',
        name: 'folder',
        component: () => import('@/features/folders/FolderView.vue'),
      },
      {
        path: '/files/:id',
        name: 'file',
        component: () => import('@/features/files/FileView.vue'),
      },
      {
        path: '/tags',
        name: 'tags',
        component: () => import('@/features/tags/TaxonomyView.vue'),
      },
      {
        path: '/docs',
        name: 'docs',
        component: () => import('@/features/docs/DocsView.vue'),
      },
      {
        path: '/:path(.*)*',
        name: 'not-found',
        component: () => import('@/features/shell/NotFoundView.vue'),
      },
    ],
  });

  router.beforeEach(async (to) => {
    const store = useSessionStore();
    if (to.meta.public === true) return true;

    const identity = await cache.ensureQueryData(sessionQuery(store.epoch));
    if (identity) return true;

    // Remembered so signing in continues where the person was going, not at the front door.
    store.intended = to.fullPath;
    return { name: 'login' };
  });

  return router;
}
