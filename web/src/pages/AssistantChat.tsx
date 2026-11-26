// Free chat with the assistant (stage 3.8): POST /api/assistant/chat with the whole visible history.
// The assistant changes ♥, playlists and downloads itself (`done`); deleting asks for a tap here.
// The Hub saves every turn into a chat (`/api/assistant/chats`): the open chat lives in the route (`?chat=<id>`)
// and in a store here, so a reply that comes after leaving the screen is not lost.
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { api, errorMessage } from '../api/client';
import type { ChatAction, ChatMessage, SavedChatMessage, TrackOut } from '../api/types';
import { TrackRow } from '../components/catalog';
import { Icon } from '../components/Icon';
import { Button, ErrorState, IconButton, Loading, Spinner, cx } from '../components/ui';
import { providerLabel, tracksWord } from '../lib/format';
import { navigate, parseHash } from '../lib/router';
import { attempt, toast } from '../lib/ui';
import { holdAutoUpdate } from '../lib/update';

const MAX_IMAGES = 15;
const MAX_IMAGE_MIB = 15;
const MAX_MESSAGE_CHARS = 5000;

function acceptedImages(files: File[], slots: number): File[] {
  const images = files.filter(f => f.type.startsWith('image/'));
  const valid = images.filter(f => f.size <= MAX_IMAGE_MIB * 1024 * 1024);
  if (valid.length !== images.length) toast(`Фото больше ${MAX_IMAGE_MIB} МиБ не добавлены.`, 'error');
  if (valid.length <= slots) return valid;
  toast(`В одном сообщении не больше ${MAX_IMAGES} фотографий. Лишние файлы не добавлены.`, 'error');
  return valid.slice(0, slots);
}

interface Proposal {
  taskId: number;
  token: string;
  summary: string;
  actions: ChatAction[];
  state: 'pending' | 'busy' | 'confirmed' | 'dismissed';
}

interface Entry {
  id: number;
  role: ChatMessage['role'];
  content: string;
  cost?: number;
  error?: boolean;
  /** what the assistant changed this turn */
  done?: string[];
  tracks?: TrackOut[];
  proposal?: Proposal;
  /** screenshots sent from this screen (object URLs) */
  images?: string[];
  /** screenshots of a saved message: the Hub keeps only their count */
  imageCount?: number;
}

// --- the open chat --------------------------------------------------------------------------------

interface ChatState {
  /** saved chat id; null — a new chat, saved by its first reply */
  id: number | null;
  entries: Entry[];
  loading: boolean;
  error?: string;
  sending: boolean;
}

let currentKey: string | null = null;
let draft: { text: string; images: File[] } | null = null;
let state: ChatState = { id: null, entries: [], loading: false, sending: false };
// bumps when another chat opens: a reply to the previous one then only stays on the Hub
let gen = 0;
let seq = 0;
const listeners = new Set<() => void>();

function update(patch: Partial<ChatState> | ((s: ChatState) => Partial<ChatState>)) {
  state = { ...state, ...(typeof patch === 'function' ? patch(state) : patch) };
  listeners.forEach((l) => l());
}

function remember(id: number | null) {
  if (currentKey === null) return;
  try {
    if (id) sessionStorage.setItem(currentKey, String(id));
    else sessionStorage.removeItem(currentKey);
  } catch {
    /* storage unavailable: the route still holds the chat */
  }
}

/** The chat open last in this tab (the assistant's tab returns to it). */
export function currentChatId(): number | null {
  if (state.id) return state.id;
  if (currentKey === null) return null;
  try {
    return Number(sessionStorage.getItem(currentKey)) || null;
  } catch {
    return null;
  }
}

const fromSaved = (m: SavedChatMessage): Entry => ({
  id: ++seq,
  role: m.role,
  content: m.content,
  cost: m.cost,
  done: m.done,
  tracks: m.tracks,
  imageCount: m.images,
  proposal: m.proposal && {
    taskId: m.proposal.task_id,
    token: m.proposal.token,
    summary: m.proposal.summary,
    actions: m.proposal.actions,
    state: m.proposal.state,
  },
});

