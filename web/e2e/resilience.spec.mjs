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

test('genre wave starts distinct sessions, retries only its own pending session and keeps playing until ready', async ({ page }) => {
  await playLibrary(page, 'genre');
  await page.evaluate(() => { location.hash = '/home'; });
  await expect(page.getByRole('heading', { name: 'Моя волна', exact: true })).toBeVisible();
  const player = page.getByRole('region', { name: 'Прослушивание' });
  const position = player.getByRole('slider', { name: 'Перемотка' });
  const startPosition = Number(await position.inputValue());
  const genre = page.getByRole('combobox', { name: 'Жанр волны' });
  await expect(genre.locator('option')).toHaveText(['Без ограничения', 'Классика', 'Поп', 'Рок', 'Джаз', 'Электроника', 'Хип-хоп']);

  const launch = async (slug, label, path) => {
    await genre.selectOption(slug);
    const [r] = await Promise.all([
      page.waitForResponse((r) => new URL(r.url()).pathname === path && r.request().method() === 'POST'),
      page.getByRole('button', { name: `Моя волна: ${label}: слушать`, exact: true }).click(),
    ]);
    expect(r.status()).toBe(200);
    const body = r.request().postDataJSON();
    if (path.endsWith('/start')) expect(body.seed).toEqual({ genre: slug });
    else expect(body).not.toHaveProperty('seed');
    const wave = await r.json();
    expect(wave).toMatchObject({ seed: { genre: slug }, title: `Моя волна: ${label}` });
    return wave;
  };
  const start = '/api/wave/start';
  const classical = await launch('classical', 'Классика', start);
  expect(classical).toMatchObject({ tracks: [], pending: true });
  await expect(player.getByText('Browser tone', { exact: true })).toBeVisible();

  // Switching surfaces must not carry the classical pending id into the jazz start.
  await page.evaluate(() => { location.hash = '/search'; });
  const jazz = await launch('jazz', 'Джаз', start);
  expect(jazz.session_id).not.toBe(classical.session_id);
  expect(jazz).toMatchObject({ tracks: [], pending: true });
  expect((await launch('jazz', 'Джаз', `/api/wave/${jazz.session_id}/next`)).session_id).toBe(jazz.session_id);
  await expect(player.getByText('Browser tone', { exact: true })).toBeVisible();
  await expect.poll(async () => Number(await position.inputValue())).toBeGreaterThan(startPosition);

  const rock = await launch('rock', 'Рок', start);
  expect(rock.session_id).not.toBe(jazz.session_id);
  expect(rock.tracks[0].ref).toBe('nd:browser-rock');
  await expect(player.getByText('Browser rock', { exact: true })).toBeVisible();
  await openPlayer(page);
  await expect(page.getByText('Моя волна: Рок', { exact: true })).toBeVisible();
});

test('embedded genre wave sends both genres and their balance without restarting on edits', async ({ page }) => {
  await login(page, 'genre-embedded', '/search');
  await page.goto('/?embedded=1#/search');
  const first = page.getByRole('combobox', { name: 'Жанр волны' });
  const second = page.getByRole('combobox', { name: 'Второй жанр' });
  const ratio = page.getByRole('slider', { name: 'Доля первого жанра: Рок' });
  const starts = [];
  page.on('request', r => { if (new URL(r.url()).pathname === '/api/wave/start') starts.push(r.postDataJSON()); });
  await first.selectOption('rock');
  await expect(second.locator('option[value=rock]')).toHaveCount(0);
  await second.selectOption('pop');
  await expect(ratio).toHaveValue('50');
  for (let i = 0; i < 20; i++) await ratio.press('ArrowRight');
  await expect(page.getByText('Рок 70% · Поп 30%')).toBeVisible();
  const [response] = await Promise.all([
    page.waitForResponse((r) => new URL(r.url()).pathname === '/api/wave/start'),
    page.getByRole('button', { name: 'Моя волна: Рок 70% + Поп 30%: слушать' }).click(),
  ]);
  expect(response.request().postDataJSON()).toMatchObject({
    seed: { genre: 'rock', second_genre: 'pop', first_percent: 70 }, client: 'pwa-app', origin: 'catalog_card',
  });
  expect(response.status()).toBe(200);
  const player = page.getByRole('region', { name: 'Прослушивание' });
  await expect(player.getByText('Browser rock', { exact: true })).toBeVisible();
  await expect.poll(async () => Number(await player.getByRole('slider', { name: 'Перемотка' }).inputValue())).toBeGreaterThan(0);
  await page.evaluate(() => { location.hash = '/downloads'; });
  await expect(first).toBeHidden();
  await page.evaluate(() => { location.hash = '/search'; });
  await expect(first).toHaveValue('rock');
  await expect(second).toHaveValue('pop');
  await expect(ratio).toHaveValue('70');
  await second.selectOption('');
  await expect(ratio).toBeHidden();
  await expect(page.getByRole('button', { name: 'Моя волна: Рок: слушать' })).toBeVisible();
  await first.selectOption('');
  await expect(second).toBeHidden();
  await expect(page.getByRole('button', { name: 'Моя волна: слушать' })).toBeVisible();
  await expect(player.getByText('Browser rock', { exact: true })).toBeVisible();
  expect(starts).toHaveLength(1);
});

