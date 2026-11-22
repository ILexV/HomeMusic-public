import { useEffect, useState } from 'react';
import { Button, Empty, PageHeader, Spinner } from '../components/ui';
import { navigate } from '../lib/router';
import { clearShare, readShare } from '../lib/shareStore';
import { toast } from '../lib/ui';
import { startChatWith } from './AssistantChat';
import { isAudioFile, queueFilesForUpload } from './Upload';

type Phase = { kind: 'working'; text: string } | { kind: 'empty' } | { kind: 'error'; text: string };

// Guards against double processing (StrictMode effects, re-renders).
let running: Promise<void> | null = null;

/**
 * Android "Share" → HomeMusic. The service worker stored the shared payload in IndexedDB and redirected here:
 * images, text / link → a new assistant chat with them ready to send, audio → upload form.
 */
export function SharePage() {
  const [phase, setPhase] = useState<Phase>({ kind: 'working', text: 'Открываю то, чем вы поделились…' });

  useEffect(() => {
    if (running) return;
    running = (async () => {
      let payload;
      try {
        payload = await readShare();
      } catch {
        payload = null;
      }
      if (!payload) {
        setPhase({ kind: 'empty' });
        return;
      }
      await clearShare().catch(() => undefined);

      const images = payload.files.filter((f) => f.type.startsWith('image/'));
      const audio = payload.files.filter((f) => !f.type.startsWith('image/') && isAudioFile(f));
      const text = [payload.title, payload.text, payload.url].map((s) => (s ?? '').trim()).filter(Boolean).join(' ');

      if (audio.length) {
        queueFilesForUpload(audio);
      }
      if (images.length || (text && !audio.length)) {
        const attached = startChatWith(text, images);
        if (audio.length) {
          if (attached) toast('Скриншоты — в чате ассистента', 'info', { label: 'Открыть', to: '/assistant' });
          navigate('/upload', { replace: true });
        }
        return;
      }
      if (audio.length) {
        navigate('/upload', { replace: true });
        return;
      }
      setPhase({ kind: 'error', text: 'Этот тип файлов не поддерживается. Поделитесь скриншотом, аудиофайлом или ссылкой.' });
    })().finally(() => {
      running = null;
    });
  }, []);

  return (
    <div>
      <PageHeader title="Поделиться" back="/search" />
      {phase.kind === 'working' ? (
        <div className="flex flex-col items-center gap-4 px-8 py-16 text-center">
          <Spinner size={32} className="text-accent" />
          <p className="text-muted">{phase.text}</p>
        </div>
      ) : phase.kind === 'empty' ? (
        <Empty icon="sparkle" title="Нечего обрабатывать" text="Поделитесь скриншотом, аудиофайлом или ссылкой из другого приложения и выберите HomeMusic.">
          <Button small onClick={() => navigate('/assistant', { replace: true })}>
            К ассистенту
          </Button>
        </Empty>
      ) : (
        <Empty icon="alert" title="Не получилось" text={phase.text}>
          <Button small onClick={() => navigate('/assistant', { replace: true })}>
            К ассистенту
          </Button>
        </Empty>
      )}
    </div>
  );
}
