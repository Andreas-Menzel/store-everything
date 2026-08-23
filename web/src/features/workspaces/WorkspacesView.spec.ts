import { VueQueryPlugin } from '@tanstack/vue-query';
import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import WorkspacesView from './WorkspacesView.vue';

/**
 * The front door, when the server will not answer
 * ([F-027/FR-8](../../../../features/F-027-web-application-shell.md)).
 *
 * This view read only the `data` half of the generated client's answer and returned `[]` for
 * anything else, so a `500` — or a `403`, or a proxy's HTML error page — arrived as a successful
 * empty list and rendered "No workspaces yet": the app told the owner of forty workspaces that
 * they had none, in the healthy-looking empty state, with no way to tell the difference. The
 * error branch already existed in the template; nothing could ever reach it.
 */

const { createWorkspace, listWorkspaces } = vi.hoisted(() => ({
  createWorkspace: vi.fn(),
  listWorkspaces: vi.fn(),
}));

vi.mock('@store-everything/api-client', () => ({ createWorkspace, listWorkspaces }));

function view() {
  return mount(WorkspacesView, {
    global: {
      // No retries: a failing query has to settle inside the test, not three backoffs later.
      plugins: [
        [VueQueryPlugin, { queryClientConfig: { defaultOptions: { queries: { retry: false } } } }],
      ],
      // The real link needs a router; its text is what the assertions read, so keep the slot.
      stubs: { RouterLink: { template: '<a><slot /></a>' } },
    },
  });
}

describe('WorkspacesView', () => {
  beforeEach(() => {
    createWorkspace.mockReset();
    listWorkspaces.mockReset();
  });

  it(
    'says the list could not be read, rather than that it is empty',
    { tags: ['@F-027/FR-8'] },
    async () => {
      listWorkspaces.mockResolvedValue({
        data: undefined,
        error: { title: 'The database is unavailable', detail: 'Try again shortly.' },
        response: { status: 503 },
      });

      const wrapper = view();
      await flushPromises();

      expect(wrapper.text()).toContain('Could not load your workspaces');
      expect(wrapper.text()).toContain('Try again shortly.');
      expect(wrapper.text()).not.toContain('No workspaces yet');
    },
  );

  it('still says nothing is here when nothing is here', async () => {
    listWorkspaces.mockResolvedValue({ data: { data: [] }, error: undefined, response: {} });

    const wrapper = view();
    await flushPromises();

    expect(wrapper.text()).toContain('No workspaces yet');
    expect(wrapper.text()).not.toContain('Could not load your workspaces');
  });

  it('lists what the server returned', async () => {
    listWorkspaces.mockResolvedValue({
      data: {
        data: [
          {
            id: 'workspace-1',
            name: 'Photos',
            root_path: '/srv/data/photos',
            placement: 'managed',
            state: 'ready',
          },
        ],
      },
      error: undefined,
      response: {},
    });

    const wrapper = view();
    await flushPromises();

    expect(wrapper.text()).toContain('Photos');
    expect(wrapper.text()).toContain('/srv/data/photos');
    expect(wrapper.text()).not.toContain('No workspaces yet');
  });
});
