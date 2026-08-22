import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import AppAlert from './AppAlert.vue';
import type { Failure } from '../api/problem';

const FAILURE: Failure = {
  title: 'Conflict',
  detail: 'A folder named "Album" is already there.',
  instance: 'req_abc123',
  status: 409,
  fields: [],
};

describe('AppAlert', { tags: ['@F-027/FR-8'] }, () => {
  it('renders the problem as prose, request id included', () => {
    const wrapper = mount(AppAlert, { props: { failure: FAILURE } });

    expect(wrapper.text()).toContain('Conflict');
    expect(wrapper.text()).toContain('already there');
    expect(wrapper.text()).toContain('req_abc123');
  });

  it('is announced when it is a failure, and merely status otherwise', () => {
    const critical = mount(AppAlert, { props: { failure: FAILURE } });
    const caution = mount(AppAlert, { props: { tone: 'caution', title: 'In the trash' } });

    expect(critical.get('div').attributes('role')).toBe('alert');
    expect(caution.get('div').attributes('role')).toBe('status');
  });

  it('still says something when the failure could not be parsed', () => {
    const wrapper = mount(AppAlert);

    expect(wrapper.text()).toContain('Something went wrong');
  });

  it('prefers an explicit title over the problem’s own', () => {
    const wrapper = mount(AppAlert, {
      props: { failure: FAILURE, title: 'Could not create that folder' },
    });

    expect(wrapper.text()).toContain('Could not create that folder');
  });
});
