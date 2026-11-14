import { toggle, useListen } from '../lib/listen';
import { canWave, startWave, useWave } from '../lib/wave';
import { Icon } from './Icon';
import { Spinner } from './ui';
import { WaveGlow } from './WaveGlow';
import { WavePreferenceControls } from './WavePreferences';

/** «Моя волна» as the hero of «Главная» on a computer (`useDesktop()`): no card, a living soft blob glows right on the
 * page background (a canvas wider than the column, its edges fade out); the title, the state line and the round play
 * button sit on top of it. The blob's colors come from the current track's cover; it is bigger while the wave plays and
 * shrinks (slowly) on pause. Everywhere else `WaveCard` is used instead. */
export function WaveHero() {
  const { title, starting } = useWave();
  const wave = useListen((s) => !!s.track && s.context.startsWith('wave:'));
  const phase = useListen((s) => s.phase);
  const cover = useListen((s) => s.track?.cover_small_url ?? s.track?.cover_url ?? null);
  if (!canWave()) return null;
  const busy = starting === '';
  const on = wave && title === 'Моя волна';
  const playing = on && phase === 'playing';
  // wanted to play (loading the next track counts): the button then offers a pause
  const running = on && (playing || phase === 'loading' || phase === 'resolving');
  // The mask fades the canvas out toward its edges, whatever the blob does there.
  const fade = 'radial-gradient(closest-side, #000 72%, transparent 100%)';
  const caption = busy ? 'busy' : running ? 'playing' : on ? 'paused' : 'idle';
  const captions: Array<[string, string]> = [
    ['idle', 'Бесконечный поток под ваш вкус'],
    ['busy', 'Подбираем первые треки…'],
    ['playing', 'Играет — подстраивается под каждый пропуск и ♥'],
    ['paused', 'На паузе — продолжим с того же места'],
  ];
  const label = busy ? 'Моя волна: запускаем' : running ? 'Моя волна: пауза' : on ? 'Моя волна: продолжить' : 'Моя волна: слушать';

  return (
    <>
    <div className="relative mt-1 flex h-[360px] items-center justify-center">
      <WaveGlow
        playing={playing}
        cover={cover}
        center={[0.5, 0.5]}
        zoom={0.9}
        className="pointer-events-none absolute inset-y-0 left-1/2 w-[min(1100px,calc(100vw-32px))] -translate-x-1/2"
        style={{ maskImage: fade, WebkitMaskImage: fade }}
      />
      <div className="relative flex flex-col items-center text-center text-white" style={{ textShadow: '0 1px 14px rgba(0,0,0,.6)' }}>
        <h2 className="text-2xl font-bold">Моя волна</h2>
        {/* A fixed-size slot: the captions cross-fade in place, so neither the title nor the button ever moves. */}
        <div className="relative mt-1 h-10 w-80 max-w-full text-sm leading-snug text-white/80">
          {captions.map(([key, text]) => (
            <p key={key} className={`absolute inset-x-0 top-0 transition-opacity duration-500 ${key === caption ? 'opacity-100' : 'opacity-0'}`} aria-hidden={key !== caption}>
              {text}
            </p>
          ))}
        </div>
        <button
          type="button"
          onClick={() => (on ? toggle() : void startWave(null, 'home_card'))}
          disabled={starting !== null}
          aria-label={label}
          className="mt-3 inline-flex h-14 w-14 items-center justify-center rounded-full bg-fg text-bg shadow-lg transition hover:scale-105 active:scale-95 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-white disabled:opacity-90"
          style={{ textShadow: 'none' }}
        >
          {busy ? <Spinner size={24} /> : running ? <Icon name="pause" size={24} stroke={3} /> : <Icon name="play" size={24} stroke={2.4} className="translate-x-px" />}
        </button>
      </div>
    </div>
    <WavePreferenceControls />
    </>
  );
}
