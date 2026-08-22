import type { Meta, StoryObj } from '@storybook/vue3-vite';

import AppSpinner from './AppSpinner.vue';

const meta = {
  title: 'Shared/AppSpinner',
  component: AppSpinner,
  tags: ['autodocs'],
} satisfies Meta<typeof AppSpinner>;

export default meta;

type Story = StoryObj<typeof meta>;

export const Default: Story = { args: {} };

/** The label is the accessible name, so it says what is being waited for. */
export const Labelled: Story = { args: { label: 'Loading workspaces' } };
