// Archivio locale nel browser (IndexedDB).
//
// Ogni registrazione è salvata a pezzi man mano che il microfono li produce:
// se la pagina si chiude, il telefono si blocca o la batteria finisce,
// ciò che è stato registrato fin lì resta qui ed è recuperabile.
// I pezzi audio vengono cancellati solo dopo che il PC ha confermato
// di aver conservato l'originale.

export type LocalStatus =
  | "in_registrazione" // il microfono sta scrivendo
  | "da_inviare" // completa sul telefono, non ancora sul PC
  | "in_invio"
  | "conservato" // il PC ha l'originale; audio locale rimosso
  | "errore";

export type LocalRecording = {
  id: string; // usato anche come Idempotency-Key
  kind: "registrazione" | "file";
  diarioId?: string; // il diario in cui è stata fatta: lì arriva, anche se intanto se ne apre un altro
  createdAt: string;
  startedAt: string | null; // ISO 8601 con offset, solo dal registratore
  durationMs: number | null;
  mimeType: string;
  filename: string | null;
  bytes: number;
  pieces: number;
  status: LocalStatus;
  recovered?: boolean; // ripresa dopo un'interruzione della pagina
  progress?: number; // 0..1 durante l'invio
  uploadId?: string;
  uploadKey?: string; // Idempotency-Key, se diversa dall'id
  recordingId?: string;
  duplicate?: boolean;
  error?: string;
};

const DB_NAME = "diario-di-bordo";
const DB_VERSION = 1;

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      db.createObjectStore("recordings", { keyPath: "id" });
      const pieces = db.createObjectStore("pieces", { keyPath: ["recId", "seq"] });
      pieces.createIndex("byRec", "recId");
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

let dbPromise: Promise<IDBDatabase> | null = null;
const db = () => (dbPromise ??= open());

function tx<T>(stores: string[], mode: IDBTransactionMode, fn: (t: IDBTransaction) => T): Promise<T> {
  return db().then(
    (d) =>
      new Promise<T>((resolve, reject) => {
        const t = d.transaction(stores, mode);
        let result: T;
        try {
          result = fn(t);
        } catch (e) {
          reject(e);
          return;
        }
        t.oncomplete = () => resolve(result);
        t.onerror = () => reject(t.error);
        t.onabort = () => reject(t.error ?? new Error("transazione annullata"));
      }),
  );
}

function reqP<T>(r: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

export async function putRecording(rec: LocalRecording): Promise<void> {
  await tx(["recordings"], "readwrite", (t) => t.objectStore("recordings").put(rec));
}

export async function getRecording(id: string): Promise<LocalRecording | undefined> {
  const d = await db();
  return reqP(d.transaction("recordings").objectStore("recordings").get(id));
}

export async function updateRecording(id: string, patch: Partial<LocalRecording>): Promise<LocalRecording> {
  let out!: LocalRecording;
  await tx(["recordings"], "readwrite", (t) => {
    const s = t.objectStore("recordings");
    const r = s.get(id);
    r.onsuccess = () => {
      if (!r.result) throw new Error("registrazione locale inesistente");
      out = { ...r.result, ...patch };
      s.put(out);
    };
  });
  return out;
}

export async function listRecordings(): Promise<LocalRecording[]> {
  const d = await db();
  const all = await reqP(d.transaction("recordings").objectStore("recordings").getAll());
  return (all as LocalRecording[]).sort((a, b) => b.createdAt.localeCompare(a.createdAt));
}

/** Aggiunge un pezzo e aggiorna i contatori nella stessa transazione. */
export async function appendPiece(recId: string, seq: number, blob: Blob): Promise<void> {
  await tx(["pieces", "recordings"], "readwrite", (t) => {
    t.objectStore("pieces").put({ recId, seq, blob });
    const s = t.objectStore("recordings");
    const r = s.get(recId);
    r.onsuccess = () => {
      if (r.result) s.put({ ...r.result, bytes: r.result.bytes + blob.size, pieces: Math.max(r.result.pieces, seq + 1) });
    };
  });
}

/** Ricompone il file dai pezzi, nell'ordine in cui sono stati registrati. */
export async function assemble(recId: string, mimeType: string): Promise<Blob> {
  const d = await db();
  const rows = (await reqP(
    d.transaction("pieces").objectStore("pieces").index("byRec").getAll(IDBKeyRange.only(recId)),
  )) as { seq: number; blob: Blob }[];
  rows.sort((a, b) => a.seq - b.seq);
  return new Blob(rows.map((r) => r.blob), { type: mimeType });
}

export async function deletePieces(recId: string): Promise<void> {
  await tx(["pieces"], "readwrite", (t) => {
    const idx = t.objectStore("pieces").index("byRec");
    const cur = idx.openKeyCursor(IDBKeyRange.only(recId));
    cur.onsuccess = () => {
      const c = cur.result;
      if (c) {
        t.objectStore("pieces").delete(c.primaryKey);
        c.continue();
      }
    };
  });
}

export async function deleteRecording(recId: string): Promise<void> {
  await deletePieces(recId);
  await tx(["recordings"], "readwrite", (t) => t.objectStore("recordings").delete(recId));
}

/** Registrazioni rimaste "in_registrazione" dopo una chiusura improvvisa. */
export async function recoverInterrupted(activeId: string | null): Promise<number> {
  const all = await listRecordings();
  let n = 0;
  for (const r of all) {
    if (r.status === "in_registrazione" && r.id !== activeId) {
      await updateRecording(r.id, { status: r.bytes > 0 ? "da_inviare" : "errore", recovered: true,
        error: r.bytes > 0 ? undefined : "Registrazione interrotta prima di salvare audio" });
      n++;
    }
    if (r.status === "in_invio") {
      await updateRecording(r.id, { status: "da_inviare", progress: undefined });
    }
  }
  return n;
}

/** Chiede al browser di non cancellare questi dati quando lo spazio scarseggia. */
export async function requestPersistence(): Promise<boolean> {
  if (navigator.storage?.persist) {
    try {
      return await navigator.storage.persist();
    } catch {
      return false;
    }
  }
  return false;
}

export function newId(): string {
  return crypto.randomUUID();
}

/** Data locale con offset, es. 2026-10-21T08:30:00+02:00 */
export function isoWithOffset(d: Date): string {
  const pad = (n: number) => String(Math.abs(n)).padStart(2, "0");
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` +
    `${sign}${pad(Math.trunc(off / 60))}:${pad(off % 60)}`
  );
}
