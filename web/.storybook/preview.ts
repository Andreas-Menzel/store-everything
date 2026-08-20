import type { Preview } from '@storybook/vue3-vite';

// Stories render against the real design tokens, never a parallel set.
import '../src/styles/tokens.css';

const preview: Preview = {
  parameters: {
    controls: { expanded: true },
  },
};

export default preview;
