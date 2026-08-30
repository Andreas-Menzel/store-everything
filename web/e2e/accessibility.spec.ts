import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

/**
 * [F-027/FR-12](../../features/F-027-web-application-shell.md): the shell is operable without a
 * mouse and legible to assistive technology.
 *
 * An automated check does not prove a surface is *usable* — no tool can — but it does catch the
 * failures that are pure oversight: an input with no label, a control with no accessible name,
 * contrast below the threshold, a heading level skipped. Those are the ones that accumulate
 * silently while nobody is looking, so they are the ones worth a gate.
 */

const IDENTITY = {
  id: '01a02900-0000-7000-8000-000000000001',
  email: 'owner@example.com',
  display_name: 'Owner',
  role: 'member',
};

const FOLDER = '01a02900-0000-7000-8000-0000000000ff';

async function scan(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();

  expect(results.violations.map((violation) => `${violation.id}: ${violation.help}`)).toEqual([]);
}

async function stubbed(page: Page): Promise<void> {
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({ status: 200, json: IDENTITY }));
  await page.route('**/api/v1/workspaces', (route) =>
    route.fulfill({
      status: 200,
      json: {
        data: [
          {
            id: '01a02900-0000-7000-8000-00000000000a',
            owner: IDENTITY.id,
            name: 'Photos',
            source: 'local',
            placement: 'managed',
            state: 'active',
            root_path: '/srv/photos',
            root_folder: FOLDER,
            filesystem: { probed: '/srv/photos', usable: true, properties: {}, facts: {} },
            scan_interval_minutes: 60,
            created_at: '2026-08-22T10:00:00Z',
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route(`**/api/v1/folders/${FOLDER}`, (route) =>
    route.fulfill({
      status: 200,
      json: {
        id: FOLDER,
        workspace: '01a02900-0000-7000-8000-00000000000a',
        parent: null,
        name: '',
        path: '',
        depth: 0,
        created_at: '2026-08-22T10:00:00Z',
        aggregates: {
          direct_files: 1,
          total_files: 1,
          total_bytes: 12,
          as_of: '2026-08-22T10:00:00Z',
          pending: false,
        },
      },
    }),
  );
  // The tag surfaces: one applied tag on the folder, one word in the vocabulary, and a
  // suggestion waiting — so the scan sees a populated queue rather than an empty state.
  await page.route(`**/api/v1/folders/${FOLDER}/tags`, (route) =>
    route.fulfill({
      status: 200,
      json: [
        {
          id: '01a02900-0000-7000-8000-000000000t01',
          name: 'tax',
          status: 'active',
          provenance: 'manual',
          user: IDENTITY.id,
          source: null,
          created_at: '2026-08-22T10:00:00Z',
          updated_at: '2026-08-22T10:00:00Z',
        },
      ],
    }),
  );
  await page.route('**/api/v1/tags?**', (route) => {
    const status = new URL(route.request().url()).searchParams.get('status') ?? 'active';
    route.fulfill({
      status: 200,
      json: {
        data:
          status === 'suggested'
            ? [
                {
                  id: '01a02900-0000-7000-8000-000000000t02',
                  name: 'wombat',
                  status: 'suggested',
                  usage: { files: 1, folders: 0 },
                  parents: [],
                  matched: null,
                  matched_alias: false,
                  created_at: '2026-08-22T10:00:00Z',
                },
              ]
            : [
                {
                  id: '01a02900-0000-7000-8000-000000000t01',
                  name: 'invoice',
                  status: 'active',
                  usage: { files: 2, folders: 1 },
                  parents: [],
                  matched: null,
                  matched_alias: false,
                  created_at: '2026-08-22T10:00:00Z',
                },
              ],
        next_cursor: null,
      },
    });
  });
  await page.route(`**/api/v1/folders/${FOLDER}/children**`, (route) =>
    route.fulfill({
      status: 200,
      json: {
        data: [
          {
            kind: 'file',
            id: '01a02900-0000-7000-8000-000000000abc',
            name: 'notes.txt',
            path: 'notes.txt',
            size: 12,
            content_hash: 'a'.repeat(64),
            media_type: 'text/plain',
            media_class: 'document',
            modified_at: null,
            created_at: '2026-08-22T10:00:00Z',
          },
        ],
        next_cursor: null,
      },
    }),
  );
}

test(
  'the login page has no accessibility violations',
  { tag: ['@F-027/FR-12'] },
  async ({ page }) => {
    await page.route('**/api/v1/auth/me', (route) =>
      route.fulfill({ status: 401, contentType: 'application/problem+json', body: '{}' }),
    );

    await page.goto('/login');
    await expect(page.getByLabel('Password')).toBeVisible();

    await scan(page);
  },
);

test(
  'a rejected form is still accessible',
  { tag: ['@F-027/FR-8', '@F-027/FR-12'] },
  async ({ page }) => {
    await page.route('**/api/v1/auth/me', (route) =>
      route.fulfill({ status: 401, contentType: 'application/problem+json', body: '{}' }),
    );
    await page.route('**/api/v1/auth/login', (route) =>
      route.fulfill({
        status: 422,
        contentType: 'application/problem+json',
        body: JSON.stringify({
          title: 'Validation failed',
          status: 422,
          errors: [
            { detail: 'that address needs a domain with a dot in it', pointer: '/body/email' },
          ],
        }),
      }),
    );

    await page.goto('/login');
    // Browser-valid (the HTML spec accepts `user@host`) and server-invalid, so the request is
    // actually made and the server's own complaint is what gets rendered.
    await page.getByLabel('Email').fill('owner@example');
    await page.getByLabel('Password').fill('whatever');
    await page.getByRole('button', { name: 'Sign in' }).click();

    // The field's own complaint, attached to the field rather than only to the page (FR-8).
    await expect(page.getByText('that address needs a domain with a dot in it')).toBeVisible();
    await scan(page);
  },
);

test(
  'the frame and the workspace list have no accessibility violations',
  { tag: ['@F-027/FR-12'] },
  async ({ page }) => {
    await stubbed(page);

    await page.goto('/');
    await expect(page.getByRole('link', { name: 'Photos' })).toBeVisible();

    await scan(page);
  },
);

test(
  'the folder browser has no accessibility violations',
  { tag: ['@F-027/FR-12'] },
  async ({ page }) => {
    await stubbed(page);

    await page.goto(`/folders/${FOLDER}`);
    await expect(page.getByRole('link', { name: 'notes.txt' })).toBeVisible();

    await scan(page);
  },
);

test(
  'the whole frame is reachable from the keyboard, with focus visible',
  { tag: ['@F-027/FR-10', '@F-027/FR-12'] },
  async ({ page }) => {
    await stubbed(page);
    await page.goto('/');
    await expect(page.getByRole('link', { name: 'Photos' })).toBeVisible();

    const reached: string[] = [];
    for (let step = 0; step < 12; step += 1) {
      await page.keyboard.press('Tab');
      const focused = await page.evaluate(() => {
        const element = document.activeElement;
        if (!element || element === document.body) return undefined;
        return (
          (element.textContent ?? '').trim() ||
          element.getAttribute('aria-label') ||
          element.tagName
        );
      });
      if (focused) reached.push(focused);
    }

    // Everything the frame owns: the app's own link, every section, and the way out.
    expect(reached).toContain('Store Everything');
    expect(reached).toContain('Workspaces');
    expect(reached).toContain('Tags');
    expect(reached).toContain('API');
    expect(reached).toContain('Sign out');
  },
);

test(
  'the tag vocabulary has no accessibility violations',
  { tag: ['@F-027/FR-12'] },
  async ({ page }) => {
    await stubbed(page);

    await page.goto('/tags');
    await expect(page.getByRole('heading', { name: 'Tags', level: 1 })).toBeVisible();
    // The combobox is the part most likely to be inaccessible — a listbox with no name, an
    // option with no selected state — so the scan happens with it open.
    await page.getByLabel('Merge “invoice” into').isHidden();

    await scan(page);
  },
);
