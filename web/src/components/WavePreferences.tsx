import { useEffect, useState } from 'react';
import type { WavePreferences as Preferences } from '../api/types';
import { DEFAULT_WAVE_PREFERENCES, setStartPreferences, updateWavePreferences, useWave } from '../lib/wave';
import { Button, Notice } from './ui';
import { useListen } from '../lib/listen';

export function WavePreferenceControls() {
  const wave = useWave();
  const context = useListen((s) => s.context);
  const active = !!wave.id && context === `wave:${wave.id}`;
  const preferences = active ? wave.preferences : wave.startPreferences;
  const [draft, setDraft] = useState(preferences);
  useEffect(() => setDraft(preferences), [active, wave.id, preferences.energy, preferences.vocals, preferences.discovery]);
  const change = (value: Preferences) => {
    setDraft(value);
    if (!active) setStartPreferences(value);
  };
  const disabled = (active && wave.updating) || wave.starting !== null;
  return (
    <details className="mx-4 mt-3 rounded-xl border border-line bg-surface p-3">
      <summary className="cursor-pointer text-sm font-semibold">Настроение волны{wave.updating ? ' · применяем…' : ''}</summary>
      <div className="mt-3 space-y-3">
        <p className="text-xs text-muted">{active ? 'Меняет только следующие треки. Текущая песня продолжит играть.' : 'Для следующего запуска. Новая волна по умолчанию нейтральна.'} Постоянные предпочтения не меняются.</p>
        <fieldset disabled={disabled} className="grid gap-3 sm:grid-cols-3">
          <label className="text-sm">Энергия
            <select className="mt-1 min-h-11 w-full rounded-lg bg-raised p-2" value={draft.energy} onChange={(e) => change({ ...draft, energy: e.target.value as Preferences['energy'] })}>
              <option value="any">Любая</option><option value="calm">Спокойно</option><option value="energetic">Энергично</option>
            </select>
          </label>
          <label className="text-sm">Вокал
            <select className="mt-1 min-h-11 w-full rounded-lg bg-raised p-2" value={draft.vocals} onChange={(e) => change({ ...draft, vocals: e.target.value as Preferences['vocals'] })}>
              <option value="any">Любой</option><option value="vocal">С вокалом</option><option value="instrumental">Без слов</option>
            </select>
          </label>
          <label className="text-sm">Открытия
            <select className="mt-1 min-h-11 w-full rounded-lg bg-raised p-2" value={draft.discovery} onChange={(e) => change({ ...draft, discovery: e.target.value as Preferences['discovery'] })}>
              <option value="balanced">Баланс</option><option value="familiar">Больше знакомого</option><option value="new">Больше нового</option>
            </select>
          </label>
        </fieldset>
        <div className="flex flex-wrap gap-2">
          {active && <Button small busy={wave.updating} disabled={disabled} onClick={() => updateWavePreferences(draft)}>Применить к следующим</Button>}
          <Button small disabled={disabled} onClick={() => change({ ...DEFAULT_WAVE_PREFERENCES })}>Нейтрально</Button>
        </div>
        {wave.preferencesStatus && <Notice kind="info">{wave.preferencesStatus}</Notice>}
        {active && wave.error && <Notice kind="error">{wave.error}. Настройки не применены — попробуйте ещё раз.</Notice>}
        {wave.preparation && <p className="text-xs text-muted" role="status">Полные записи: готовы {wave.preparation.ready}, готовятся {wave.preparation.pending}, недоступны {wave.preparation.unavailable}.</p>}
      </div>
    </details>
  );
}
