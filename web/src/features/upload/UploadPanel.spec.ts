import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import UploadPanel from './UploadPanel.vue';

/**
 * The upload panel's progress display.
 *
 * The number on screen is the *server's* committed offset, reported through `onProgress` while
 * the upload is still running. That made the regression this file exists for a quiet one: the
 * panel kept its list item as a plain object and mutated it after handing it to the reactive
 * array, so nothing re-rendered — a multi-GB upload sat at `0%` for its whole life and reached
 * `100%` only because the surrounding `isSending` flag happened to flip at the end. The first
 * test is the one that discriminates: it asserts *mid-upload*, while the promise the panel is
 * awaiting is still pending, which is the only window in which a repaint has to have been
 * triggered by the item itself. The other two hold the surrounding behaviour still.
 */

const { upload, UploadFailed } = vi.hoisted(() => ({
  upload: vi.fn(),
  UploadFailed: class UploadFailed extends Error {
    constructor(readonly failure: unknown) {
      super('stub');
    }
  },
}));

vi.mock('./resumable', () => ({ upload, UploadFailed }));

type ProgressReporter = (progress: { sent: number; total: number }) => void;
type UploadOptions = { onProgress?: ProgressReporter };

/** A file the panel can queue; its bytes are never read, only its name and size. */
function fileOf(name: string, size: number): File {
  return { name, size } as File;
}

/** Pick files, the way a person does — through the panel's own file input. */
async function pick(wrapper: ReturnType<typeof panel>, ...files: File[]): Promise<void> {
  const list = Object.assign([...files], { item: (i: number) => files[i] ?? null });
  const input = wrapper.get('input[type="file"]');
  Object.defineProperty(input.element, 'files', { value: list, configurable: true });
  await input.trigger('change');
  await flushPromises();
}

function panel() {
  return mount(UploadPanel, { props: { workspaceId: 'workspace-1', folderPath: 'photos' } });
}

describe('UploadPanel', () => {
  beforeEach(() => {
    upload.mockReset();
  });

  it('renders progress while the upload is still running', async () => {
    // Hold the upload open so the assertions land mid-flight, and keep the reporter to drive it.
    let report: ProgressReporter = () => {};
    let finish: () => void = () => {};
    upload.mockImplementation(
      (_workspace: string, _path: string, _file: File, options: UploadOptions) =>
        new Promise<void>((resolve) => {
          report = options.onProgress ?? report;
          finish = resolve;
        }),
    );

    const wrapper = panel();
    await pick(wrapper, fileOf('holiday.mov', 1000));
    expect(wrapper.text()).toContain('0%');

    report({ sent: 400, total: 1000 });
    await flushPromises();

    expect(wrapper.text()).toContain('40%');

    finish();
    await flushPromises();
  });

  it('shows a file as done as soon as its own upload resolves', async () => {
    // Two files, the second still pending: the first must read `100%` before the batch ends.
    let finishSecond: () => void = () => {};
    upload
      .mockImplementationOnce(
        (_workspace: string, _path: string, file: File, options: UploadOptions) => {
          options.onProgress?.({ sent: file.size, total: file.size });
          return Promise.resolve();
        },
      )
      .mockImplementationOnce(
        () =>
          new Promise<void>((resolve) => {
            finishSecond = resolve;
          }),
      );

    const wrapper = panel();
    await pick(wrapper, fileOf('one.txt', 10), fileOf('two.txt', 10));

    expect(wrapper.text()).toContain('Uploading…');
    expect(wrapper.findAll('li')[0]?.text()).toContain('100%');

    finishSecond();
    await flushPromises();
  });

  it('attaches the server’s reason to the file that failed', async () => {
    upload.mockRejectedValue(new UploadFailed({ title: 'The storage refused it', fields: [] }));

    const wrapper = panel();
    await pick(wrapper, fileOf('rejected.bin', 10));

    expect(wrapper.text()).toContain('failed');
    expect(wrapper.text()).toContain('The storage refused it');
  });
});
