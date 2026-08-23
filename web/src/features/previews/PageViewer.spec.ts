import { VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import PageViewer from './PageViewer.vue';

/**
 * Reading a document page by page
 * ([F-028/FR-7](../../../../features/F-028-thumbnails-and-previews.md)).
 *
 * The case worth a test is the one a naive viewer gets wrong: only page one exists in advance, so
 * asking for page two answers `202` while a container renders it. A viewer that treated that as a
 * failure would show a broken image for the half-second it takes; this one says what is happening
 * and comes back for the bytes.
 */

const { readFilePage } = vi.hoisted(() => ({ readFilePage: vi.fn() }));

vi.mock('@store-everything/api-client', () => ({ readFilePage }));

function served(bytes: string) {
  return {
    data: new Blob([bytes], { type: 'image/webp' }),
    error: undefined,
    response: { status: 200 },
  };
}

const RENDERING = { data: undefined, error: undefined, response: { status: 202 } };

function viewer(pages = 3) {
  return mount(PageViewer, {
    props: { fileId: 'file-1', version: 'version-1', pages },
    global: {
      plugins: [
        [VueQueryPlugin, { queryClientConfig: { defaultOptions: { queries: { retry: false } } } }],
      ],
    },
  });
}

describe('PageViewer', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // jsdom has no object URLs.
    globalThis.URL.createObjectURL = vi.fn(() => 'blob:page');
    globalThis.URL.revokeObjectURL = vi.fn();
  });

  it(
    'asks for the page it is showing, pinned to the version',
    { tags: ['@F-028/FR-7'] },
    async () => {
      readFilePage.mockResolvedValue(served('page-one'));

      const wrapper = viewer();
      await flushPromises();

      expect(readFilePage).toHaveBeenCalledWith({
        path: { file_id: 'file-1', page: 1 },
        query: { v: 'version-1' },
        parseAs: 'blob',
      });
      expect(wrapper.get('img').attributes('alt')).toBe('Page 1 of 3');
      expect(wrapper.text()).toContain('Page 1 of 3');
    },
  );

  it('moves through the document within its bounds', { tags: ['@F-028/FR-7'] }, async () => {
    readFilePage.mockResolvedValue(served('a page'));

    const wrapper = viewer(2);
    await flushPromises();
    expect(wrapper.get('[aria-label="Previous page"]').attributes('disabled')).toBeDefined();

    await wrapper.get('[aria-label="Next page"]').trigger('click');
    await flushPromises();

    expect(readFilePage).toHaveBeenLastCalledWith(
      expect.objectContaining({ path: { file_id: 'file-1', page: 2 } }),
    );
    // Two pages, so there is nowhere further to go.
    expect(wrapper.get('[aria-label="Next page"]').attributes('disabled')).toBeDefined();
  });

  it('says a page is being rendered rather than failing', { tags: ['@F-028/FR-7'] }, async () => {
    readFilePage.mockResolvedValueOnce(served('page-one')).mockResolvedValueOnce(RENDERING);

    const wrapper = viewer();
    await flushPromises();
    await wrapper.get('[aria-label="Next page"]').trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain('Rendering page 2');
    // Not an error state: nothing has gone wrong, the work is simply not done yet.
    expect(wrapper.text()).not.toContain('Could not show this page');
  });

  it('shows a real refusal as one', { tags: ['@F-028/FR-7'] }, async () => {
    readFilePage.mockResolvedValue({
      data: undefined,
      error: { title: 'Not found', status: 404, detail: 'no such page' },
      response: { status: 404 },
    });

    const wrapper = viewer();
    await flushPromises();

    expect(wrapper.text()).toContain('Could not show this page');
    expect(wrapper.text()).toContain('no such page');
  });
});
