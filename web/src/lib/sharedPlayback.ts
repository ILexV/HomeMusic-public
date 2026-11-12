import { useSyncExternalStore } from 'react';
import { errorMessage } from '../api/client';
import { playbackRequest, type PlaybackAck, type PlaybackCommand, type PlaybackControl, type PlaybackSnapshot, type PlaybackView } from '../api/playback';
import { newEventId } from './plays';

export interface PlaybackAdapter {
  snapshot(): PlaybackSnapshot | null;
  execute(command: PlaybackCommand, signal: AbortSignal): Promise<PlaybackSnapshot>;
  pause(): void;
  unlock(): Promise<void>;
}

interface SharedState {
  view: PlaybackView | null;
  busy: boolean;
  connected: boolean;
  error: string | null;
  enabled: boolean;
  localOnly: boolean;
}
let state: SharedState = { view: null, busy: false, connected: false, error: null, enabled: false, localOnly: false };
const listeners = new Set<() => void>();
function update(patch: Partial<SharedState>) {
  state = { ...state, ...patch };
  for (const fn of listeners) fn();
}
const subscribe = (fn: () => void) => { listeners.add(fn); return () => { listeners.delete(fn); }; };
export function useSharedPlayback() {
  return useSyncExternalStore(subscribe, () => state);
}
const tabDeviceId = newEventId();

let runtime: Runtime | null = null;
let activeAdapter: PlaybackAdapter | null = null;
let activeAccount: number | null = null;

/** A tab identity survives pagehide/pageshow without making a second browser its owner. */
class Runtime {
  readonly id = tabDeviceId;
  readonly ctl = new AbortController();
  private timer: number;
  private lastHeartbeat = performance.now();
  private nextHeartbeat = 0;
  private inFlight = false;
  private queuedAction: (() => void) | null = null;
  private pendingAck: PlaybackAck | null = null;
  private completed = new Map<string, PlaybackAck>();
  private operation: AbortController | null = null;
  private prepared: { id: string; deadline: number } | null = null;
  private leaseLost = false;
  private published = false;
  private recoveryFence: string | null = null;

  constructor(readonly adapter: PlaybackAdapter, private attachAfterHeartbeat = false) {
    update({ view: null, busy: false, connected: false, error: null, enabled: true, localOnly: false });
    this.timer = window.setInterval(() => {
      const expired = performance.now() - this.lastHeartbeat > 12_000;
      if (expired && !this.leaseLost) {
        this.leaseLost = true;
        if (this.published || this.prepared || state.view?.owner === this.id) {
          this.operation?.abort();
          this.adapter.pause();
          this.prepared = null;
        }
        update({ connected: false, error: 'Связь с сервером потеряна. Общее воспроизведение приостановлено; продолжите вручную.' });
      }
      if (this.prepared && Date.now() / 1000 > this.prepared.deadline) {
        this.operation?.abort();
        this.adapter.pause();
        this.prepared = null;
        update({ error: 'Время передачи истекло. Повторите передачу.' });
      }
      if (performance.now() >= this.nextHeartbeat) void this.heartbeat();
    }, 500);
    void this.heartbeat();
  }

  close() {
    this.queuedAction = null;
    this.ctl.abort();
    this.operation?.abort();
    clearInterval(this.timer);
    if (this.published || this.prepared) this.adapter.pause();
  }

  private accept(view: PlaybackView) {
    if (this.ctl.signal.aborted) return;
    const transfer = view.handoff;
    const recoveryFence = transfer?.recovery && transfer.source === this.id ? transfer.id : null;
    if (recoveryFence && recoveryFence !== this.recoveryFence) this.adapter.pause();
    this.recoveryFence = recoveryFence;
    if (this.prepared && (!transfer || transfer.id !== this.prepared.id || transfer.phase === 'failed' || transfer.phase === 'aborted' || transfer.phase === 'done')) {
      if (view.owner !== this.id) this.adapter.pause();
      this.prepared = null;
    }
    this.published = view.owner === this.id;
    update({ view, connected: true, ...(view.error || transfer?.error ? { error: view.error || transfer?.error || null } : {}) });
  }

