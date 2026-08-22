import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import AppField from './AppField.vue';

describe('AppField', { tags: ['@F-027/FR-8'] }, () => {
  it(
    'associates the label with the input, so clicking it focuses the field',
    { tags: ['@F-027/FR-12'] },
    () => {
      const wrapper = mount(AppField, { props: { label: 'Email' } });

      const id = wrapper.get('input').attributes('id');
      expect(id).toBeTruthy();
      expect(wrapper.get('label').attributes('for')).toBe(id);
    },
  );

  it('announces an error rather than only colouring the border', { tags: ['@F-027/FR-12'] }, () => {
    const wrapper = mount(AppField, {
      props: { label: 'Email', error: 'that address is already taken' },
    });

    const input = wrapper.get('input');
    expect(input.attributes('aria-invalid')).toBe('true');
    const described = input.attributes('aria-describedby');
    expect(described).toBeTruthy();
    expect(wrapper.get(`#${described}`).text()).toBe('that address is already taken');
  });

  it('describes itself with both the hint and the error when it has both', () => {
    const wrapper = mount(AppField, {
      props: { label: 'Name', hint: 'becomes a directory', error: 'already taken' },
    });

    const described = wrapper.get('input').attributes('aria-describedby')?.split(' ') ?? [];
    expect(described).toHaveLength(2);
  });

  it('says nothing about validity when there is nothing wrong', () => {
    const wrapper = mount(AppField, { props: { label: 'Email' } });

    expect(wrapper.get('input').attributes('aria-invalid')).toBeUndefined();
    expect(wrapper.get('input').attributes('aria-describedby')).toBeUndefined();
  });

  it('carries the value both ways', async () => {
    const wrapper = mount(AppField, { props: { label: 'Email', modelValue: 'a@b.c' } });
    expect((wrapper.get('input').element as HTMLInputElement).value).toBe('a@b.c');

    await wrapper.get('input').setValue('d@e.f');

    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['d@e.f']);
  });
});
