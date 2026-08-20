import type { Meta, StoryObj } from '@storybook/vue3-vite';

import AppButton from './AppButton.vue';

const meta = {
  title: 'Shared/AppButton',
  component: AppButton,
  tags: ['autodocs'],
} satisfies Meta<typeof AppButton>;

export default meta;

type Story = StoryObj<typeof meta>;

const render = (args: Record<string, unknown>, label: string) => ({
  components: { AppButton },
  setup: () => ({ args }),
  template: `<AppButton v-bind="args">${label}</AppButton>`,
});

export const Primary: Story = {
  args: { variant: 'primary' },
  render: (args) => render(args, 'Save changes'),
};

export const Quiet: Story = {
  args: { variant: 'quiet' },
  render: (args) => render(args, 'Check again'),
};

export const Disabled: Story = {
  args: { variant: 'primary', disabled: true },
  render: (args) => render(args, 'Unavailable'),
};