  private async send(path: Parameters<typeof playbackRequest>[0], body: unknown) {
    const view = await playbackRequest(path, body, this.ctl.signal);
    if (this.ctl.signal.aborted) throw new Error('Сеанс закрыт');
    this.accept(view);
    return view;
  }

  private async acknowledge() {
    if (!this.pendingAck) return state.view;
    const view = await this.send('ack', this.pendingAck);
    this.pendingAck = null;
    return view;
  }

  private async commands(initial: PlaybackView) {
    let view = initial;
    // Acknowledgements can return the next transfer phase, but never an unbounded work loop.
    for (let count = 0; count < 32 && !this.ctl.signal.aborted; count++) {
      const command = view.commands[0];
      if (!command) break;
      update({ busy: true });
      const prior = this.completed.get(command.id);
      if (prior) this.pendingAck = prior;
      else {
        let ack: PlaybackAck = { device_id: this.id, command_id: command.id, ok: false };
        try {
          if (command.expires_at <= Date.now() / 1000) throw new Error('Срок команды истёк');
          if (this.leaseLost) throw new Error('Нет действующего соединения с сервером');
          const transfer = view.handoff;
          if (command.kind === 'prepare' || command.kind === 'start' || command.kind === 'stop') {
            const phase = command.kind === 'prepare' ? 'preparing' : command.kind === 'stop' ? 'stopping' : 'starting';
            if (!transfer || transfer.id !== command.handoff_id || transfer.phase !== phase
              || (command.kind === 'stop' ? transfer.source : transfer.target) !== this.id) throw new Error('Передача уже не активна');
            if (command.kind === 'start' && this.prepared?.id !== transfer.id) throw new Error('Устройство не подготовлено. Повторите передачу.');
            if (command.kind !== 'stop') this.prepared = { id: transfer.id, deadline: command.expires_at };
          } else if (view.owner !== this.id || this.recoveryFence || (transfer && ['preparing', 'stopping', 'starting'].includes(transfer.phase))) {
            throw new Error('Устройство больше не управляет сеансом');
          }
          this.operation = new AbortController();
          const abort = () => this.operation?.abort();
          this.ctl.signal.addEventListener('abort', abort, { once: true });
          const timeout = setTimeout(abort, Math.max(1, Math.min(20_000, command.expires_at * 1000 - Date.now())));
          // Media preparation is not an HTTP request. Keep the lease alive while it decodes,
          // with at most one network request; once ready, stop pulses and acknowledge first.
          let pulse: Promise<void> | null = null;
          const pulseTimer = setInterval(() => {
            if (pulse || this.ctl.signal.aborted || this.operation?.signal.aborted) return;
            pulse = (async () => {
              try {
                const latest = await this.send('heartbeat', {
                  device_id: this.id, name: 'Браузер', kind: 'web', state: null, revision: state.view?.revision ?? null,
                });
                this.lastHeartbeat = performance.now();
                this.leaseLost = false;
                if (command.handoff_id) {
                  const phase = command.kind === 'prepare' ? 'preparing' : command.kind === 'stop' ? 'stopping' : 'starting';
                  if (latest.handoff?.id !== command.handoff_id || latest.handoff.phase !== phase) abort();
                } else if (latest.owner !== this.id || this.recoveryFence) abort();
              } catch (e) {
                if (!this.ctl.signal.aborted) update({ connected: false, error: errorMessage(e) });
              } finally { pulse = null; }
            })();
          }, 2000);
          try {
            const snapshot = await this.adapter.execute(command, this.operation.signal);
            if (this.operation.signal.aborted || command.expires_at <= Date.now() / 1000) throw new Error('Время команды истекло');
            ack = { ...ack, ok: true, state: snapshot };
          } finally {
            clearInterval(pulseTimer);
            if (pulse) await pulse;
            const current = state.view;
            const phase = command.kind === 'prepare' ? 'preparing' : command.kind === 'stop' ? 'stopping' : 'starting';
            const invalid = command.handoff_id
              ? current?.handoff?.id !== command.handoff_id || current.handoff.phase !== phase
              : current?.owner !== this.id || !!this.recoveryFence;
            const expired = this.operation.signal.aborted || this.leaseLost || invalid || command.expires_at <= Date.now() / 1000;
            clearTimeout(timeout);
            this.ctl.signal.removeEventListener('abort', abort);
            this.operation = null;
            if (expired) throw new Error('Время команды истекло или передача отменена');
          }
        } catch (e) {
          if (this.ctl.signal.aborted) return;
          if (command.kind === 'start' || command.kind === 'prepare' || command.kind === 'play' || command.kind === 'next' || command.kind === 'prev') this.adapter.pause();
          ack = { device_id: this.id, command_id: command.id, ok: false, error: errorMessage(e) };
          update({ error: ack.error });
          if (command.kind === 'prepare' || command.kind === 'start') this.prepared = null;
        }
        if (command.kind === 'prepare' && ack.ok && this.prepared) {
          // Ready audio can wait through the source's 10s stop and target's 25s start phase.
          this.prepared.deadline = Date.now() / 1000 + 35;
        }
        this.completed.set(command.id, ack);
        if (this.completed.size > 64) this.completed.delete(this.completed.keys().next().value!);
        this.pendingAck = ack;
      }
      view = (await this.acknowledge())!;
    }
  }

