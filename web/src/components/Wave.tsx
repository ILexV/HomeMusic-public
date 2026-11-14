import { useListen } from '../lib/listen';
import { useRoute } from '../lib/router';
import { canWave, startWave, useWave } from '../lib/wave';
import { Icon } from './Icon';
import { Spinner } from './ui';
import { WavePreferenceControls } from './WavePreferences';

/** «Моя волна» in the catalog, next to «Микс дня»: a tap starts the user's own endless stream. */
export function WaveCard() {
  const { title, starting } = useWave();
  const playing = useListen((s) => !!s.track && s.context.startsWith('wave:'));
  const { path } = useRoute();
  if (!canWave()) return null;
  const busy = starting === '';
  const on = playing && title === 'Моя волна';

  return (
    <>
    <button
      type="button"
      onClick={() => void startWave(null, path.startsWith('/home') ? 'home_card' : 'catalog_card')}
      disabled={starting !== null}
      aria-label="Моя волна: слушать"
      className="mx-4 mt-3 flex w-[calc(100%-2rem)] items-center gap-3 rounded-xl bg-raised p-3 text-left transition hover:bg-hover disabled:opacity-80"
    >
      <span className="flex h-14 w-14 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-accent to-accent/50 text-on-accent">
        {busy ? <Spinner size={24} /> : <Icon name="wave" size={30} />}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block font-bold">Моя волна</span>
        <span className="block text-[13px] leading-snug text-muted">
          {busy ? 'Подбираем первые треки…' : on ? 'Играет — подстраивается под каждый пропуск и ♥' : 'Бесконечный поток под ваш вкус'}
        </span>
      </span>
      {!busy && <Icon name={on ? 'refresh' : 'play'} size={22} className="shrink-0 text-accent" />}
    </button>
    <WavePreferenceControls />
    </>
  );
}
