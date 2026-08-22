import type { Meta, StoryObj } from '@storybook/vue3-vite';

import AppField from './AppField.vue';

const meta = {
  title: 'Shared/AppField',
  component: AppField,
  tags: ['autodocs'],
} satisfies Meta<typeof AppField>;

export default meta;

type Story = StoryObj<typeof meta>;

export const Plain: Story = { args: { label: 'Workspace name' } };

export const WithHint: Story = {
  args: { label: 'Workspace name', hint: 'Becomes a directory on the storage.' },
};

export const Rejected: Story = {
  args: { label: 'Workspace name', error: 'A workspace of that name already exists.' },
};

export const Password: Story = {
  args: { label: 'Password', type: 'password', autocomplete: 'current-password' },
};

export const Disabled: Story = { args: { label: 'Email', disabled: true } };
