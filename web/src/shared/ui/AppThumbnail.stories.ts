import type { Meta, StoryObj } from '@storybook/vue3-vite';

import AppThumbnail from './AppThumbnail.vue';

/**
 * The three states a file's picture can be in, which is the whole reason this component exists:
 * a grid must look right before any thumbnail arrives, after it arrives, and for a file that
 * will never have one.
 */
const meta = {
  title: 'Shared/AppThumbnail',
  component: AppThumbnail,
  tags: ['autodocs'],
  decorators: [
    () => ({ template: '<div class="h-32 w-32 rounded-md overflow-hidden"><story /></div>' }),
  ],
} satisfies Meta<typeof AppThumbnail>;

export default meta;

type Story = StoryObj<typeof meta>;

/** The real placeholder of the corpus fixture: red on the left, blue on the right. */
const TWO_TONE = 'AQQDAyABkM4fJcIiLSdAmRtDoc4fJcIiLSdAmRtDoc4fJcIiLSdAmRtDoQ';

export const Placeholder: Story = { args: { placeholder: TWO_TONE } };

export const Loaded: Story = {
  args: {
    placeholder: TWO_TONE,
    alt: 'A two-tone fixture image',
    // An inline SVG stands in for the served WebP: the story is about the component, and it
    // must not depend on an instance being up.
    src:
      'data:image/svg+xml;utf8,' +
      encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">' +
          '<rect width="100" height="100" fill="#cb2027"/>' +
          '<rect x="100" width="100" height="100" fill="#1e429f"/></svg>',
      ),
  },
};

export const NothingToRender: Story = {
  args: {},
  render: (args) => ({
    components: { AppThumbnail },
    setup: () => ({ args }),
    template: '<AppThumbnail v-bind="args"><template #fallback>BIN</template></AppThumbnail>',
  }),
};
