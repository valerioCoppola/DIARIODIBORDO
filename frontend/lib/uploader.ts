// Invio riprendibile al PC: sessione con Idempotency-Key = id locale,
// blocchi da 4 MiB con impronta, ripresa dai soli blocchi mancanti.
// Ogni richiesta porta il diario del vocale (X-Diario).

import { api, ApiError } from "./api";
import { assemble, deletePieces, getRecording, updateRecording, type LocalRecording } from "./localstore";

const CHUNK = 4 * 1024 * 1024;
const MAX_RETRIES = 6;

type Session = {
  id: string;
  stato: string;
  numero_blocchi: number;
  dimensione_blocco: number;
  blocchi_mancanti: number[];
  recording_id: string | null;
  duplicato: boolean;
};

async function sha256Hex(data: Blob | ArrayBuffer): Promise<string> {
  const buf = data instanceof Blob ? await data.arrayBuffer() : data;
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Ripete in caso di rete assente o errori temporanei del server. */
async function withRetry<T>(fn: () => Promise<T>): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    try {
      return await fn();
    } catch (e) {
      const transient = !(e instanceof ApiError) || e.status >= 500 || e.status === 429 || e.status === 408;
      if (!transient || attempt >= MAX_RETRIES) throw e;
      if (!navigator.onLine) {
        await new Promise<void>((r) => window.addEventListener("online", () => r(), { once: true }));
      } else {
        await sleep(Math.min(30_000, 1000 * 2 ** attempt));
      }
    }
  }
}

const inFlight = new Set<string>();

export async function uploadRecording(recId: string, onChange: (r: LocalRecording) => void): Promise<void> {
  if (inFlight.has(recId)) return;
  inFlight.add(recId);
  try {
    let rec = await getRecording(recId);
    if (!rec || rec.status === "conservato" || rec.status === "in_registrazione") return;
    onChange((rec = await updateRecording(recId, { status: "in_invio", progress: 0, error: undefined })));

    const blob = await assemble(recId, rec.mimeType);
    if (blob.size === 0) throw new Error("Nessun audio salvato per questa registrazione");
    const sha = await sha256Hex(blob);

    const durationSeconds = rec.durationMs != null ? Math.round(rec.durationMs) / 1000 : null;
    const diario: Record<string, string> = rec.diarioId ? { "X-Diario": rec.diarioId } : {};
    const init = (key: string) =>
      api<Session>("/api/v1/uploads", {
        method: "POST",
        headers: { "Idempotency-Key": key, ...diario },
        json: {
          filename: rec!.filename,
          mime_type: rec!.mimeType,
          total_size: blob.size,
          chunk_size: CHUNK,
          sha256: sha,
          source: rec!.kind === "registrazione" ? "registratore" : "caricamento",
          recorded_at: rec!.startedAt,
          recorded_at_source: rec!.startedAt ? "registratore_integrato" : null,
          duration_seconds: durationSeconds,
          client_metadata: {
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            recuperata: rec!.recovered ?? false,
            pezzi: rec!.pieces,
          },
        },
      });
    let key = rec.uploadKey ?? recId;
    let session: Session;
    try {
      session = await withRetry(() => init(key));
    } catch (e) {
      // Sessione scaduta sul server (giorni senza rete): se ne apre una nuova.
      // Il contenuto è deduplicato per impronta, quindi non si crea un doppione.
      if (!(e instanceof ApiError) || e.status !== 410) throw e;
      key = `${recId}-${Date.now()}`;
      await updateRecording(recId, { uploadKey: key });
      session = await withRetry(() => init(key));
    }
    onChange((rec = await updateRecording(recId, { uploadId: session.id })));

    if (session.stato !== "completata") {
      const total = session.numero_blocchi;
      const missing = [...session.blocchi_mancanti];
      let done = total - missing.length;
      for (const i of missing) {
        const part = blob.slice(i * session.dimensione_blocco, (i + 1) * session.dimensione_blocco);
        const buf = await part.arrayBuffer();
        const partSha = await sha256Hex(buf);
        await withRetry(() =>
          api(`/api/v1/uploads/${session.id}/chunks/${i}`, {
            method: "PUT",
            headers: { "Content-Type": "application/octet-stream", "X-Chunk-SHA256": partSha, ...diario },
            body: buf,
          }),
        );
        done++;
        onChange((rec = await updateRecording(recId, { progress: done / total })));
      }
      session = await withRetry(() =>
        api<Session>(`/api/v1/uploads/${session.id}/complete`, { method: "POST", headers: diario }),
      );
    }

    // Solo ora, con l'originale conservato sul PC, si libera lo spazio sul telefono.
    await deletePieces(recId);
    onChange(
      await updateRecording(recId, {
        status: "conservato",
        progress: 1,
        recordingId: session.recording_id ?? undefined,
        duplicate: session.duplicato,
        bytes: blob.size,
      }),
    );
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    onChange(await updateRecording(recId, { status: "errore", error: msg, progress: undefined }));
    throw e;
  } finally {
    inFlight.delete(recId);
  }
}
