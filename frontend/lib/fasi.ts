// Stati dell'elaborazione sul PC, in parole.

export const FASE: Record<string, string> = {
  ricevuto: "Ricevuto",
  originale_conservato: "In coda per la trascrizione",
  trascrizione_in_corso: "Trascrizione in corso",
  trascrizione_completata: "Trascritto, in coda per l’analisi",
  analisi_in_corso: "Analisi in corso",
  dati_strutturati_salvati: "Nota salvata, indicizzazione",
  indicizzazione_completata: "Quasi pronta",
  archiviazione_completata: "Nota pronta",
  errore: "In attesa di un nuovo tentativo",
  errore_definitivo: "Fermo: serve un intervento",
  annullato: "Annullato",
};

export const CONCLUSI = new Set(["archiviazione_completata", "errore_definitivo", "annullato"]);

/** Spiegazioni per i codici di errore più probabili. */
export const ERRORE: Record<string, string> = {
  ollama_non_raggiungibile: "Il motore di analisi (Ollama) non risponde sul PC. Riparte da solo quando torna attivo.",
  modello_non_installato: "Il modello di analisi non è ancora scaricato sul PC: esegui scripts/prepara-ai.sh.",
  modello_trascrizione_non_disponibile: "Il modello di trascrizione non è ancora sul PC: esegui scripts/prepara-ai.sh.",
  nessun_parlato: "Nell’audio non è stato riconosciuto parlato.",
  audio_illeggibile: "Il file audio non si riesce a leggere.",
  originale_mancante: "Il file originale non è più nello storage del PC.",
  analisi_non_valida: "Il modello di analisi ha dato risposte non valide.",
};

export function spiegaErrore(code: string | null | undefined): string | null {
  return code ? ERRORE[code] ?? null : null;
}