  private async heartbeat() {
    if (this.inFlight || this.ctl.signal.aborted) return;
    this.inFlight = true;
    try {
      // Never execute a command twice when its acknowledgement response was lost.
      await this.acknowledge();
      let snapshot: PlaybackSnapshot | null = null;
      const view = state.view;
      const transferring = !!view?.handoff && ['preparing', 'stopping', 'starting'].includes(view.handoff.phase);
      if (view?.owner === this.id && !transferring) {
        try { snapshot = this.adapter.snapshot(); }
        catch (e) { update({ error: errorMessage(e) }); }
      }
      const received = await this.send('heartbeat', {
        device_id: this.id, name: 'Браузер', kind: 'web', state: snapshot, revision: view?.revision ?? null,
      });
      this.lastHeartbeat = performance.now();
      this.leaseLost = false;
      await this.commands(received);
      if (this.attachAfterHeartbeat) {
        this.attachAfterHeartbeat = false;
        const fresh = state.view!;
        try {
          if (fresh.owner && fresh.owner !== this.id) {
            await this.commands(await this.send('handoff', { device_id: this.id, target: this.id, revision: fresh.revision }));
          } else {
            const local = this.adapter.snapshot();
            if (!local) throw new Error('Сначала выберите трек в этом браузере.');
            await this.commands(await this.send('claim', { device_id: this.id, state: local, revision: fresh.revision }));
          }
        } catch (e) {
          if (!this.ctl.signal.aborted) update({ error: errorMessage(e) });
        }
      }
    } catch (e) {
      if (!this.ctl.signal.aborted) update({ connected: false, error: errorMessage(e) });
    } finally {
      this.nextHeartbeat = performance.now() + 2000;
      this.finishRequest();
    }
  }

  private finishRequest() {
    this.inFlight = false;
    const queued = this.queuedAction;
    this.queuedAction = null;
    if (!this.ctl.signal.aborted) {
      update({ busy: false });
      queued?.();
    }
  }

