// Client delle API. Le richieste che modificano dati portano X-CSRF-Token.

let csrfToken: string | null = null;

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public body: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

export function setCsrf(token: string | null) {
  csrfToken = token;
}

export async function api<T = unknown>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD" && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  const res = await fetch(path, { ...init, method, headers, body, credentials: "same-origin" });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) {
    // Il diario è stato chiuso o eliminato altrove: si torna allo scaffale.
    if (res.status === 409 && data.errore === "nessun_diario" && !headers.has("X-Diario")) {
      window.location.replace("/diari/");
    }
    throw new ApiError(res.status, data.errore ?? "errore", data.messaggio ?? `Errore ${res.status}`, data);
  }
  return data as T;
}

export type Me = {
  id: string;
  username: string;
  csrf_token: string | null;
  amministratore: boolean;
  cambia_password: boolean;
  diario: { id: string; nome: string; copertina: "cuoio" | "notte" | "bosco" | "vino" | "ardesia" | "senape" } | null;
};

/** Sessione corrente, o null se non autenticati. */
export async function loadSession(): Promise<Me | null> {
  try {
    const me = await api<Me>("/api/v1/auth/me");
    setCsrf(me.csrf_token);
    return me;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}

export async function login(username: string, password: string): Promise<void> {
  const r = await api<{ csrf_token: string }>("/api/v1/auth/login", {
    method: "POST",
    json: { username, password },
  });
  setCsrf(r.csrf_token);
}

export async function registrati(codice: string, username: string, password: string): Promise<void> {
  const r = await api<{ csrf_token: string }>("/api/v1/auth/registrati", {
    method: "POST",
    json: { codice, username, password },
  });
  setCsrf(r.csrf_token);
}

export async function logout(): Promise<void> {
  await api("/api/v1/auth/logout", { method: "POST" });
  setCsrf(null);
}

/** Data e ora locali con il fuso (es. 2026-10-21T18:00:00+02:00): "ieri" è quello del telefono. */
export function isoLocale(d: Date = new Date()): string {
  const p = (n: number) => String(Math.abs(n)).padStart(2, "0");
  const off = -d.getTimezoneOffset();
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:` +
    `${p(d.getSeconds())}${off >= 0 ? "+" : "-"}${p(Math.trunc(off / 60))}:${p(off % 60)}`;
}

/** POST con risposta NDJSON: ogni riga arriva a onRiga appena il server la manda. */
export async function apiStream(path: string, json: unknown, onRiga: (r: Record<string, unknown>) => void,
  signal?: AbortSignal): Promise<void> {
  const headers = new Headers({ "Content-Type": "application/json" });
  if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  const res = await fetch(path, { method: "POST", headers, body: JSON.stringify(json), credentials: "same-origin", signal });
  if (!res.ok || !res.body) {
    const data = await res.json().catch(() => ({}));
    throw new ApiError(res.status, data.errore ?? "errore", data.messaggio ?? `Errore ${res.status}`, data);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let resto = "";
  for (;;) {
    const { done, value } = await reader.read();
    resto += dec.decode(value ?? new Uint8Array(), { stream: !done });
    const righe = resto.split("\n");
    resto = righe.pop() ?? "";
    for (const r of righe) if (r.trim()) onRiga(JSON.parse(r));
    if (done) break;
  }
  if (resto.trim()) onRiga(JSON.parse(resto));
}
