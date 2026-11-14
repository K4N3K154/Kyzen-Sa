/* App da Kyzen SA: abre rápido, funciona sem internet com a última versão vista e mostra notificações.
   Página: rede primeiro (sempre a versão nova), cópia salva se estiver offline.
   Imagens do site: cópia salva, atualizada por trás. Dados (/api) nunca ficam salvos aqui. */
const V = "kyzen-v2";
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
  if (r.method !== "GET" || u.origin !== location.origin || u.pathname.startsWith("/api/") || u.pathname.startsWith("/auth/")) return;
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

/* Notificações (anúncios novos e eventos começando) */
self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { b: e.data ? e.data.text() : "" }; }
  const url = typeof d.u === "string" && d.u.startsWith("/") ? d.u : "/";
  e.waitUntil(self.registration.showNotification(d.t || "Kyzen SA", {
    body: d.b || "", icon: "/img/icon-192.png", badge: "/img/icon-192.png", data: { url }, tag: d.t || "kyzen"
  }));
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(ws => {
    for (const w of ws) { if (new URL(w.url).origin === location.origin) { w.focus(); return w.navigate(url); } }
    return self.clients.openWindow(url);
  }));
});