test('assistant search results play, like and download separately, including after reopening the chat', async ({ page }) => {
  await login(page, 'alice', '/assistant');
  await page.getByRole('textbox', { name: 'Сообщение ассистенту' }).fill('Найди песни Browser');
  await page.getByRole('button', { name: 'Отправить' }).click();
  const list = page.getByRole('list', { name: 'Найденные песни' });
  await expect(list.getByRole('listitem')).toHaveCount(3);
  await page.reload();
  await list.getByRole('button', { name: 'Слушать «Browser rock»', exact: true }).click();
  const player = page.getByRole('region', { name: 'Прослушивание' });
  await expect(player.getByText('Browser rock', { exact: true })).toBeVisible();
  await expect.poll(async () => Number(await player.getByRole('slider', { name: 'Перемотка' }).inputValue())).toBeGreaterThan(0);

  const [like] = await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/api/star') && r.request().method() === 'POST'),
    list.getByRole('button', { name: '«Browser single» — в понравившиеся' }).click(),
  ]);
  expect(like.ok()).toBeTruthy();
  expect(like.request().postDataJSON().targets[0].ref).toBe('dz:710');
  await expect(list.getByRole('button', { name: 'Убрать «Browser single» из понравившихся' })).toHaveAttribute('aria-pressed', 'true');

  const demo = list.getByRole('listitem').filter({ hasText: 'Browser demo' });
  const [download] = await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/api/downloads') && r.request().method() === 'POST'),
    demo.getByRole('button', { name: 'Скачать трек' }).click(),
  ]);
  expect(download.ok()).toBeTruthy();
  expect(download.request().postDataJSON()).toEqual({ track_refs: ['dz:711'] });
  await expect(demo.getByText('качается')).toBeVisible();
  await expect(demo.getByRole('button', { name: '«Browser demo» — в понравившиеся' })).toHaveAttribute('aria-pressed', 'false');
});

test('an automatic update waits for an unsent screenshot even without message text', async ({ page }) => {
  await login(page, 'alice', '/assistant');
  await page.locator('input[type=file][accept="image/*"]').setInputFiles({
    name: 'draft.png', mimeType: 'image/png',
    buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/L1sAAAAASUVORK5CYII=', 'base64'),
  });
  await expect(page.getByRole('img', { name: 'draft.png' })).toBeVisible();
  await page.route('**/healthz', route => route.fulfill({ json: { web_build: 'next-assistant-build' } }));
  const [check] = await Promise.all([
    page.waitForResponse('**/healthz'),
    page.evaluate(() => window.dispatchEvent(new Event('hm:resume'))),
  ]);
  await check.finished();
  const remove = page.getByRole('button', { name: 'Убрать скриншот' });
  await remove.click({ timeout: 5000 });
  await expect(remove).toBeHidden();
  await Promise.all([
    page.waitForEvent('domcontentloaded', { timeout: 5000 }),
    page.evaluate(() => window.dispatchEvent(new Event('hm:resume'))),
  ]);
  await expect(page.getByRole('textbox', { name: 'Сообщение ассистенту' })).toBeVisible();
});

