import { useState } from 'react';
import { isWaveGenre, waveGenreIds, waveGenres, waveGenreTitle, type WaveGenreSeed } from '../api/waveGenres';
import { useListen } from '../lib/listen';
import { seedKey, useWave } from '../lib/wave';

/** A choice belongs to this start control, not to the playing session or the user's taste. */
export function useWaveGenre() {
  const { id, title, genreSeed, starting } = useWave();
  const wave = useListen((s) => !!s.track && s.context === `wave:${id}`);
  const [draft, select] = useState<WaveGenreSeed | null>();
  const seed = draft === undefined ? (wave ? genreSeed : null) : draft;
  const key = seedKey(seed);
  const active = wave && (seed ? key === seedKey(genreSeed) : !genreSeed && title === waveGenreTitle(null));
  return { seed, select, label: waveGenreTitle(seed), active, busy: starting === key, disabled: starting !== null };
}

interface GenreSelectProps {
  seed: WaveGenreSeed | null;
  select: (seed: WaveGenreSeed | null) => void;
  disabled: boolean;
}

export function WaveGenreSelect({ seed, select, disabled }: GenreSelectProps) {
  const selectClass = 'min-h-11 rounded-lg border border-line bg-surface px-3 text-fg';
  return (
    <fieldset disabled={disabled} className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm font-medium">
      <legend className="sr-only">Жанры волны</legend>
      <label className="inline-flex items-center gap-2">Жанр волны
        <select className={selectClass} value={seed?.genre ?? ''} onChange={(e) => {
          const genre = e.currentTarget.value;
          if (!isWaveGenre(genre)) select(null);
          else if (seed?.second_genre && genre !== seed.second_genre) select({ ...seed, genre });
          else select({ genre });
        }}>
          <option value="">Без ограничения</option>
          {waveGenreIds.map((g) => <option key={g} value={g}>{waveGenres[g]}</option>)}
        </select>
      </label>
      {seed && <label className="inline-flex items-center gap-2">Второй жанр
        <select className={selectClass} value={seed.second_genre ?? ''} onChange={(e) => {
          const second = e.currentTarget.value;
          select(isWaveGenre(second) && second !== seed.genre
            ? { genre: seed.genre, second_genre: second, first_percent: seed.first_percent ?? 50 }
            : { genre: seed.genre });
        }}>
          <option value="">Без второго жанра</option>
          {waveGenreIds.filter(g => g !== seed.genre).map((g) => <option key={g} value={g}>{waveGenres[g]}</option>)}
        </select>
      </label>}
      {seed?.second_genre && <label className="flex w-full flex-col items-center gap-1">
        <span>{waveGenres[seed.genre]} {seed.first_percent}% · {waveGenres[seed.second_genre]} {100 - seed.first_percent}%</span>
        <input type="range" min={1} max={99} step={1} value={seed.first_percent}
          aria-label={`Доля первого жанра: ${waveGenres[seed.genre]}`} aria-valuetext={`${seed.first_percent}%`}
          className="min-h-8 w-full max-w-80 accent-accent"
          onChange={(e) => select({ ...seed, first_percent: e.currentTarget.valueAsNumber })} />
      </label>}
    </fieldset>
  );
}
