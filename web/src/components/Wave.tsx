import { useRoute } from '../lib/router';
import { canWave, startWave } from '../lib/wave';
import { Icon } from './Icon';
import { Spinner } from './ui';
import { useWaveGenre, WaveGenreSelect } from './WaveGenre';
import { WavePreferenceControls } from './WavePreferences';

/** «Моя волна» in the catalog, next to «Микс дня»: a tap starts the user's own endless stream. */
export function WaveCard() {
  const choice = useWaveGenre();
  const { path } = useRoute();
  if (!canWave()) return null;
  const { busy, active: on, label } = choice;

  return (
    <>
    <div className="mx-4 mt-3"><WaveGenreSelect {...choice} /></div>
    <button
      type="button"
      onClick={() => void startWave(choice.seed, path.startsWith('/home') ? 'home_card' : 'catalog_card')}
      disabled={choice.disabled}
      aria-label={`${label}: слушать`}
      className="mx-4 mt-1 flex w-[calc(100%-2rem)] items-center gap-3 rounded-xl bg-raised p-3 text-left transition hover:bg-hover disabled:opacity-80"
    >
      <span className="flex h-14 w-14 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-accent to-accent/50 text-on-accent">
        {busy ? <Spinner size={24} /> : <Icon name="wave" size={30} />}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block font-bold">{label}</span>
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