test('an oversized screenshot does not displace a valid attachment or erase the draft', async ({ page }) => {
  await login(page, 'alice', '/assistant');
  const input = page.getByRole('textbox', { name: 'Сообщение ассистенту' });
  await input.fill('Сравни скриншоты');
  await input.evaluate(el => {
    const clipboardData = new DataTransfer();
    const limit = 15 * 1024 * 1024;
    const png = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/L1sAAAAASUVORK5CYII='), c => c.charCodeAt(0));
    for (const [name, size] of [['too-big.png', limit + 1], ['at-limit.png', limit]]) {
      clipboardData.items.add(new File([png, new Uint8Array(size - png.length)], name, { type: 'image/png' }));
    }
    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true }));
  });
  await expect(page.getByRole('img', { name: 'at-limit.png' })).toBeVisible();
  await expect(page.getByRole('img', { name: 'too-big.png' })).toHaveCount(0, { timeout: 2000 });
  await expect(page.getByText(/15 МиБ/)).toBeVisible();
  await expect(input).toHaveValue('Сравни скриншоты');
  await expect(page.getByRole('button', { name: 'Отправить' })).toBeEnabled();
});

test('a new chat clears the composer but the first reply in the same chat does not', async ({ page }) => {
  await login(page, 'alice', '/assistant');
  const input = page.getByRole('textbox', { name: 'Сообщение ассистенту' });
  const send = page.getByRole('button', { name: 'Отправить' });
  const file = page.locator('input[type=file][accept="image/*"]');
  await input.fill('Это для другого разговора');
  await file.setInputFiles({ name: 'old.svg', mimeType: 'image/svg+xml', buffer: Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>') });
  await expect(page.getByRole('img', { name: 'old.svg' })).toBeVisible();
  await page.getByRole('button', { name: 'Новый чат' }).click();
  await expect(input).toHaveValue('');
  await expect(page.getByRole('img', { name: 'old.svg' })).toHaveCount(0);
  await expect(send).toBeDisabled();

  let capture;
  const pending = new Promise(resolve => { capture = resolve; });
  await page.route('**/api/assistant/chat', route => { capture(route); });
  await input.fill('Первое сообщение');
  await send.click();
  const request = await pending;
  await input.fill('Уточнение к первому вопросу');
  await request.fulfill({ json: { chat_id: 57, reply: 'Первый ответ' } });
  await expect(page.getByText('Первый ответ', { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#\/assistant\?chat=57$/);
  await expect(input).toHaveValue('Уточнение к первому вопросу');
});

test('an automatic update preserves a pending chat reply across navigation', async ({ page }) => {
  await login(page, 'alice', '/assistant');
  let capture;
  const pending = new Promise(resolve => { capture = resolve; });
  await page.route('**/api/assistant/chat', route => { capture(route); });
  await page.getByRole('textbox', { name: 'Сообщение ассистенту' }).fill('Подбери музыку');
  await page.getByRole('button', { name: 'Отправить' }).click();
  const request = await pending;
  await page.evaluate(() => { location.hash = '/more'; });
  await expect(page.getByRole('heading', { name: 'Ещё', exact: true })).toBeVisible();
  await page.route('**/healthz', route => route.fulfill({ json: { web_build: 'next-chat-build' } }));
  const [check] = await Promise.all([
    page.waitForResponse('**/healthz'),
    page.evaluate(() => window.dispatchEvent(new Event('hm:resume'))),
  ]);
  await check.finished();
  await page.getByRole('link', { name: /Ассистент/ }).click();
  await expect(page.getByText('Подбери музыку', { exact: true })).toBeVisible({ timeout: 5000 });
  await request.fulfill({ json: { chat_id: 42, reply: 'Ответ на первое сообщение' } });
  await expect(page.getByText('Ответ на первое сообщение', { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#\/assistant\?chat=42$/);
  await Promise.all([
    page.waitForEvent('domcontentloaded', { timeout: 5000 }),
    page.evaluate(() => { location.hash = '/more'; }),
  ]);
  await expect(page.getByRole('heading', { name: 'Ещё', exact: true })).toBeVisible();
});

test('switching accounts starts a separate assistant conversation', async ({ page }) => {
  const turns = [];
  await page.route('**/api/assistant/chat', route => {
    turns.push(route.request().postDataJSON());
    return route.fulfill({ json: { chat_id: turns.length, reply: `Ответ ${turns.length}` } });
  });
  await login(page, 'alice', '/assistant');
  const input = page.getByRole('textbox', { name: 'Сообщение ассистенту' });
  const send = page.getByRole('button', { name: 'Отправить' });
  await input.fill('Плейлист для Алисы');
  await send.click();
  await expect(page.getByText('Ответ 1', { exact: true })).toBeVisible();
  await page.evaluate(() => { location.hash = '/more'; });
  await page.getByRole('button', { name: 'Выйти', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Выйти', exact: true }).click();
  await page.getByLabel('Имя или почта').fill('bob');
  await page.getByLabel('Пароль', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await expect(page.getByRole('navigation', { name: 'Разделы' })).toBeVisible();
  await page.evaluate(() => { location.hash = '/assistant'; });
  await expect(input).toBeVisible();
  await expect(page.getByText('Плейлист для Алисы', { exact: true })).toHaveCount(0);
  await input.fill('Музыка для Боба');
  await send.click();
  await expect(page.getByText('Ответ 2', { exact: true })).toBeVisible();
  expect(turns[1].messages).toEqual([{ role: 'user', content: 'Музыка для Боба' }]);
  expect(turns[1].chat_id ?? null).toBeNull();
});

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

test('device panel clears the connection warning on reconnect without losing a playback error', async ({ page, context }) => {
  await login(page, 'offline', '/player');
  const panel = page.getByRole('region', { name: 'Устройства и общий сеанс' });
  await expect(panel).toContainText('связь с сервером установлена');
  await panel.getByRole('button', { name: 'Разрешить звук' }).click();
  const alert = panel.getByRole('alert');
  await expect(alert).toContainText('Сначала передайте трек');
  await context.setOffline(true);
  await expect(panel).toContainText('нет связи с сервером');
  await expect(alert).not.toContainText('Сначала передайте трек');
  await context.setOffline(false);
  await expect(panel).toContainText('связь с сервером установлена');
  await expect(alert).toContainText('Сначала передайте трек');
});

test('remote seek stays bound to the track visible when the drag began', async ({ page }) => {
  await login(page, 'seek', '/player');
  const send = async (route, body) => {
    const response = await page.request.post(`/api/playback/${route}`, { data: { device_id: 'source', ...body } });
    expect(response.ok()).toBeTruthy();
    return response.json();
  };
  const heartbeat = (body = {}) => send('heartbeat', { name: 'Source', kind: 'desktop', ...body });
  const view = await heartbeat();
  const tracks = ['First', 'Second'].map((title) => ({
    title, artist: 'Test', album: '', ref: `nd:${title}`, duration_s: 120,
  }));
  const state = { queue: tracks, index: 0, occurrence: 'first', position: 0, playing: true, volume: 1, repeat: 'off', shuffle: false };
  const first = await send('claim', { revision: view.revision, state });
  const panel = page.getByRole('region', { name: 'Устройства и общий сеанс' });
  await expect(panel.getByText('Test — First', { exact: true })).toBeVisible();
  const slider = panel.getByRole('slider', { name: 'Позиция на устройстве' });
  await slider.scrollIntoViewIfNeeded();
  const drag = async () => {
    const box = await slider.boundingBox();
    await page.mouse.move(box.x + box.width * 0.6, box.y + box.height / 2);
    await page.mouse.down();
  };
  const release = () => Promise.all([
    page.waitForResponse((r) => new URL(r.url()).pathname === '/api/playback/command'), page.mouse.up(),
  ]);
  await drag();
  const next = { ...state, index: 1, occurrence: 'second' };
  const second = await heartbeat({ revision: first.revision, state: next });
  await expect(panel.getByText('Test — Second', { exact: true })).toBeVisible();
  await release();
  expect((await heartbeat()).commands).toEqual([]);

  await drag();
  await heartbeat({ revision: second.revision, state: { ...next, position: 9 } });
  await expect(panel).toContainText('Позиция устройства: 0:09');
  await release();
  const [command] = (await heartbeat()).commands;
  expect(command.kind).toBe('seek');
  expect(command.value).toBeGreaterThan(60);
});

test('revoked session returns to login and closes the old playback', async ({ page }) => {
  await playLibrary(page, 'expiry');
  // Revoke the real server-side session without replacing the application's HTTP client.
  const logout = await page.request.post('/api/auth/logout');
  const library = page.getByRole('navigation', { name: 'Разделы' }).getByRole('link', { name: 'Библиотека' });
  expect(logout.ok()).toBeTruthy();
  await page.evaluate(() => { location.hash = '/downloads'; });
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeVisible();
  await expect(page.getByRole('slider', { name: 'Перемотка', exact: true })).toBeHidden();
  await page.getByLabel('Имя или почта').fill('bob');
  await page.getByLabel('Пароль', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await expect(page.getByText('bob download 0', { exact: true })).toBeVisible();
  await expect(library).toHaveAttribute('href', '#/library');
  await openPlayer(page);
  await expect(page.getByText('Сейчас ничего не играет', { exact: true })).toBeVisible();
  await expect(page.getByText('Browser tone', { exact: true })).toHaveCount(0);
});

test('offline logout warns that the session may still be active and allows a retry', async ({ page, context }) => {
  await login(page, 'offline', '/more');
  const logout = page.getByRole('button', { name: 'Выйти', exact: true });
  const confirm = page.getByRole('alertdialog', { name: 'Выйти из аккаунта?' }).getByRole('button', { name: 'Выйти', exact: true });
  await context.setOffline(true);
  await logout.click();
  await confirm.click();
  await expect(page.getByRole('alert')).toContainText('Выход не подтверждён');
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeHidden();

  await context.setOffline(false);
  expect((await page.request.get('/api/auth/me')).status()).toBe(200);
  await logout.click();
  await confirm.click();
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeVisible();
  expect((await page.request.get('/api/auth/me')).status()).toBe(401);
});

test('a late upload rejection does not sign out the next account', async ({ page }) => {
  await login(page, 'alice', '/upload');
  let holdUpload;
  const pending = new Promise(resolve => { holdUpload = resolve; });
  let submissions = 0;
  await page.route('**/api/uploads', (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    if (++submissions === 1) holdUpload(route);
    else return route.abort();
  });
  await page.locator('input[type=file][multiple]').setInputFiles(['first.wav', 'second.wav'].map(name => ({
    name, mimeType: 'audio/wav', buffer: Buffer.from('delayed upload'),
  })));
  const upload = await pending;
  await page.evaluate(() => { location.hash = '/more'; });
  await page.getByRole('button', { name: 'Выйти', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Выйти', exact: true }).click();
  await page.getByLabel('Имя или почта').fill('bob');
  await page.getByLabel('Пароль', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await expect(page.getByRole('navigation', { name: 'Разделы' })).toBeVisible();

  await upload.fulfill({ status: 401, json: { detail: 'Сессия истекла' } });
  await page.evaluate(() => { location.hash = '/downloads'; });
  await expect(page.getByText('bob download 0', { exact: true })).toBeVisible();
  expect(submissions).toBe(1);
});

test('explicit logout and account switch do not expose the previous queue or downloads', async ({ page }) => {
  await playLibrary(page, 'alice');
  await page.evaluate(() => { location.hash = '/more'; });
  const library = page.getByRole('navigation', { name: 'Разделы' }).getByRole('link', { name: 'Библиотека' });
  await expect(library).toHaveAttribute('href', '#/liked');
  await page.getByRole('button', { name: 'Выйти', exact: true }).click();
  await page.getByRole('alertdialog', { name: 'Выйти из аккаунта?' }).getByRole('button', { name: 'Выйти', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeVisible();
  await page.getByLabel('Имя или почта').fill('bob');
  await page.getByLabel('Пароль', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await page.evaluate(() => { location.hash = '/downloads'; });
  await expect(page.getByText('bob download 0', { exact: true })).toBeVisible();
  await expect(page.getByText('alice download 0', { exact: true })).toHaveCount(0);
  await library.click();
  await expect(page).toHaveURL(/#\/library$/);
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

test('a browser can play the selected track locally without taking another device’s session', async ({ page, browser }) => {
  await playLibrary(page, 'listener');
  const source = page.getByRole('region', { name: 'Устройства и общий сеанс' });
  await source.getByRole('button', { name: 'Сделать этот браузер владельцем' }).click();
  await expect(source).toContainText('Владелец: Браузер (здесь)');

  const target = await browser.newPage({ baseURL: new URL(page.url()).origin });
  try {
    await login(target, 'listener', '/player');
    await expect(target.getByRole('region', { name: 'Устройства и общий сеанс' })).toContainText('Владелец: Браузер');
    await target.evaluate(() => { location.hash = '/liked'; });
    const track = target.getByRole('button', { name: 'Слушать «Browser tone»', exact: true });
    const streamPath = '/api/listen/stream';
    let requests = 0;
    target.on('request', (req) => { if (new URL(req.url()).pathname === streamPath) requests++; });
    await track.click();
    const dialog = target.getByRole('alertdialog', { name: 'Слушать в этом браузере?' });
    await dialog.getByText('Отмена', { exact: true }).click();
    await expect(dialog).toBeHidden();
    expect(requests).toBe(0);

    await track.click();
    const [audio] = await Promise.all([
      target.waitForResponse((r) => new URL(r.url()).pathname === streamPath),
      dialog.getByRole('button', { name: 'Слушать локально' }).click(),
    ]);
    expect(new URL(audio.url()).searchParams.get('ref')).toBe('nd:browser-tone');
    expect([200, 206]).toContain(audio.status());
    expect(audio.headers()['content-type']).toMatch(/^audio\//);
    await openPlayer(target);
    await expect(target.getByRole('region', { name: 'Очередь' }).getByRole('button', {
      name: 'Убрать «Browser tone» из понравившихся', pressed: true,
    })).toBeVisible();
    await expect(target.getByRole('region', { name: 'Устройства и общий сеанс' })).toContainText('Локальный режим');
    await expect.poll(async () => Number(await target.getByRole('slider', { name: 'Перемотка', exact: true }).inputValue())).toBeGreaterThan(0);
    await expect(source).toContainText('Владелец: Браузер (здесь)');
  } finally {
    await target.close();
  }
});

test('former owner can choose local playback after an offline recovery', async ({ page, context, browser }) => {
  await playLibrary(page, 'recovery');
  const panel = { name: 'Устройства и общий сеанс' };
  const source = page.getByRole('region', panel);
  await source.getByRole('button', { name: 'Сделать этот браузер владельцем' }).click();
  await expect(source).toContainText('Владелец: Браузер (здесь)');
  const target = await browser.newPage({ baseURL: new URL(page.url()).origin, storageState: await context.storageState() });
  try {
    await context.setOffline(true);
    await target.goto('/#/player');
    const devices = target.getByRole('region', panel);
    await expect(devices.getByRole('listitem').filter({ hasText: 'владелец' })).toContainText('не в сети', { timeout: 22_000 });
    await devices.getByRole('button', { name: 'Слушать здесь', exact: true }).click();
    await expect(devices).toContainText('Владелец: Браузер (здесь)');
    await context.setOffline(false);
    await expect(source).toContainText('Передача завершена');
    await expect(page.getByRole('button', { name: 'Слушать', exact: true })).toBeVisible();
    await page.evaluate(() => { location.hash = '/liked'; });
    await page.getByRole('button', { name: 'Слушать «Browser tone»', exact: true }).click();
    await page.getByRole('alertdialog', { name: 'Слушать в этом браузере?' })
      .getByRole('button', { name: 'Слушать локально' }).click({ timeout: 5000 });
    await openPlayer(page);
    await expect(source).toContainText('Локальный режим');
    await expect.poll(async () => Number(await page.getByRole('slider', { name: 'Перемотка', exact: true }).inputValue())).toBeGreaterThan(0);
    await expect(devices).toContainText('Владелец: Браузер (здесь)');
  } finally {
    await target.close();
  }
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
