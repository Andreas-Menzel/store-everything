import { expect, test, type Page } from '@playwright/test';

/**
 * [F-027/FR-3](../../features/F-027-web-application-shell.md) and
 * [FR-7](../../features/F-027-web-application-shell.md), both negative: what the app must *not*
 * be found holding afterwards.
 *
 * A credential the client never keeps cannot be read out of a shared browser, exfiltrated by a
 * script that gets in, or handed to a third party by a URL — which is why the session is an
 * `HttpOnly` cookie this code cannot see even to check
 * ([07 § tokens & credentials](../../specs/07-identity-permissions-sharing.md#tokens--credentials)).
 * So the assertion is made from outside: sign in for real, then look everywhere a value could
 * have been left.
 */

const PASSWORD = 'correct-horse-battery-staple';
/** The value the server puts in the cookie. Nothing but the cookie jar may contain it. */
const SESSION = 'sesess_9f3c1d7a0b';

const IDENTITY = {
  id: '01a02900-0000-7000-8000-000000000001',
  email: 'owner@example.com',
  display_name: 'Owner',
  role: 'member',
};

const WORKSPACE = {
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
};

/** Signed out until `POST /auth/login` succeeds, then signed in — as the real server behaves. */
async function instance(page: Page): Promise<void> {
  let signedIn = false;

  await page.route('**/api/v1/auth/me', (route) =>
    signedIn
      ? route.fulfill({ status: 200, json: IDENTITY })
      : route.fulfill({
          status: 401,
          contentType: 'application/problem+json',
          body: JSON.stringify({ title: 'Not authenticated', status: 401 }),
        }),
  );
  await page.route('**/api/v1/auth/login', (route) => {
    signedIn = true;
    return route.fulfill({
      status: 200,
      json: { user: IDENTITY, credential_kind: 'session' },
      headers: { 'set-cookie': `se_session=${SESSION}; Path=/; HttpOnly; SameSite=Lax` },
    });
  });
  await page.route('**/api/v1/auth/logout', (route) => {
    signedIn = false;
    return route.fulfill({
      status: 204,
      body: '',
      headers: { 'set-cookie': 'se_session=; Path=/; HttpOnly; Max-Age=0' },
    });
  });
  await page.route('**/api/v1/workspaces**', (route) =>
    signedIn
      ? route.fulfill({ status: 200, json: { data: [WORKSPACE], next_cursor: null } })
      : route.fulfill({
          status: 401,
          contentType: 'application/problem+json',
          body: JSON.stringify({ title: 'Not authenticated', status: 401 }),
        }),
  );
}

async function signIn(page: Page): Promise<void> {
  await page.getByLabel('Email').fill(IDENTITY.email);
  await page.getByLabel('Password').fill(PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('link', { name: 'Photos' })).toBeVisible();
}

test(
  'a signed-in app holds no credential in any store the page can read',
  { tag: ['@F-027/FR-3'] },
  async ({ page }) => {
    await instance(page);
    await page.goto('/login');
    await signIn(page);

    const held = await page.evaluate(async () => ({
      local: JSON.stringify(Object.entries(localStorage)),
      session: JSON.stringify(Object.entries(sessionStorage)),
      databases: (await indexedDB.databases()).map((database) => database.name ?? '(unnamed)'),
    }));

    for (const store of [held.local, held.session]) {
      expect(store).not.toContain(PASSWORD);
      expect(store).not.toContain(SESSION);
    }
    // The app opens none at all — there is nothing yet that needs to survive a reload, and an
    // empty list is a stronger claim than an absent key (F-026 will change this deliberately).
    expect(held.databases).toEqual([]);

    // And the cookie really is the one thing holding it, out of reach of any script.
    const cookie = (await page.context().cookies()).find((each) => each.name === 'se_session');
    expect(cookie?.value).toBe(SESSION);
    expect(cookie?.httpOnly).toBe(true);
  },
);

test(
  'no credential is ever put in a URL, and no request leaves this origin',
  { tag: ['@F-027/FR-3'] },
  async ({ page }) => {
    const requested: string[] = [];
    page.on('request', (request) => requested.push(request.url()));

    await instance(page);
    await page.goto('/login');
    await signIn(page);
    await page.goto('/docs');
    await expect(page.getByRole('heading', { name: 'API', exact: true })).toBeVisible();

    expect(requested.length).toBeGreaterThan(0);
    const origin = new URL(page.url()).origin;
    const offOrigin = requested.filter((url) => new URL(url).origin !== origin);
    const leaked = requested.filter((url) => url.includes(PASSWORD) || url.includes(SESSION));

    // FR-2's other half, observed rather than declared: an instance on a private network with no
    // egress at all must work, so nothing — not a font, not a schema viewer — may be off-origin.
    expect(offOrigin).toEqual([]);
    expect(leaked).toEqual([]);
  },
);

test(
  'after signing out, going back reveals nothing from the session',
  { tag: ['@F-027/FR-7'] },
  async ({ page }) => {
    await instance(page);
    await page.goto('/login');
    await signIn(page);

    await page.getByRole('button', { name: 'Sign out' }).click();
    await expect(page.getByLabel('Password')).toBeVisible();

    // The browser may serve the previous document from its own cache; what must not come back is
    // the *data*, which lived in the query cache the logout cleared.
    await page.goBack();

    await expect(page.getByRole('link', { name: 'Photos' })).toHaveCount(0);
    await expect(page.getByText(IDENTITY.email)).toHaveCount(0);
  },
);
