import { VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import TagList from './TagList.vue';

/**
 * What the tag chips say, and which answers they offer
 * ([F-003](../../../../features/F-003-tagging.md) FR-2, FR-3, FR-4, FR-5, FR-12).
 *
 * The interesting assertions are about *provenance*, because that is the difference the whole
 * feature turns on: a word somebody typed and a guess a model made must not look alike, a
 * detected tag has to offer both of the answers ADR-0004 gives a person, and a tag still in
 * review has to say so rather than quietly failing to turn up in search.
 */

const { readFileTags, readFolderTags, tagFile, untagFile, confirmFileTag, listTags } = vi.hoisted(
  () => ({
    readFileTags: vi.fn(),
    readFolderTags: vi.fn(),
    tagFile: vi.fn(),
    untagFile: vi.fn(),
    confirmFileTag: vi.fn(),
    listTags: vi.fn(),
  }),
);

vi.mock('@store-everything/api-client', () => ({
  readFileTags,
  readFolderTags,
  tagFile,
  tagFolder: vi.fn(),
  untagFile,
  untagFolder: vi.fn(),
  confirmFileTag,
  listTags,
}));

const MANUAL = {
  id: 'tag-manual',
  name: 'invoice',
  status: 'active',
  provenance: 'manual',
  user: 'user-1',
  source: null,
  created_at: '2026-08-24T10:00:00Z',
  updated_at: '2026-08-24T10:00:00Z',
};

const DETECTED = {
  id: 'tag-auto',
  name: 'receipt',
  status: 'active',
  provenance: 'auto',
  user: null,
  source: {
    extractor: 'image-vision',
    extractor_version: '2.0.0',
    model_version: '1.4',
    generation: 1,
    confidence: 0.87,
  },
  created_at: '2026-08-24T10:00:00Z',
  updated_at: '2026-08-24T10:00:00Z',
};

const SUGGESTED = {
  ...DETECTED,
  id: 'tag-suggested',
  name: 'wombat',
  status: 'suggested',
  source: { ...DETECTED.source, confidence: null },
};

function answered(tags: unknown[]) {
  readFileTags.mockResolvedValue({ data: tags, error: undefined, response: { status: 200 } });
  readFolderTags.mockResolvedValue({ data: tags, error: undefined, response: { status: 200 } });
}

async function list(kind: 'file' | 'folder' = 'file') {
  const wrapper = mount(TagList, {
    props: { subject: { kind, id: 'subject-1' } },
    global: {
      plugins: [
        [VueQueryPlugin, { queryClientConfig: { defaultOptions: { queries: { retry: false } } } }],
      ],
    },
  });
  await flushPromises();
  return wrapper;
}

describe('TagList', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listTags.mockResolvedValue({
      data: { data: [], next_cursor: null },
      error: undefined,
      response: { status: 200 },
    });
    tagFile.mockResolvedValue({ data: MANUAL, error: undefined, response: { status: 201 } });
    untagFile.mockResolvedValue({ data: undefined, error: undefined, response: { status: 204 } });
    confirmFileTag.mockResolvedValue({
      data: { ...DETECTED, provenance: 'confirmed' },
      error: undefined,
      response: { status: 200 },
    });
  });

  it('tells a machine’s guess apart from a person’s word', { tags: ['@F-003/FR-3'] }, async () => {
    answered([MANUAL, DETECTED]);

    const wrapper = await list();

    expect(wrapper.get('[data-testid="tag-invoice"]').text()).toContain('Added by hand');
    const detected = wrapper.get('[data-testid="tag-receipt"]').text();
    expect(detected).toContain('Detected');
    // The confidence and the model that produced it, which is what makes an auto tag
    // debuggable rather than mysterious.
    expect(detected).toContain('87%');
    expect(detected).toContain('image-vision');
  });

  it('says when a tag is still in review', { tags: ['@F-003/FR-12'] }, async () => {
    answered([SUGGESTED]);

    const wrapper = await list();

    expect(wrapper.get('[data-testid="suggested-badge"]').text()).toBe('Awaiting review');
    // No confidence was reported, so none is shown rather than an invented number.
    expect(wrapper.get('[data-testid="tag-wombat"]').text()).not.toContain('%');
  });

  it(
    'offers both answers to a detected tag',
    { tags: ['@F-003/FR-4', '@F-003/FR-5'] },
    async () => {
      answered([MANUAL, DETECTED]);

      const wrapper = await list();

      // A person's own tag has nothing to confirm — there is no machine to agree with.
      expect(wrapper.find('[aria-label="Confirm invoice"]').exists()).toBe(false);

      await wrapper.get('[aria-label="Confirm receipt"]').trigger('click');
      await flushPromises();
      expect(confirmFileTag).toHaveBeenCalledWith({
        path: { file_id: 'subject-1', tag_id: 'tag-auto' },
      });

      await wrapper.get('[aria-label="Remove receipt"]').trigger('click');
      await flushPromises();
      expect(untagFile).toHaveBeenCalledWith({
        path: { file_id: 'subject-1', tag_id: 'tag-auto' },
      });
    },
  );

  it('has nothing to confirm on a folder', { tags: ['@F-015/FR-9'] }, async () => {
    // Extractors never run on folders, so a folder tag is always a person's word — there is no
    // claim to agree with, and offering the control would be a lie about the model.
    answered([MANUAL]);

    const wrapper = await list('folder');

    expect(wrapper.find('[aria-label="Confirm invoice"]').exists()).toBe(false);
    expect(wrapper.find('[aria-label="Remove invoice"]').exists()).toBe(true);
  });

  it('shows what the server refused', { tags: ['@F-003/FR-2'] }, async () => {
    answered([]);
    tagFile.mockResolvedValue({
      data: undefined,
      error: {
        title: 'Validation failed',
        status: 422,
        errors: [{ detail: 'no tag goes by that name', pointer: '/name' }],
      },
      response: { status: 422 },
    });

    const wrapper = await list();
    await wrapper.get('input[role="combobox"]').setValue('whatever');
    await wrapper.get('input[role="combobox"]').trigger('keydown', { key: 'Enter' });
    await flushPromises();

    expect(tagFile).toHaveBeenCalledWith({
      path: { file_id: 'subject-1' },
      body: { name: 'whatever' },
    });
    expect(wrapper.text()).toContain('no tag goes by that name');
  });
});
