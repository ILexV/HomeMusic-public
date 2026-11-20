import assert from 'node:assert/strict';
import test from 'node:test';
import { api, invalidateCache } from '../src/api/client.ts';

test('an artist request from a signed-out account cannot replace the next account’s cached library', async (t) => {
  let finishOld;
  const delayed = new Promise(resolve => { finishOld = resolve; });
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json([{ name: 'Bob artist' }]));
  fetch.mock.mockImplementationOnce(() => delayed);
  t.after(invalidateCache);

  const previous = api.ndArtists();
  invalidateCache();
  const current = await api.ndArtists();
  assert.equal(current[0].name, 'Bob artist');
  finishOld(Response.json([{ name: 'Alice artist' }]));
  await assert.rejects(previous, { name: 'AbortError' });
  assert.deepEqual(await api.ndArtists(), current);
  assert.equal(fetch.mock.callCount(), 2);
});
