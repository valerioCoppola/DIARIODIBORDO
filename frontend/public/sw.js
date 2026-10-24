// Service worker del Diario di Bordo.
// Conserva l'interfaccia per aprirla anche con rete instabile.
// Non tocca mai le richieste /api: dati e audio non passano dalla cache.
// Le pagine si chiedono sempre al PC scavalcando la cache del browser (cache: "no-cache"):
// dopo un aggiornamento una pagina vecchia punterebbe a file che non esistono più.
const CACHE = "ddb-shell-v7";
const SHELL = ["/", "/accesso/", "/registrati/", "/diari/", "/account/", "/persone/", "/archivio/", "/nota/", "/mappa/",
  "/cerca/", "/manifest.webmanifest", "/icons/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL.map((u) => new Request(u, { cache: "reload" }))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  // File con hash nel nome: immutabili, prima la cache.
  if (url.pathname.startsWith("/_next/static/")) {
    e.respondWith(
      caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return res;
      })),
    );
    return;
  }

  // Pagine: prima la rete, la cache solo se il PC non risponde.
  e.respondWith(
    fetch(e.request, { cache: "no-cache" })
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(e.request).then((hit) => hit || caches.match("/"))),
  );
});
