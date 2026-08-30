import { VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import TagPicker from './TagPicker.vue';

/**
 * Completing a tag from what somebody typed ([F-003/FR-8](../../../../features/F-003-tagging.md)).
 *
 * Three things are worth a test here, and none of them is the happy path being pretty: a
 * keystroke must not be a request, the keyboard must be able to do everything the mouse can, and
 * a word the server does not know must still be *sendable* — the server decides what a name
 * means, through synonyms or with a refusal, and guessing locally would invent vocabulary the
 * taxonomy never approved.
 */

const { listTags } = vi.hoisted(() => ({ listTags: vi.fn() }));

vi.mock('@store-everything/api-client', () => ({ listTags }));

const OFFERS = [
  {
    id: 'tag-1',
    name: 'investment',
    status: 'active',
    usage: { files: 3, folders: 0 },
    parents: [],
    matched: 'investment',
    matched_alias: false,
    created_at: '2026-08-24T10:00:00Z',
  },
  {
    id: 'tag-2',
    name: 'invoice',
    status: 'active',
    usage: { files: 1, folders: 0 },
    parents: [],
    matched: 'bill',
    matched_alias: true,
    created_at: '2026-08-24T10:00:00Z',
  },
];

function picker(applied: string[] = []) {
  return mount(TagPicker, {
    props: { applied },
    global: {
      plugins: [
        [VueQueryPlugin, { queryClientConfig: { defaultOptions: { queries: { retry: false } } } }],
      ],
    },
  });
}

/** Type, let the debounce elapse, and let the request settle. */
async function type(wrapper: ReturnType<typeof picker>, value: string) {
  await wrapper.get('input[role="combobox"]').setValue(value);
  await vi.advanceTimersByTimeAsync(250);
  await flushPromises();
}

describe('TagPicker', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    listTags.mockResolvedValue({
      data: { data: OFFERS, next_cursor: null },
      error: undefined,
      response: { status: 200 },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('asks once for a word, not once per letter', { tags: ['@F-003/FR-8'] }, async () => {
    const wrapper = picker();
    const input = wrapper.get('input[role="combobox"]');

    for (const value of ['i', 'in', 'inv']) {
      await input.setValue(value);
      await vi.advanceTimersByTimeAsync(50);
    }
    expect(listTags).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(250);
    await flushPromises();

    expect(listTags).toHaveBeenCalledTimes(1);
    expect(listTags).toHaveBeenCalledWith({ query: { prefix: 'inv' } });
  });

  it('shows usage and which spelling matched', { tags: ['@F-003/FR-8'] }, async () => {
    const wrapper = picker();
    await type(wrapper, 'inv');

    const options = wrapper.findAll('[role="option"]');
    expect(options).toHaveLength(2);
    expect(options[0]?.text()).toContain('investment');
    expect(options[0]?.text()).toContain('3 used');
    // A synonym match says so, because otherwise offering `invoice` for `bill` looks like a bug.
    expect(options[1]?.text()).toContain('matched “bill”');
  });

  it('drops what the subject already carries', { tags: ['@F-003/FR-8'] }, async () => {
    const wrapper = picker(['tag-1']);
    await type(wrapper, 'inv');

    const options = wrapper.findAll('[role="option"]');
    expect(options).toHaveLength(1);
    expect(options[0]?.text()).toContain('invoice');
  });

  it('is operable from the keyboard alone', { tags: ['@F-027/FR-12'] }, async () => {
    const wrapper = picker();
    await type(wrapper, 'inv');
    const input = wrapper.get('input[role="combobox"]');

    expect(input.attributes('aria-expanded')).toBe('true');
    await input.trigger('keydown', { key: 'ArrowDown' });
    await input.trigger('keydown', { key: 'ArrowDown' });
    // The input keeps focus and points at the highlighted option, which is how a screen reader
    // follows a combobox.
    expect(input.attributes('aria-activedescendant')).toBe(
      wrapper.findAll('[role="option"]')[1]?.attributes('id'),
    );

    await input.trigger('keydown', { key: 'Enter' });
    expect(wrapper.emitted('picked')?.[0]?.[0]).toMatchObject({ id: 'tag-2' });
  });

  it('sends an unrecognised word for the server to judge', { tags: ['@F-003/FR-8'] }, async () => {
    listTags.mockResolvedValue({
      data: { data: [], next_cursor: null },
      error: undefined,
      response: { status: 200 },
    });

    const wrapper = picker();
    await type(wrapper, 'wombat');
    await wrapper.get('input[role="combobox"]').trigger('keydown', { key: 'Enter' });

    expect(wrapper.emitted('typed')?.[0]?.[0]).toBe('wombat');
    expect(wrapper.emitted('picked')).toBeUndefined();
  });

  it('dismisses on Escape', { tags: ['@F-027/FR-12'] }, async () => {
    const wrapper = picker();
    await type(wrapper, 'inv');
    expect(wrapper.findAll('[role="option"]')).toHaveLength(2);

    await wrapper.get('input[role="combobox"]').trigger('keydown', { key: 'Escape' });
    await flushPromises();

    expect(wrapper.findAll('[role="option"]')).toHaveLength(0);
  });
});
