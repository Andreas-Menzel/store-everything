import { expect, test, type Page } from '@playwright/test';

/**
 * The shell's own behaviour ([F-027](../../features/F-027-web-application-shell.md)): who gets in,
 * what happens when a session ends, and what a failure looks like.
 *
 * The API is stubbed at the network boundary, so these assert *our* wiring rather than the state
 * of whatever instance happens to be running — and they can put the server into states that are
 * hard to arrange for real, like a `401` arriving halfway through a session.
 */

const IDENTITY = {
  id: '01a02900-0000-7000-8000-000000000001',
  email: 'owner@example.com',
  display_name: 'Owner',
  role: 'member',
};

/** Signed out: `/auth/me` says nobody is here. */
async function signedOut(page: Page): Promise<void> {
  await page.route('**/api/v1/auth/me', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/problem+json',
      body: JSON.stringify({ title: 'Not authenticated', status: 401 }),
    }),
  );
}

async function signedIn(page: Page): Promise<void> {
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({ status: 200, json: IDENTITY }));
  await page.route('**/api/v1/workspaces**', (route) =>
    route.fulfill({ status: 200, json: { data: [], next_cursor: null } }),
  );
}

test('an unauthenticated visit lands on login and comes back afterwards', async ({ page }) => {
  await signedOut(page);

  await page.goto('/folders/01a02900-0000-7000-8000-0000000000ff');

  await expect(page.getByLabel('Email')).toBeVisible();
  expect(new URL(page.url()).pathname).toBe('/login');

  // Signing in returns to where the person was going, not to the front door (F-027/FR-5).
  await page.route('**/api/v1/auth/login', (route) => route.fulfill({ status: 204, body: '' }));
  await page.unroute('**/api/v1/auth/me');
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({ status: 200, json: IDENTITY }));
  await page.route('**/api/v1/folders/*', (route) =>
    route.fulfill({
      status: 200,
      json: {
        id: '01a02900-0000-7000-8000-0000000000ff',
        workspace: '01a02900-0000-7000-8000-00000000000a',
        parent: null,
        name: '',
        path: '',
        depth: 0,
        created_at: '2026-08-22T10:00:00Z',
        aggregates: {
          direct_files: 0,
          total_files: 0,
          total_bytes: 0,
          as_of: '2026-08-22T10:00:00Z',
          pending: false,
        },
      },
    }),
  );
  await page.route('**/api/v1/folders/*/children**', (route) =>
    route.fulfill({ status: 200, json: { data: [], next_cursor: null } }),
  );

  await page.getByLabel('Email').fill('owner@example.com');
  await page.getByLabel('Password').fill('correct-horse');
  await page.getByRole('button', { name: 'Sign in' }).click();

  await expect(page.getByRole('heading', { name: 'All files' })).toBeVisible();
  expect(new URL(page.url()).pathname).toBe('/folders/01a02900-0000-7000-8000-0000000000ff');
});

test('a rejected credential shows the server’s own message', async ({ page }) => {
  await signedOut(page);
  // Deliberately undifferentiated: "no such account" and "wrong password" must look identical
  // (07 § abuse protection).
  await page.route('**/api/v1/auth/login', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/problem+json',
      body: JSON.stringify({
        title: 'Invalid credentials',
        detail: 'That email and password do not match an account.',
        status: 401,
        instance: 'req_deadbeef',
      }),
    }),
  );

  await page.goto('/login');
  await page.getByLabel('Email').fill('owner@example.com');
  await page.getByLabel('Password').fill('wrong');
  await page.getByRole('button', { name: 'Sign in' }).click();

  await expect(page.getByRole('alert')).toContainText('do not match an account');
  await expect(page.getByRole('alert')).toContainText('req_deadbeef');
  await expect(page.getByLabel('Email')).toBeVisible();
});

test('a 401 mid-session returns to login with no wall of errors', async ({ page }) => {
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({ status: 200, json: IDENTITY }));
  await page.route('**/api/v1/workspaces**', (route) =>
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
            root_folder: '01a02900-0000-7000-8000-0000000000ff',
            filesystem: { probed: '/srv/photos', usable: true, properties: {}, facts: {} },
            scan_interval_minutes: 60,
            created_at: '2026-08-22T10:00:00Z',
          },
        ],
        next_cursor: null,
      },
    }),
  );

  await page.goto('/');
  await expect(page.getByRole('link', { name: 'Photos' })).toBeVisible();

  // The session ages out. The guard already let this navigation through on the cached identity,
  // so the `401` arrives from the surface's own request — which is the case FR-6 is about.
  await page.unroute('**/api/v1/auth/me');
  await page.unroute('**/api/v1/workspaces**');
  await signedOut(page);
  await page.route('**/api/v1/workspaces**', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/problem+json',
      body: JSON.stringify({ title: 'Not authenticated', status: 401 }),
    }),
  );

  await page.getByRole('link', { name: 'Photos' }).click();

  await expect(page.getByLabel('Password')).toBeVisible();
  expect(new URL(page.url()).pathname).toBe('/login');
  await expect(page.getByRole('alert')).toHaveCount(0);
});

test('the documentation route renders this instance’s own schema', async ({ page }) => {
  await signedIn(page);
  // Bundled with the app and never fetched from a CDN: the schema is the only thing that comes
  // over the network here (F-027/FR-9).
  await page.route('**/api/v1/openapi.json', (route) =>
    route.fulfill({
      status: 200,
      json: {
        openapi: '3.1.0',
        info: { title: 'Store Everything', version: '0.1.0' },
        paths: {
          '/api/v1/workspaces': {
            get: { operationId: 'listWorkspaces', summary: 'List your workspaces', responses: {} },
          },
        },
      },
    }),
  );

  await page.goto('/docs');

  await expect(page.getByRole('heading', { name: 'API', exact: true })).toBeVisible();
  await expect(page.getByText('List your workspaces')).toBeVisible();
});

test('the documentation route says so when the instance has it switched off', async ({ page }) => {
  await signedIn(page);
  // `SE_API_DOCS_ENABLED=false` removes the schema route entirely.
  await page.route('**/api/v1/openapi.json', (route) =>
    route.fulfill({
      status: 404,
      contentType: 'application/problem+json',
      body: JSON.stringify({ title: 'Not found', status: 404 }),
    }),
  );

  await page.goto('/docs');

  await expect(page.getByRole('alert')).toContainText('not available');
  await expect(page.getByText('SE_API_DOCS_ENABLED=false')).toBeVisible();
});

test('the frame names the signed-in user and signs them out', async ({ page }) => {
  await signedIn(page);
  await page.route('**/api/v1/auth/logout', (route) => route.fulfill({ status: 204, body: '' }));

  await page.goto('/');
  await expect(page.getByText('owner@example.com')).toBeVisible();

  await page.unroute('**/api/v1/auth/me');
  await signedOut(page);
  await page.getByRole('button', { name: 'Sign out' }).click();

  await expect(page.getByLabel('Password')).toBeVisible();
});

test('login is reachable without a session, and says whether the instance is up', async ({
  page,
}) => {
  await signedOut(page);
  await page.route('**/readyz', (route) =>
    route.fulfill({ status: 503, contentType: 'application/problem+json', body: '{}' }),
  );

  await page.goto('/login');

  // A sign-in failing because the database is unreachable is not a wrong password.
  await expect(page.getByText('Instance unavailable')).toBeVisible();
});

test('a client route that does not exist says so', async ({ page }) => {
  await signedIn(page);

  await page.goto('/not-a-real-route');

  await expect(page.getByRole('heading', { name: 'Not found' })).toBeVisible();
});
