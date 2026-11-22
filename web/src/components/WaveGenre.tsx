import { useState } from 'react';
import { waveGenres, type WaveGenre } from '../api/waveGenres';
import { useListen } from '../lib/listen';
import { seedKey, useWave } from '../lib/wave';

const genres = Object.keys(waveGenres) as WaveGenre[];
const waveTitle = (genre: WaveGenre | '') => genre ? `Моя волна: ${waveGenres[genre]}` : 'Моя волна';

/** A choice belongs to this start control, not to the playing session or the user's taste. */
export function useWaveGenre() {
  const { title, starting } = useWave();
  const wave = useListen((s) => !!s.track && s.context.startsWith('wave:'));
  const [genre, select] = useState<WaveGenre | ''>(() => genres.find((g) => wave && waveTitle(g) === title) ?? '');
  const seed = genre ? { genre } : null;
  const label = waveTitle(genre);
  return { genre, select, label, seed, active: wave && title === label, busy: starting === seedKey(seed), disabled: starting !== null };
}

interface GenreSelectProps {
  genre: WaveGenre | '';
  select: (genre: WaveGenre | '') => void;
  disabled: boolean;
}

export function WaveGenreSelect({ genre, select, disabled }: GenreSelectProps) {
  return (
    <label className="inline-flex items-center gap-2 text-sm font-medium">Жанр волны
      <select className="min-h-11 rounded-lg border border-line bg-surface px-3 text-fg" value={genre} disabled={disabled}
        onChange={(e) => select(e.target.value as WaveGenre | '')}>
        <option value="">Без ограничения</option>
        {genres.map((g) => <option key={g} value={g}>{waveGenres[g]}</option>)}
      </select>
    </label>
  );
}
