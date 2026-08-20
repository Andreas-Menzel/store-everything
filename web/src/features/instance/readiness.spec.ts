import { VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { defineComponent, h } from 'vue';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useInstanceReadiness } from './readiness';

const { readyz } = vi.hoisted(() => ({ readyz: vi.fn() }));

vi.mock('@store-everything/api-client', () => ({ readyz }));

const Probe = defineComponent({
  setup() {
    const { state } = useInstanceReadiness();
    return () => h('span', state.value);
  },
});

function mountProbe() {
  return mount(Probe, { global: { plugins: [VueQueryPlugin] } });
}

describe('useInstanceReadiness', () => {
  beforeEach(() => {
    readyz.mockReset();
  });

  it('starts out pending rather than claiming a state it has not checked', () => {
    readyz.mockReturnValue(new Promise(() => {}));

    expect(mountProbe().text()).toBe('pending');
  });

  it('reports ready when the server says so', async () => {
    readyz.mockResolvedValue({ data: { status: 'ready' }, error: undefined });

    const wrapper = mountProbe();
    await flushPromises();

    expect(wrapper.text()).toBe('ready');
  });

  it('treats a 503 as an answer, not a crash', async () => {
    // /readyz answers 503 while migrations are pending — a legitimate state to render.
    readyz.mockResolvedValue({ data: undefined, error: { status: 503 } });

    const wrapper = mountProbe();
    await flushPromises();

    expect(wrapper.text()).toBe('unavailable');
  });

  it('reports unavailable when the request itself fails', async () => {
    readyz.mockRejectedValue(new Error('offline'));

    const wrapper = mountProbe();
    await flushPromises();

    expect(wrapper.text()).toBe('unavailable');
  });
});
