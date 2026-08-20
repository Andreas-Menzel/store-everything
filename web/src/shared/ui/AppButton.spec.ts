import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import AppButton from './AppButton.vue';

describe('AppButton', () => {
  it('renders its content', () => {
    const wrapper = mount(AppButton, { slots: { default: 'Check again' } });

    expect(wrapper.text()).toBe('Check again');
  });

  it('defaults to type=button so it never submits a form by accident', () => {
    const wrapper = mount(AppButton);

    expect(wrapper.get('button').attributes('type')).toBe('button');
  });

  it('passes disabled through to the element, not just the styling', () => {
    const wrapper = mount(AppButton, { props: { disabled: true } });

    expect(wrapper.get('button').attributes('disabled')).toBeDefined();
  });

  it('treats the variant as data rather than a different component', () => {
    const primary = mount(AppButton).get('button').classes().join(' ');
    const quiet = mount(AppButton, { props: { variant: 'quiet' } })
      .get('button')
      .classes()
      .join(' ');

    expect(primary).not.toBe(quiet);
  });

  it('emits clicks to its parent', async () => {
    const wrapper = mount(AppButton);

    await wrapper.get('button').trigger('click');

    expect(wrapper.emitted('click')).toHaveLength(1);
  });
});
