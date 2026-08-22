import type { Meta, StoryObj } from '@storybook/vue3-vite';

import AppButton from './AppButton.vue';
import AppCard from './AppCard.vue';

const meta = {
  title: 'Shared/AppCard',
  component: AppCard,
  tags: ['autodocs'],
} satisfies Meta<typeof AppCard>;

export default meta;

type Story = StoryObj<typeof meta>;

export const Titled: Story = {
  args: { title: 'Import' },
  render: (args) => ({
    components: { AppCard },
    setup: () => ({ args }),
    template: '<AppCard v-bind="args">Nothing has been scanned yet.</AppCard>',
  }),
};

export const WithAction: Story = {
  args: { title: 'Files' },
  render: (args) => ({
    components: { AppCard, AppButton },
    setup: () => ({ args }),
    template:
      '<AppCard v-bind="args"><template #actions><AppButton variant="quiet">Rescan</AppButton></template>Browse this workspace</AppCard>',
  }),
};

export const Untitled: Story = {
  render: () => ({
    components: { AppCard },
    template: '<AppCard>A surface with no heading of its own.</AppCard>',
  }),
};
