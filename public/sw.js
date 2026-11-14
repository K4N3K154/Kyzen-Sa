/* App da Kyzen SA: abre rápido e funciona sem internet com a última versão vista.
   Página: rede primeiro (sempre a versão nova), cópia salva se estiver offline.
   Imagens do site: cópia salva, atualizada por trás. Dados (/api) nunca ficam salvos aqui. */
const V = "kyzen-v1";
const BASE = ["/", "/img/logo.jpg", "/img/icon-192.png", "/manifest.webmanifest"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(V).then(c => c.addAll(BASE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== "GET" || u.origin !== location.origin || u.pathname.startsWith("/api/")) return;
  if (r.mode === "navigate") {
    e.respondWith(fetch(r).then(res => {
      if (res.ok && u.pathname === "/") caches.open(V).then(c => c.put("/", res.clone()));
      return res;
    }).catch(() => caches.match("/")));
    return;
  }
  if (u.pathname.startsWith("/img/")) {
    e.respondWith(caches.open(V).then(c => c.match(r).then(hit => {
      const rede = fetch(r).then(res => { if (res.ok) c.put(r, res.clone()); return res; }).catch(() => hit);
      return hit || rede;
    })));
  }
});
