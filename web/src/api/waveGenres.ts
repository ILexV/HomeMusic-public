export const waveGenres = {
  classical: 'Классика', pop: 'Поп', rock: 'Рок', jazz: 'Джаз', electronic: 'Электроника', hiphop: 'Хип-хоп',
} as const;

export type WaveGenre = keyof typeof waveGenres;
