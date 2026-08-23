import { VueQueryPlugin } from '@tanstack/vue-query';
import { createPinia, setActivePinia } from 'pinia';
import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import TaxonomyView from './TaxonomyView.vue';

/**
 * The vocabulary page ([F-003/FR-10, FR-12](../../../../features/F-003-tagging.md)).
 *
 * Two things it must get right. **Who may shape the vocabulary**: a member browses it and sees no
 * controls, because a page full of buttons that all answer `403` is worse than a page without
 * them — and the server refuses regardless, so this is courtesy rather than security. And **the
 * review queue**, which is the only part of the page with a deadline: every pending word is a
 * machine's claim sitting on somebody's file that search cannot reach yet.
 */

const { listTags, approveTag, rejectTag, createTag } = vi.hoisted(() => ({
  listTags: vi.fn(),
  approveTag: vi.fn(),
  rejectTag: vi.fn(),
  createTag: vi.fn(),
}));

vi.mock('@store-everything/api-client', () => ({
  listTags,
  approveTag,
  rejectTag,
  createTag,
  deleteTag: vi.fn(),
  mergeTag: vi.fn(),
  updateTag: vi.fn(),
}));

const identity = {
  value: { id: 'user-1', email: 'a@example.com', display_name: 'A', role: 'admin' },
};
vi.mock('@/features/auth/session', () => ({
  useSession: () => ({ identity }),
}));

function tag(name: string, status: 'active' | 'suggested', id = name) {
  return {
    id,
    name,
    status,
    usage: { files: status === 'suggested' ? 2 : 5, folders: 0 },
    parents: [],
    matched: null,
    matched_alias: false,
    created_at: '2026-08-24T10:00:00Z',
  };
}

function answered(active: unknown[], suggested: unknown[]) {
  listTags.mockImplementation(async ({ query }: { query: { status: string } }) => ({
    data: { data: query.status === 'suggested' ? suggested : active, next_cursor: null },
    error: undefined,
    response: { status: 200 },
  }));
}

async function page() {
  const wrapper = mount(TaxonomyView, {
    global: {
      plugins: [
        [VueQueryPlugin, { queryClientConfig: { defaultOptions: { queries: { retry: false } } } }],
      ],
    },
  });
  await flushPromises();
  return wrapper;
}

describe('TaxonomyView', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    identity.value = { id: 'user-1', email: 'a@example.com', display_name: 'A', role: 'admin' };
    approveTag.mockResolvedValue({
      data: tag('wombat', 'active'),
      error: undefined,
      response: { status: 200 },
    });
    rejectTag.mockResolvedValue({
      data: tag('wombat', 'suggested'),
      error: undefined,
      response: { status: 200 },
    });
    createTag.mockResolvedValue({
      data: tag('invoice', 'active'),
      error: undefined,
      response: { status: 201 },
    });
  });

  it('reviews what a machine proposed', { tags: ['@F-003/FR-12'] }, async () => {
    answered([tag('invoice', 'active')], [tag('wombat', 'suggested')]);

    const wrapper = await page();

    const queue = wrapper.get('[data-testid="suggestions"]');
    expect(queue.text()).toContain('wombat');
    // How much is riding on the decision: the word is already on files.
    expect(queue.text()).toContain('2 of your file(s)');

    await wrapper.get('[aria-label="Approve wombat"]').trigger('click');
    await flushPromises();
    expect(approveTag).toHaveBeenCalledWith({ path: { tag_id: 'wombat' } });

    await wrapper.get('[aria-label="Reject wombat"]').trigger('click');
    await flushPromises();
    expect(rejectTag).toHaveBeenCalledWith({ path: { tag_id: 'wombat' } });
  });

  it('adds a word to the vocabulary', { tags: ['@F-003/FR-10'] }, async () => {
    answered([], []);

    const wrapper = await page();
    await wrapper.get('input').setValue('invoice');
    await wrapper.get('form').trigger('submit');
    await flushPromises();

    expect(createTag).toHaveBeenCalledWith({ body: { name: 'invoice' } });
  });

  it('offers a member nothing to press', { tags: ['@F-003/FR-10'] }, async () => {
    identity.value = { ...identity.value, role: 'member' };
    answered([tag('invoice', 'active')], []);

    const wrapper = await page();

    // The vocabulary is readable by anyone: a shared list nobody can browse is not usable.
    expect(wrapper.get('[data-testid="vocabulary-invoice"]').text()).toBe('invoice');
    expect(wrapper.find('[aria-label="Rename invoice"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="suggestions"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain('Add a word');
    // And the queue was never even asked for.
    expect(listTags).toHaveBeenCalledTimes(1);
    expect(listTags).toHaveBeenCalledWith({ query: { status: 'active', limit: 200 } });
  });
});
