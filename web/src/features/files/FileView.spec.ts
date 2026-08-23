import { VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import FileView from './FileView.vue';

/**
 * What the file page says about content analysis.
 *
 * The API answers in the vocabulary of jobs — `pending`, `indexed`, `none` — and the person
 * looking at their own file is asking something else: can I find this by its content yet
 * ([F-001/FR-8](../../../../features/F-001-upload-and-import.md))? So the view translates, and
 * `none` in particular must not read as a failure: nothing installed analyses that kind of file.
 */

const { readFile } = vi.hoisted(() => ({ readFile: vi.fn() }));

vi.mock('@store-everything/api-client', () => ({ readFile }));
vi.mock('vue-router', () => ({
  RouterLink: { template: '<a><slot /></a>' },
  useRoute: () => ({ params: { id: 'file-1' } }),
}));

function answered(extraction_status: string) {
  readFile.mockResolvedValue({
    data: {
      id: 'file-1',
      name: 'report.txt',
      path: 'report.txt',
      size: 12,
      media_type: 'text/plain',
      media_class: 'document',
      state: 'live',
      content_hash: 'a'.repeat(64),
      version: 'version-1',
      extraction_status,
      created_at: '2026-08-23T10:00:00Z',
      modified_at: null,
      trash: null,
    },
    error: undefined,
    response: { status: 200 },
  });
}

async function view() {
  const wrapper = mount(FileView, {
    global: {
      plugins: [
        [VueQueryPlugin, { queryClientConfig: { defaultOptions: { queries: { retry: false } } } }],
      ],
      stubs: { RouterLink: { template: '<a><slot /></a>' } },
    },
  });
  await flushPromises();
  return wrapper;
}

describe('FileView', () => {
  beforeEach(() => {
    readFile.mockReset();
  });

  it(
    'says a freshly uploaded file is still being analysed',
    { tags: ['@F-001/FR-8'] },
    async () => {
      answered('pending');

      const wrapper = await view();

      expect(wrapper.get('[data-testid="extraction-status"]').text()).toBe('Analysing…');
    },
  );

  it('says so once the analysis is done', { tags: ['@F-001/FR-8'] }, async () => {
    answered('indexed');

    const wrapper = await view();

    expect(wrapper.get('[data-testid="extraction-status"]').text()).toBe('Analysed');
  });

  it('does not dress "nothing analyses this" up as a failure', async () => {
    answered('none');

    const wrapper = await view();

    const shown = wrapper.get('[data-testid="extraction-status"]').text();
    expect(shown).toBe('Not analysed');
    expect(shown).not.toContain('failed');
  });

  it('shows a status it has no word for rather than nothing at all', async () => {
    // A core that grew a sixth answer must not leave the field blank in an older client.
    answered('reprocessing');

    const wrapper = await view();

    expect(wrapper.get('[data-testid="extraction-status"]').text()).toBe('reprocessing');
  });
});
