import { createRouter, createWebHistory, type Router } from 'vue-router';

import HomeView from '@/views/HomeView.vue';

export function createAppRouter(): Router {
  return createRouter({
    history: createWebHistory(),
    routes: [{ path: '/', name: 'home', component: HomeView }],
  });
}
