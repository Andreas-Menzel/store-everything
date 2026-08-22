import type { Meta, StoryObj } from '@storybook/vue3-vite';

import AppAlert from './AppAlert.vue';

const meta = {
  title: 'Shared/AppAlert',
  component: AppAlert,
  tags: ['autodocs'],
} satisfies Meta<typeof AppAlert>;

export default meta;

type Story = StoryObj<typeof meta>;

/** What the API actually sends: a title, a detail, and the request id worth quoting. */
export const Problem: Story = {
  args: {
    failure: {
      title: 'Conflict',
      detail: 'A folder named "Album" is already there.',
      instance: 'req_79b2f7f0fa2c4d87',
      status: 409,
      fields: [],
    },
  },
};

export const Unexplained: Story = { args: {} };

export const Caution: Story = { args: { tone: 'caution', title: 'This file is in the trash' } };

export const Neutral: Story = { args: { tone: 'neutral', title: 'Nothing has been scanned yet' } };