function selectChat(id: number | null) {
  const generation = ++gen;
  for (const entry of state.entries) entry.images?.forEach(url => URL.revokeObjectURL(url));
  draft = null;
  remember(id);
  update({ id, entries: [], loading: id !== null, error: undefined, sending: false });
  return generation;
}

function openChat(id: number) {
  if (state.id === id && !state.error) return;
  const g = selectChat(id);
  api.savedChat(id).then(
    (c) => g === gen && update({ entries: c.messages.map(fromSaved), loading: false }),
    (e) => g === gen && update({ loading: false, error: errorMessage(e) }),
  );
}

/** Starts a new chat; the previous one stays in the history. */
export function newChat() {
  selectChat(null);
}

/** Forgets a chat deleted from the history if it is the open one. */
export function forgetChat(id: number) {
  if (currentChatId() === id) newChat();
}

export function setChatAccount(account: number | null) {
  const key = account === null ? null : `hm.chat.current:${account}`;
  if (key === currentKey) return;
  newChat();
  currentKey = key;
}

/** Opens a new chat with a message ready to send (Android "Share" → HomeMusic). */
export function startChatWith(text: string, images: File[] = []) {
  newChat();
  const accepted = acceptedImages(images, MAX_IMAGES);
  draft = { text: text || (accepted.length ? 'Найди и скачай треки с этих скриншотов' : ''), images: accepted };
  navigate('/assistant', { replace: true });
  return accepted.length > 0;
}

// --------------------------------------------------------------------------------------------------

const fmtCost = (c?: number) => (c ? `$${c < 0.01 ? c.toFixed(4) : c.toFixed(3)}` : '');

function describe(a: ChatAction): string {
  switch (a.type) {
    case 'delete_playlist':
      return `Удалить плейлист «${a.name || a.playlist_id}»`;
    case 'delete_library_tracks': {
      const more = a.titles.length > 5 ? ` и ещё ${a.titles.length - 5}` : '';
      return `Удалить из библиотеки ${tracksWord(a.titles.length)}: ${a.titles.slice(0, 5).join('; ')}${more}`;
    }
    default:
      return JSON.stringify(a);
  }
}

/** Preserve the displayed order and exact refs for follow-ups like «скачай второй». */
const historyContent = (e: Entry) => [
  e.content,
  e.done?.length && `[Сделано: ${e.done.join('; ')}]`,
  e.tracks?.length && `[Найдено: ${e.tracks.map(t => `${t.ref} ${t.artist} — ${t.title}`).join('; ')}]`,
].filter(Boolean).join('\n\n');

const shotsWord = (n: number) => (n === 1 ? 'Скриншот' : `Скриншоты: ${n}`);

async function send(content: string, attached: File[]) {
  const g = gen;
  const imageUrls = attached.map((f) => URL.createObjectURL(f));
  const user: Entry = { id: ++seq, role: 'user', content, images: imageUrls };
  const history: ChatMessage[] = [...state.entries.filter((e) => !e.error), user].map((e) => ({ role: e.role, content: historyContent(e) }));
  update((s) => ({ entries: [...s.entries, user], sending: true }));
  const releaseUpdate = holdAutoUpdate();
  try {
    const r = await api.chat(history, state.id, attached);
    if (g !== gen) return;
    if (!state.id) {
      remember(r.chat_id);
      update({ id: r.chat_id });
      // the new chat gets its address, unless the user has gone elsewhere meanwhile
      if (parseHash(window.location.hash).path === '/assistant') navigate(`/assistant?chat=${r.chat_id}`, { replace: true });
    }
    const actions = r.actions ?? [];
    const proposal: Proposal | undefined =
      actions.length && r.task_id && r.confirm_token
        ? { taskId: r.task_id, token: r.confirm_token, summary: r.summary ?? '', actions, state: 'pending' }
        : undefined;
    update((s) => ({ entries: [...s.entries, { id: ++seq, role: 'assistant', content: r.reply || '', cost: r.cost_usd, done: r.done ?? [], tracks: r.tracks, proposal }] }));
  } catch (e) {
    if (g === gen) update((s) => ({ entries: [...s.entries, { id: ++seq, role: 'assistant', content: errorMessage(e), error: true }] }));
  } finally {
    releaseUpdate();
    if (g === gen) update({ sending: false });
  }
}

