import { VueQueryPlugin } from '@tanstack/vue-query';
import { createPinia } from 'pinia';
import { createApp } from 'vue';

import App from './App.vue';
import { createAppRouter } from './router';
import { configureApiClient } from '@/shared';
import './styles/tokens.css';

configureApiClient();

createApp(App).use(createPinia()).use(createAppRouter()).use(VueQueryPlugin).mount('#app');
