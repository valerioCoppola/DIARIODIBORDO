// Registratore: MediaRecorder con salvataggio incrementale ogni 5 secondi.

import { appendPiece, deleteRecording, isoWithOffset, newId, putRecording, updateRecording } from "./localstore";

const TIMESLICE_MS = 5000;
const VOICE_BITRATE = 48_000; // ~21 MB per ora: buona qualità per la voce

export type RecorderState = "fermo" | "registra" | "pausa";
export type StopReason = "utente" | "microfono_perso" | "errore" | "eliminata";

/** Formato preferito: Opus in WebM (Chrome/Android, Firefox), poi MP4/AAC (Safari/iOS). */
export function pickMimeType(): string {
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4;codecs=mp4a.40.2", "audio/mp4", "audio/ogg;codecs=opus"];
  for (const c of candidates) if (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(c)) return c;
  return "";
}

export function recordingSupported(): { ok: boolean; reason?: string } {
  if (typeof window === "undefined") return { ok: false };
  if (!window.isSecureContext) return { ok: false, reason: "Il microfono richiede una connessione HTTPS (o localhost)." };
  if (!navigator.mediaDevices?.getUserMedia) return { ok: false, reason: "Questo browser non permette di usare il microfono." };
  if (typeof MediaRecorder === "undefined") return { ok: false, reason: "Questo browser non supporta la registrazione audio." };
  return { ok: true };
}

type Listener = {
  onState: (s: RecorderState) => void;
  onLevel: (level: number) => void;
  onSaved: (bytes: number) => void;
  onStop: (recId: string, reason: StopReason) => void;
};

export class Recorder {
  private media?: MediaRecorder;
  private stream?: MediaStream;
  private audioCtx?: AudioContext;
  private raf = 0;
  private wakeLock: { release: () => Promise<void> } | null = null;
  private seq = 0;
  private saved = 0;
  private writes: Promise<void> = Promise.resolve();
  private elapsedBefore = 0;
  private segmentStart = 0;
  private stopReason: StopReason = "utente";
  recId: string | null = null;
  state: RecorderState = "fermo";

  constructor(private l: Listener) {}

  elapsedMs(): number {
    return this.elapsedBefore + (this.state === "registra" ? performance.now() - this.segmentStart : 0);
  }

  /** Inizia a registrare nel diario indicato (il vocale arriverà lì). */
  async start(diarioId?: string): Promise<string> {
    const mimeType = pickMimeType();
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    const id = newId();
    const startedAt = new Date();
    await putRecording({
      id, kind: "registrazione", diarioId, createdAt: startedAt.toISOString(), startedAt: isoWithOffset(startedAt),
      durationMs: 0, mimeType: mimeType || "audio/webm", filename: null, bytes: 0, pieces: 0,
      status: "in_registrazione",
    });
    this.recId = id;
    this.seq = 0;
    this.saved = 0;
    this.elapsedBefore = 0;

    this.media = new MediaRecorder(this.stream, { ...(mimeType ? { mimeType } : {}), audioBitsPerSecond: VOICE_BITRATE });
    const actualMime = this.media.mimeType || mimeType || "audio/webm";
    if (actualMime !== mimeType) await updateRecording(id, { mimeType: actualMime });

    this.media.ondataavailable = (ev) => {
      if (!ev.data || ev.data.size === 0) return;
      const seq = this.seq++;
      const blob = ev.data;
      // Scritture in fila: l'ordine dei pezzi è garantito.
      this.writes = this.writes.then(async () => {
        await appendPiece(id, seq, blob);
        this.saved += blob.size;
        await updateRecording(id, { durationMs: Math.round(this.elapsedMs()) });
        this.l.onSaved(this.saved);
      });
    };
    this.media.onerror = () => {
      this.stopReason = "errore";
      this.stop();
    };
    this.media.onstop = () => void this.finish();
    // Telefonata in arrivo, cuffie scollegate, permesso revocato: si chiude salvando.
    this.stream.getAudioTracks()[0].onended = () => {
      if (this.state !== "fermo") {
        this.stopReason = "microfono_perso";
        this.stop();
      }
    };

    this.media.start(TIMESLICE_MS);
    this.segmentStart = performance.now();
    this.setState("registra");
    this.meter();
    await this.acquireWakeLock();
    document.addEventListener("visibilitychange", this.onVisibility);
    return id;
  }

  pause() {
    if (this.media?.state !== "recording") return;
    this.media.pause();
    this.media.requestData();
    this.elapsedBefore += performance.now() - this.segmentStart;
    this.setState("pausa");
  }

  resume() {
    if (this.media?.state !== "paused") return;
    this.media.resume();
    this.segmentStart = performance.now();
    this.setState("registra");
  }

  /** Chiude la registrazione e la cancella dal telefono: non viene inviata al PC. */
  discard() {
    if (!this.media || this.media.state === "inactive") return;
    this.stopReason = "eliminata";
    this.media.stop();
  }

  stop() {
    if (!this.media || this.media.state === "inactive") return;
    if (this.state === "registra") this.elapsedBefore += performance.now() - this.segmentStart;
    this.media.stop(); // genera l'ultimo dataavailable, poi onstop
  }

  private async finish() {
    const id = this.recId!;
    await this.writes;
    if (this.stopReason === "eliminata") {
      await deleteRecording(id); // pezzi audio e voce dell'elenco
      this.recId = null;
      this.elapsedBefore = 0;
    } else {
      await updateRecording(id, { status: "da_inviare", durationMs: Math.round(this.elapsedBefore) });
    }
    this.cleanup();
    this.setState("fermo");
    this.l.onStop(id, this.stopReason);
    this.stopReason = "utente";
  }

  private cleanup() {
    cancelAnimationFrame(this.raf);
    this.stream?.getTracks().forEach((t) => t.stop());
    void this.audioCtx?.close();
    this.audioCtx = undefined;
    void this.wakeLock?.release().catch(() => {});
    this.wakeLock = null;
    document.removeEventListener("visibilitychange", this.onVisibility);
  }

  private setState(s: RecorderState) {
    this.state = s;
    this.l.onState(s);
  }

  private meter() {
    if (!this.stream) return;
    this.audioCtx = new AudioContext();
    const src = this.audioCtx.createMediaStreamSource(this.stream);
    const an = this.audioCtx.createAnalyser();
    an.fftSize = 1024;
    src.connect(an);
    const buf = new Float32Array(an.fftSize);
    const tick = () => {
      an.getFloatTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) sum += v * v;
      const rms = Math.sqrt(sum / buf.length);
      this.l.onLevel(this.state === "registra" ? Math.min(1, rms * 4) : 0);
      this.raf = requestAnimationFrame(tick);
    };
    tick();
  }

  // Lo schermo acceso evita che alcuni browser sospendano la pagina.
  private async acquireWakeLock() {
    try {
      const wl = (navigator as Navigator & { wakeLock?: { request: (t: "screen") => Promise<{ release: () => Promise<void> }> } }).wakeLock;
      this.wakeLock = (await wl?.request("screen")) ?? null;
    } catch {
      this.wakeLock = null;
    }
  }

  private onVisibility = () => {
    if (document.visibilityState === "visible" && this.state !== "fermo") void this.acquireWakeLock();
  };
}
