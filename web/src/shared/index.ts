/**
 * The shared layer's public surface (11-engineering-standards.md § code reuse).
 *
 * A feature imports from here, never from a path inside it, and nothing here imports from a
 * feature — the import direction is one-way and lint enforces it.
 */
export { default as AppAlert } from './ui/AppAlert.vue';
export { default as AppButton } from './ui/AppButton.vue';
export { default as AppCard } from './ui/AppCard.vue';
export { default as AppEmpty } from './ui/AppEmpty.vue';
export { default as AppField } from './ui/AppField.vue';
export { default as AppSpinner } from './ui/AppSpinner.vue';
export { configureApiClient, onSessionEnded } from './api/client';
export { fieldFailure, isUnauthenticated, toFailure } from './api/problem';
export type { Failure, FieldProblem } from './api/problem';
