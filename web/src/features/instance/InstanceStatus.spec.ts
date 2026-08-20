import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import InstanceStatus from './InstanceStatus.vue';
import type { ReadinessState } from './readiness';

describe('InstanceStatus', () => {
  const cases: Array<[ReadinessState, string]> = [
    ['pending', 'Checking the instance…'],
    ['ready', 'Instance ready'],
    ['unavailable', 'Instance unavailable'],
  ];

  it.each(cases)('describes the %s state in words', (state, expected) => {
    const wrapper = mount(InstanceStatus, { props: { state } });

    expect(wrapper.text()).toContain(expected);
  });

  it('exposes the state as data so it is assertable end to end', () => {
    const wrapper = mount(InstanceStatus, { props: { state: 'ready' } });

    expect(wrapper.get('[data-state]').attributes('data-state')).toBe('ready');
  });

  it('never conveys the state by colour alone', () => {
    const wrapper = mount(InstanceStatus, { props: { state: 'unavailable' } });

    // The dot is decorative; the words carry the meaning.
    expect(wrapper.find('[aria-hidden="true"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('Instance unavailable');
  });
});
