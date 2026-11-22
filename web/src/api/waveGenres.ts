export const waveGenres = {
  classical: 'Классика', pop: 'Поп', rock: 'Рок', jazz: 'Джаз', electronic: 'Электроника', hiphop: 'Хип-хоп',
} as const;

export type WaveGenre = keyof typeof waveGenres;
export const waveGenreIds = Object.keys(waveGenres) as WaveGenre[];

export type WaveGenreSeed =
  | { genre: WaveGenre; second_genre?: never; first_percent?: never }
  | { genre: WaveGenre; second_genre: WaveGenre; first_percent: number };

export const isWaveGenre = (value: unknown): value is WaveGenre =>
  typeof value === 'string' && Object.hasOwn(waveGenres, value);

/** A saved choice or a seed received over the wire must still describe a playable combination. */
export function readWaveGenreSeed(seed?: { genre?: unknown; second_genre?: unknown; first_percent?: unknown } | null): WaveGenreSeed | null {
  if (!seed || !isWaveGenre(seed.genre)) return null;
  const { genre, second_genre, first_percent } = seed;
  if (second_genre == null && first_percent == null) return { genre };
  if (!isWaveGenre(second_genre) || second_genre === genre || typeof first_percent !== 'number' ||
      !Number.isInteger(first_percent) || first_percent < 1 || first_percent > 99) return null;
  return { genre, second_genre, first_percent };
}

export function waveGenreTitle(seed: WaveGenreSeed | null): string {
  if (!seed) return 'Моя волна';
  const first = waveGenres[seed.genre];
  return `Моя волна: ${seed.second_genre
    ? `${first} ${seed.first_percent}% + ${waveGenres[seed.second_genre]} ${100 - seed.first_percent}%`
    : first}`;
}
