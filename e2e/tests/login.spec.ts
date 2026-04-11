import { test, expect } from '@playwright/test';

test('ログイン画面が表示されること', async ({ page }) => {
  await page.goto('/login');
  await expect(page).toHaveTitle(/フォトマネージャー/);
  await expect(page.getByRole('button', { name: 'ログイン' })).toBeVisible();
});
