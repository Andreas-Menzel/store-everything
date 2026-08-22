import type { Meta, StoryObj } from '@storybook/vue3-vite';

import AppButton from './AppButton.vue';
import AppEmpty from './AppEmpty.vue';

const meta = {
  title: 'Shared/AppEmpty',
  component: AppEmpty,
  tags: ['autodocs'],
} satisfies Meta<typeof AppEmpty>;

export default meta;

type Story = StoryObj<typeof meta>;

export const Bare: Story = { args: { title: 'This folder is empty' } };

export const Explained: Story = {
  args: {
    title: 'No workspaces yet',
    detail: 'A workspace is a tree of your files. Create one to start putting things in it.',
  },
};

export const WithAction: Story = {
  args: { title: 'No workspaces yet' },
  render: (args) => ({
    components: { AppEmpty, AppButton },
    setup: () => ({ args }),
    template: '<AppEmpty v-bind="args"><AppButton>Create one</AppButton></AppEmpty>',
  }),
};