  async action(kind: 'claim' | 'handoff' | 'command', target?: string, control?: PlaybackControl, value?: number,
    revision = state.view?.revision) {
    if (this.ctl.signal.aborted || !state.view) {
      update({ error: 'Нет подключения к общему сеансу.' });
      return;
    }
    if (this.inFlight) {
      if (this.queuedAction || this.operation) {
        update({ error: 'Предыдущая команда ещё выполняется.' });
      } else {
        // A regular heartbeat must not eat a tap on an enabled remote button.
        // Keep its observed revision: do not retarget a delayed seek to a new song.
        this.queuedAction = () => { void this.action(kind, target, control, value, revision); };
        update({ busy: true, error: null });
      }
      return;
    }
    this.inFlight = true;
    update({ busy: true, error: null });
    try {
      await this.acknowledge();
      const expectedRevision = revision ?? state.view!.revision;
      let body: unknown;
      if (kind === 'claim') {
        const snapshot = this.adapter.snapshot();
        if (!snapshot) throw new Error('Сначала выберите трек в этом браузере.');
        body = { device_id: this.id, revision: expectedRevision, state: snapshot };
      } else if (kind === 'handoff') {
        body = { device_id: this.id, target, revision: expectedRevision };
      } else body = { device_id: this.id, target, revision: expectedRevision, kind: control, ...(value === undefined ? {} : { value }) };
      await this.commands(await this.send(kind, body));
    } catch (e) {
      if (!this.ctl.signal.aborted) update({ error: errorMessage(e) });
    } finally {
      this.finishRequest();
    }
  }

  localAllowed() {
    const view = state.view;
    if (this.recoveryFence) {
      update({ error: 'Сеанс восстанавливался после потери связи. Подтвердите продолжение здесь или выберите локальный режим.' });
      return false;
    }
    if (this.leaseLost && this.published) {
      update({ error: 'Нет связи с сервером. Дождитесь подключения перед продолжением.' });
      return false;
    }
    if (this.prepared || (view?.handoff && ['preparing', 'stopping', 'starting'].includes(view.handoff.phase)) || (view?.owner && view.owner !== this.id)) {
      update({ error: 'Воспроизведение на другом устройстве или передача активны. Нажмите «Слушать здесь» в панели устройств.' });
      return false;
    }
    return true;
  }
}

export function startSharedPlayback(adapter: PlaybackAdapter, account: number) {
  runtime?.close();
  runtime = null;
  activeAdapter = adapter;
  activeAccount = account;
  let localOnly = false;
  try { localOnly = localStorage.getItem(`hm-playback-local-only:${account}`) === 'true'; } catch { /* Optional preference. */ }
  if (localOnly) update({ view: null, enabled: true, localOnly: true, busy: false, connected: false, error: null });
  else runtime = new Runtime(adapter);
}
export function stopSharedPlayback() {
  runtime?.close();
  runtime = null;
  activeAdapter = null;
  activeAccount = null;
  update({ view: null, enabled: false, localOnly: false, connected: false, busy: false, error: null });
}

/** An explicit local escape is available even when the Hub is unreachable. */
export function detachPlayback() {
  runtime?.close();
  runtime = null;
  activeAdapter?.pause();
  if (activeAccount !== null) {
    try { localStorage.setItem(`hm-playback-local-only:${activeAccount}`, 'true'); } catch { /* Optional preference. */ }
  }
  update({ view: null, enabled: true, localOnly: true, connected: false, busy: false, error: null });
}

export function attachPlayback() {
  if (!activeAdapter || activeAccount === null) return;
  runtime?.close();
  activeAdapter.pause();
  try { localStorage.removeItem(`hm-playback-local-only:${activeAccount}`); } catch { /* Optional preference. */ }
  runtime = new Runtime(activeAdapter, true);
}
export function localPlaybackAllowed() {
  return runtime?.localAllowed() ?? true;
}
export function claimPlayback() {
  return runtime?.action('claim');
}
export function controlPlayback(kind: PlaybackControl, value?: number) {
  return runtime?.action('command', state.view?.owner ?? undefined, kind, value);
}
export function handoffPlayback(target: string) {
  return runtime?.action('handoff', target);
}
export async function unlockPlayback() {
  if (!activeAdapter) return;
  if (state.busy || (state.view?.handoff && ['preparing', 'stopping', 'starting'].includes(state.view.handoff.phase))) {
    update({ error: 'Дождитесь окончания передачи, затем разрешите звук.' });
    return;
  }
  try { await activeAdapter.unlock(); update({ error: null }); }
  catch (e) { update({ error: errorMessage(e) }); }
}
