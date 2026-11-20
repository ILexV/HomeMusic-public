import assert from 'node:assert/strict';
import test from 'node:test';
import { api, resetApiSession } from '../src/api/client.ts';

test('an artist request from a signed-out account cannot replace the next account’s cached library', async (t) => {
  let finishOld;
  const delayed = new Promise(resolve => { finishOld = resolve; });
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json([{ name: 'Bob artist' }]));
  fetch.mock.mockImplementationOnce(() => delayed);
  t.after(resetApiSession);

  const previous = api.ndArtists();
  resetApiSession();
  const current = await api.ndArtists();
  assert.equal(current[0].name, 'Bob artist');
  finishOld(Response.json([{ name: 'Alice artist' }]));
  await assert.rejects(previous, { name: 'AbortError' });
  assert.deepEqual(await api.ndArtists(), current);
  assert.equal(fetch.mock.callCount(), 2);
});

test('a delayed 401 from an old session is cancelled, not handled as a current login failure', async (t) => {
  let respond;
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => { respond = resolve; }));
  const pending = api.me();
  resetApiSession();
  respond(Response.json({ detail: 'Сессия истекла' }, { status: 401 }));
  await assert.rejects(pending, { name: 'AbortError' });
});

for (const status of [200, 403]) {
  test(`switching accounts while reading a ${status} response discards the old body`, async (t) => {
    let sink;
    const stream = new ReadableStream({ start(controller) { sink = controller; } });
    const response = new Response(stream, { status, headers: { 'Content-Type': 'application/json' } });
    t.mock.method(globalThis, 'fetch', async () => response);
    const pending = api.me();
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(response.bodyUsed);
    resetApiSession();
    const body = status === 200 ? { username: 'alice' } : { detail: 'Alice-only detail' };
    sink.enqueue(new TextEncoder().encode(JSON.stringify(body)));
    sink.close();
    await assert.rejects(pending, { name: 'AbortError' });
  });
}
