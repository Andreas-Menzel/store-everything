import { expect, test } from '@playwright/test';

/**
 * The smoke path: the app boots, calls the API through the generated client, and renders
 * what the server said. The API is stubbed at the network boundary so the test asserts
 * our wiring rather than the state of whatever instance happens to be running.
 */

test('renders the shell and reports a ready instance', async ({ page }) => {
  await page.route('**/readyz', (route) =>
    route.fulfill({ status: 200, json: { status: 'ready' } }),
  );

  await page.goto('/');

  await expect(page.getByRole('heading', { name: 'Store Everything' })).toBeVisible();
  await expect(page.getByText('Instance ready')).toBeVisible();
});

test('reports an unavailable instance when readiness fails', async ({ page }) => {
  // 503 is what /readyz answers while migrations are pending — a state, not a crash.
  await page.route('**/readyz', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/problem+json',
      body: JSON.stringify({ title: 'Service not ready', status: 503 }),
    }),
  );

  await page.goto('/');

  await expect(page.getByText('Instance unavailable')).toBeVisible();
});

test('re-checks readiness on demand', async ({ page }) => {
  let attempts = 0;
  await page.route('**/readyz', (route) => {
    attempts += 1;
    return attempts === 1
      ? route.fulfill({ status: 503, body: '{}' })
      : route.fulfill({ status: 200, json: { status: 'ready' } });
  });

  await page.goto('/');
  await expect(page.getByText('Instance unavailable')).toBeVisible();

  await page.getByRole('button', { name: 'Check again' }).click();

  await expect(page.getByText('Instance ready')).toBeVisible();
});