const setProposal = (id: number, st: Proposal['state']) =>
  update((s) => ({ entries: s.entries.map((e) => (e.id === id && e.proposal ? { ...e, proposal: { ...e.proposal, state: st } } : e)) }));

export function AssistantChat({ chatId }: { chatId: number | null }) {
  const chat = useSyncExternalStore(
    (l) => (listeners.add(l), () => listeners.delete(l)),
    () => state,
  );
  useEffect(() => {
    if (chatId) openChat(chatId);
  }, [chatId]);
  if (chatId && chat.error) return <ErrorState text={chat.error} onRetry={() => openChat(chatId)} />;
  if (chatId && (chat.loading || chat.id !== chatId)) return <Loading />;
  // The first reply assigns a server ID without starting another conversation.
  return <Conversation key={gen} {...chat} />;
}

function Conversation({ id, entries, loading, error, sending }: ChatState) {
  const [text, setText] = useState('');
  const [images, setImages] = useState<File[]>([]);
  const textChars = Array.from(text).length;
  const textTooLong = textChars > MAX_MESSAGE_CHARS;
  const imagePreviews = useMemo(() => images.map((f) => URL.createObjectURL(f)), [images]);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!draft) return;
    setText(draft.text);
    setImages(draft.images);
    draft = null;
  }, []);

  const addImages = (files: File[]) => {
    const valid = acceptedImages(files, MAX_IMAGES - images.length);
    if (valid.length) setImages((prev) => [...prev, ...valid].slice(0, MAX_IMAGES));
  };

  useEffect(() => {
    const paste = (e: ClipboardEvent) => {
      const files = Array.from(e.clipboardData?.files ?? []).filter((f) => f.type.startsWith('image/'));
      if (files.length) { e.preventDefault(); addImages(files); }
    };
    window.addEventListener('paste', paste);
    return () => window.removeEventListener('paste', paste);
  }, [images.length]);

  useEffect(() => () => imagePreviews.forEach((url) => URL.revokeObjectURL(url)), [imagePreviews]);

  // Keep the latest message visible above the fixed input bar.
  useEffect(() => {
    if (entries.length || sending) window.scrollTo({ top: document.documentElement.scrollHeight, behavior: entries.length > 2 && !sending ? 'auto' : 'smooth' });
  }, [entries.length, sending]);

  const submit = () => {
    const content = text.trim();
    if ((!content && !images.length) || sending || loading || error) return;
    if (textTooLong) {
      toast(`В одном сообщении не больше ${MAX_MESSAGE_CHARS} символов`, 'error');
      return;
    }
    setText('');
    setImages([]);
    if (inputRef.current) inputRef.current.style.height = '';
    void send(content, images);
  };

  const confirm = async (e: Entry) => {
    if (!e.proposal) return;
    setProposal(e.id, 'busy');
    const r = await attempt(() => api.chatConfirm(e.proposal!.taskId, e.proposal!.token, id), 'Готово');
    setProposal(e.id, r ? 'confirmed' : 'pending');
  };

  const dismiss = (e: Entry) => {
    if (!e.proposal) return;
    setProposal(e.id, 'dismissed');
    void api.chatDismiss(e.proposal.taskId, id).catch(() => undefined);
  };

  const totalCost = entries.reduce((s, e) => s + (e.cost ?? 0), 0);

  return (
    <div className={images.length ? 'pb-52' : 'pb-32'}>
      {entries.length === 0 ? (
        <div className="px-6 py-10 text-center text-sm text-muted">
          <Icon name="sparkle" size={32} className="mx-auto mb-3 text-accent" />
          <p className="mb-4">Спросите что угодно о музыке и своём аккаунте или пришлите скриншот плейлиста. Ассистент сам ставит ♥, собирает плейлисты и качает музыку, а удалить что-то — только после вашего подтверждения.</p>
          <div className="flex flex-col items-center gap-2">
            {['Найди концертные версии «Группы крови» в интернете', 'Скачай последний альбом Земфиры', 'Что пришло из Яндекс Музыки?'].map((s) => (
              <button key={s} type="button" className="rounded-full bg-raised px-4 py-2 text-fg hover:bg-hover" onClick={() => setText(s)}>
                {s}
              </button>
            ))}
          </div>
        </div>
      ) : (
        totalCost > 0 && <div className="px-4 pb-1 text-xs text-dim">Стоимость диалога {fmtCost(totalCost)}</div>
      )}

      <ol className="space-y-3 px-4" aria-live="polite">
        {entries.map((e) => (
          <li key={e.id} className={cx('flex flex-col', e.role === 'user' ? 'items-end' : 'items-start')}>
            {!!e.images?.length && <div className="mb-1 flex max-w-[85%] flex-wrap justify-end gap-1">{e.images.map((src, i) => <img key={i} src={src} alt="Скриншот" className="max-h-40 max-w-40 rounded-xl object-contain" />)}</div>}
            {!e.images?.length && !!e.imageCount && (
              <span className="mb-1 inline-flex items-center gap-1.5 rounded-full bg-raised px-3 py-1 text-xs text-muted">
                <Icon name="image" size={14} /> {shotsWord(e.imageCount)}
              </span>
            )}
            {e.content && (
              <div
                className={cx(
                  'max-w-[85%] whitespace-pre-wrap break-words rounded-2xl px-4 py-2.5 text-[15px]',
                  e.role === 'user' ? 'rounded-br-md bg-accent text-on-accent' : e.error ? 'rounded-bl-md bg-danger/15 text-danger' : 'rounded-bl-md bg-surface',
                )}
              >
                {e.content}
              </div>
            )}
            {!!e.tracks?.length && (
              <ol aria-label="Найденные песни" className="mt-2 w-full divide-y divide-line rounded-2xl border border-line bg-surface">
                {e.tracks.map((t, i) => (
                  <li key={t.ref}>
                    {t.source === 'file' && <div className="px-4 pt-2 text-xs text-muted">{providerLabel(t.ref.split(':', 3)[1])}</div>}
                    <TrackRow track={t} number={i + 1} showAlbum link={false} queue={e.tracks} />
                  </li>
                ))}
              </ol>
            )}
            {!!e.done?.length && (
              <ul className="mt-1.5 max-w-[85%] space-y-1 px-1" aria-label="Сделано">
                {e.done.map((d, i) => (
                  <li key={i} className="flex items-start gap-1.5 text-sm text-muted">
                    <Icon name="check" size={16} stroke={3} className="mt-0.5 shrink-0 text-accent" />
                    <span>{d}</span>
                  </li>
                ))}
              </ul>
            )}
            {e.proposal && (
              <div className="mt-2 w-full max-w-[92%] rounded-2xl border border-line bg-surface p-4">
                <div className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">Нужно подтверждение</div>
                <ul className="space-y-1.5">
                  {e.proposal.actions.map((a, i) => (
                    <li key={i} className="flex items-start gap-2 text-sm">
                      <Icon name="trash" size={18} className="mt-0.5 shrink-0 text-danger" />
                      <span>{describe(a)}</span>
                    </li>
                  ))}
                </ul>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  {e.proposal.state === 'confirmed' ? (
                    <span className="inline-flex items-center gap-1 text-sm text-accent">
                      <Icon name="check" size={16} stroke={3} /> Удалено
                    </span>
                  ) : e.proposal.state === 'dismissed' ? (
                    <span className="text-sm text-dim">Отменено</span>
                  ) : (
                    <>
                      <Button small variant="danger" icon="trash" busy={e.proposal.state === 'busy'} onClick={() => confirm(e)}>
                        Удалить
                      </Button>
                      <Button small variant="ghost" disabled={e.proposal.state === 'busy'} onClick={() => dismiss(e)}>
                        Отмена
                      </Button>
                    </>
                  )}
                </div>
              </div>
            )}
            {e.cost ? <span className="mt-1 px-1 text-[11px] text-dim">{fmtCost(e.cost)}</span> : null}
          </li>
        ))}
        {sending && (
          <li className="flex items-center gap-2 text-sm text-muted">
            <Spinner size={16} /> Думаю…
          </li>
        )}
      </ol>

      <form
        className="fixed inset-x-0 bottom-[calc(var(--tabbar-h)+var(--player-h,0px)+env(safe-area-inset-bottom))] z-30 mx-auto flex max-w-2xl items-end gap-2 border-t border-line bg-bg/95 px-3 py-2 backdrop-blur"
        onSubmit={(ev) => {
          ev.preventDefault();
          submit();
        }}
      >
        <input ref={fileRef} type="file" accept="image/*" multiple className="hidden" onChange={(e) => { addImages(Array.from(e.target.files ?? [])); e.target.value = ''; }} />
        <div className="flex min-w-0 flex-1 flex-col rounded-3xl border border-line bg-raised transition-colors focus-within:border-accent">
          {!!images.length && (
            <ul className="flex gap-2 overflow-x-auto px-2 pt-2" aria-label="Скриншоты к сообщению" data-hold-update="">
              {images.map((f, i) => (
                <li key={`${f.name}-${i}`} className="relative shrink-0">
                  <img src={imagePreviews[i]} alt={f.name || 'Скриншот'} className="h-16 w-16 rounded-2xl object-cover" />
                  <button
                    type="button"
                    aria-label="Убрать скриншот"
                    className="absolute right-1 top-1 inline-flex h-6 w-6 items-center justify-center rounded-full bg-black/65 text-white backdrop-blur transition hover:bg-black/80 active:scale-95"
                    onClick={() => setImages((prev) => prev.filter((_, n) => n !== i))}
                  >
                    <Icon name="x" size={14} />
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div className="flex items-end">
            <IconButton
              icon="image"
              size={20}
              label="Прикрепить скриншот"
              type="button"
              className="m-px !h-10 !w-10 !text-muted hover:!text-fg"
              onClick={() => fileRef.current?.click()}
              disabled={sending || images.length >= MAX_IMAGES}
            />
            <textarea
              ref={inputRef}
              className="max-h-32 min-h-[42px] flex-1 resize-none bg-transparent py-[9px] pr-4 text-fg outline-none placeholder:text-dim"
              rows={1}
              placeholder="Сообщение ассистенту"
              aria-label="Сообщение ассистенту"
              aria-describedby="assistant-message-limits"
              aria-invalid={textTooLong || undefined}
              value={text}
              onChange={(ev) => {
                setText(ev.target.value);
                ev.target.style.height = 'auto';
                ev.target.style.height = `${Math.min(ev.target.scrollHeight, 128)}px`;
              }}
              onKeyDown={(ev) => {
                if (ev.key === 'Enter' && !ev.shiftKey && !('ontouchstart' in window)) {
                  ev.preventDefault();
                  submit();
                }
              }}
            />
          </div>
          <div id="assistant-message-limits" className="flex justify-between gap-2 px-3 pb-2 text-[11px] text-dim">
            <span>{images.length ? `${images.length} / ${MAX_IMAGES} фото` : `До ${MAX_IMAGES} фото`}</span>
            <span className={textTooLong ? 'text-danger' : undefined}>{textChars} / {MAX_MESSAGE_CHARS}</span>
          </div>
          {textTooLong && <p className="px-3 pb-2 text-xs text-danger">Сократите сообщение до {MAX_MESSAGE_CHARS} символов</p>}
        </div>
        <IconButton icon="send" label="Отправить" type="submit" busy={sending} disabled={textTooLong || (!text.trim() && !images.length)} className="bg-accent !text-on-accent hover:!bg-accent-strong" />
      </form>
    </div>
  );
}
