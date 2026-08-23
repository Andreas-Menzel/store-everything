import { expect, test, type Page } from '@playwright/test';

/**
 * Tagging a file in a browser ([F-003/FR-2, FR-8](../../features/F-003-tagging.md)).
 *
 * The component tests prove the wiring; this proves the *journey* — type three letters, take the
 * completion with the keyboard, and see the word on the file — through the real router, the real
 * cache and a real keyboard. The API is stubbed at the network boundary so the test asserts our
 * behaviour rather than whatever an instance happens to hold.
 */

const IDENTITY = {
  id: '01a02900-0000-7000-8000-000000000001',
  email: 'owner@example.com',
  display_name: 'Owner',
  role: 'member',
};

const FILE = '01a02900-0000-7000-8000-000000000abc';
const TAG = '01a02900-0000-7000-8000-000000000t01';

const INVOICE = {
  id: TAG,
  name: 'invoice',
  status: 'active',
  usage: { files: 2, folders: 0 },
  parents: [],
  matched: 'invoice',
  matched_alias: false,
  created_at: '2026-08-24T10:00:00Z',
};

async function stubbed(page: Page): Promise<void> {
  const applied: unknown[] = [];

  await page.route('**/api/v1/auth/me', (route) => route.fulfill({ status: 200, json: IDENTITY }));
  await page.route(`**/api/v1/files/${FILE}`, (route) =>
    route.fulfill({
      status: 200,
      json: {
        id: FILE,
        workspace: '01a02900-0000-7000-8000-00000000000a',
        path: 'notes.txt',
        name: 'notes.txt',
        size: 12,
        content_hash: 'a'.repeat(64),
        digest_algorithm: 'sha256',
        media_type: 'text/plain',
        media_class: 'document',
        version: '01a02900-0000-7000-8000-0000000000v1',
        extraction_status: 'indexed',
        state: 'live',
        created_at: '2026-08-24T10:00:00Z',
        modified_at: null,
        trash: null,
        tags: applied,
      },
    }),
  );
  await page.route('**/api/v1/tags?**', (route) =>
    route.fulfill({ status: 200, json: { data: [INVOICE], next_cursor: null } }),
  );
  // The file's tags, and the one write that changes them: the stub keeps state so the page shows
  // what a real instance would show after the mutation refetches.
  await page.route(`**/api/v1/files/${FILE}/tags`, (route) => {
    if (route.request().method() === 'POST') {
      applied.push({
        id: TAG,
        name: 'invoice',
        status: 'active',
        provenance: 'manual',
        user: IDENTITY.id,
        source: null,
        created_at: '2026-08-24T10:00:00Z',
        updated_at: '2026-08-24T10:00:00Z',
      });
      route.fulfill({ status: 201, json: applied[0] });
      return;
    }
    route.fulfill({ status: 200, json: applied });
  });
}

test(
  'a tag is added from the keyboard and appears on the file',
  { tag: ['@F-003/FR-2', '@F-003/FR-8'] },
  async ({ page }) => {
    await stubbed(page);

    await page.goto(`/files/${FILE}`);
    await expect(page.getByRole('heading', { name: 'notes.txt' })).toBeVisible();
    await expect(page.getByText('No tags yet.')).toBeVisible();

    await page.getByLabel('Add a tag').fill('inv');
    const offered = page.getByRole('option', { name: /invoice/ });
    await expect(offered).toBeVisible();
    // How many files already carry it: the ranking signal, shown rather than implied.
    await expect(offered).toContainText('2 used');

    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');

    await expect(page.getByTestId('tag-invoice')).toContainText('invoice');
    await expect(page.getByTestId('tag-invoice')).toContainText('Added by hand');
    // The picker cleared itself, so the next word starts from empty rather than from the last.
    await expect(page.getByLabel('Add a tag')).toHaveValue('');
  },
);
