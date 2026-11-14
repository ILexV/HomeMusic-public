import { test, expect } from '@playwright/test';

const password = 'browser-test-password';

async function login(page, user, route = '/downloads') {
  await page.goto(`/#${route}`);
  await page.getByLabel('Имя или почта').fill(user);
  await page.getByLabel('Пароль', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeHidden();
}

async function openPlayer(page) {
  // Hash navigation preserves the running HTMLMediaElement, unlike a page reload.
  await page.evaluate(() => { location.hash = '/player'; });
  await expect(page.getByRole('region', { name: 'Устройства и общий сеанс' })).toBeVisible();
}

async function playLibrary(page, user) {
  await login(page, user, '/liked');
  await page.getByRole('button', { name: 'Слушать', exact: true }).click();
  await openPlayer(page);
  await expect.poll(async () => Number(await page.getByRole('slider', { name: 'Перемотка', exact: true }).inputValue())).toBeGreaterThan(0);
}

test('network loss keeps the account and a retry restores the real library', async ({ page, context }) => {
  await login(page, 'offline');
  await expect(page.getByText('offline download 0', { exact: true })).toBeVisible();
  await context.setOffline(true);
  await page.evaluate(() => { location.hash = '/liked'; });
  await expect(page.getByText('Нет связи с сервером', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeHidden();
  await context.setOffline(false);
  await page.getByRole('button', { name: /Повторить/ }).click();
  await expect(page.getByText('Browser tone', { exact: true })).toBeVisible();
});

test('revoked session returns to login and closes the old playback', async ({ page }) => {
  await playLibrary(page, 'expiry');
  // Revoke the real server-side session without replacing the application's HTTP client.
  const logout = await page.request.post('/api/auth/logout');
  expect(logout.ok()).toBeTruthy();
  await page.evaluate(() => { location.hash = '/downloads'; });
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeVisible();
  await expect(page.getByRole('slider', { name: 'Перемотка', exact: true })).toBeHidden();
  await page.getByLabel('Имя или почта').fill('bob');
  await page.getByLabel('Пароль', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await expect(page.getByText('bob download 0', { exact: true })).toBeVisible();
  await openPlayer(page);
  await expect(page.getByText('Сейчас ничего не играет', { exact: true })).toBeVisible();
  await expect(page.getByText('Browser tone', { exact: true })).toHaveCount(0);
});

test('explicit logout and account switch do not expose the previous queue or downloads', async ({ page }) => {
  await playLibrary(page, 'alice');
  await page.evaluate(() => { location.hash = '/more'; });
  await page.getByRole('button', { name: 'Выйти', exact: true }).click();
  await page.getByRole('alertdialog', { name: 'Выйти из аккаунта?' }).getByRole('button', { name: 'Выйти', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeVisible();
  await page.getByLabel('Имя или почта').fill('bob');
  await page.getByLabel('Пароль', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await page.evaluate(() => { location.hash = '/downloads'; });
  await expect(page.getByText('bob download 0', { exact: true })).toBeVisible();
  await expect(page.getByText('alice download 0', { exact: true })).toHaveCount(0);
  await openPlayer(page);
  await expect(page.getByText('Сейчас ничего не играет', { exact: true })).toBeVisible();
});

test('device handoff prepares real audio, stops the source and starts the target', async ({ page, context }) => {
  await playLibrary(page, 'handoff');
  const source = page.getByRole('region', { name: 'Устройства и общий сеанс' });
  await source.getByRole('button', { name: 'Сделать этот браузер владельцем' }).click();
  await expect(source).toContainText('Владелец: Браузер (здесь)');
  const target = await context.newPage();
  await target.goto('/#/player');
  const targetDevices = target.getByRole('region', { name: 'Устройства и общий сеанс' });
  await targetDevices.getByRole('button', { name: 'Разрешить звук' }).click();
  await targetDevices.getByRole('button', { name: 'Слушать здесь', exact: true }).click();
  await expect(targetDevices).toContainText('Передача завершена', { timeout: 30_000 });
  await expect(targetDevices).toContainText('Владелец: Браузер (здесь)');
  await expect(page.getByRole('button', { name: 'Слушать', exact: true })).toBeVisible();
  const position = target.getByRole('slider', { name: 'Перемотка', exact: true });
  const before = Number(await position.inputValue());
  await expect.poll(async () => Number(await position.inputValue())).toBeGreaterThan(before);
  const stopped = Number(await page.getByRole('slider', { name: 'Перемотка', exact: true }).inputValue());
  expect(stopped).toBeLessThanOrEqual(Number(await position.inputValue()));
});

test('problem filter finds an old failure outside the latest 200 downloads', async ({ page }) => {
  await login(page, 'pagination');
  await page.getByRole('tab', { name: /Проблемы/ }).click();
  await expect(page.getByText('pagination download 0', { exact: true })).toBeVisible();
  await expect(page.getByText('pagination download 224', { exact: true })).toHaveCount(0);
});

test('administrator sees real queue, disk and backup readiness', async ({ page }) => {
  await login(page, 'operator', '/admin/system');
  await expect(page.getByRole('heading', { name: 'Состояние сервиса', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Компоненты' })).toContainText('Navidrome');
  await expect(page.getByRole('region', { name: 'Свободное место' })).toContainText('Данные Hub');
  await expect(page.getByRole('region', { name: 'Резервные копии' })).toContainText('Не настроена');
});

test('recommendation comparison distinguishes missing evidence from zero quality', async ({ page }) => {
  await login(page, 'offline', '/recommendations');
  await expect(page.getByRole('heading', { name: 'Качество рекомендаций', exact: true })).toBeVisible();
  await expect(page.getByText('Недостаточно данных', { exact: false }).first()).toBeVisible();
  await expect(page.getByText('Ошибки воспроизведения', { exact: true })).toBeVisible();
});
