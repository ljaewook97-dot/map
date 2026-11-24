// 법령 뷰어 서비스 워커 v7.2
// 앱 파일은 '설치할 때 통째로 저장해 두고 그것을 쓴다'. 새 버전은 뒤에서 받아 두고 기다리다가,
// 화면에서 사용자가 '업데이트'를 눌렀을 때만 바꿔 끼운다(딸깍 지도처럼). 파이오다이드(큰 파일)는 한 번 받으면 저장본을 쓴다.
const VER = "10.101";
const V = "lv-" + VER;
const SHELL = ["./", "index.html", "worker.js", "lawcore.py", "mobile_api.py", "manifest.webmanifest", "icon-192.png", "icon-512.png", "changes.txt"];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(V).then((c) => Promise.all(SHELL.map((u) => c.add(new Request(u, { cache: "reload" })).catch(() => {})))));   // skipWaiting 하지 않음: 승인을 기다린다
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== V && k !== "lv-pyo").map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("message", (e) => {
  const d = e.data || {};
  if (d.t === "skip") self.skipWaiting();
  else if (d.t === "ver" && e.ports && e.ports[0]) e.ports[0].postMessage({ v: VER });
});
self.addEventListener("fetch", (e) => {
  const r = e.request;
  if (r.method !== "GET") return;
  const u = new URL(r.url);
  const pyo = /pyodide/i.test(u.pathname) || u.hostname === "cdn.jsdelivr.net";
  if (pyo) {                                                    // 파이오다이드: 저장본 우선
    e.respondWith(caches.open("lv-pyo").then(async (c) => {
      const hit = await c.match(r);
      if (hit) return hit;
      const res = await fetch(r);
      if (res && res.ok) c.put(r, res.clone());
      return res;
    }));
    return;
  }
  if (u.origin !== location.origin) return;                     // 법제처·GitHub·구글 서버는 그대로 통과
  if (u.searchParams.has("nc")) return;                         // ?nc= 가 붙은 요청(새 버전 소식 확인)은 항상 인터넷으로
  e.respondWith(caches.open(V).then(async (c) => {              // 앱 파일: 저장본 우선, 없으면 인터넷
    const hit = await c.match(r, { ignoreSearch: true });
    if (hit) {
      if (u.search && hit.url !== r.url) return new Response(await hit.clone().blob(), { status: hit.status, statusText: hit.statusText, headers: hit.headers });   // 주소 뒤 ?값(워커 설정)이 사라지지 않게 새 응답으로 감싼다
      return hit;
    }
    try {
      const res = await fetch(r);
      if (res && res.ok) c.put(r, res.clone());
      return res;
    } catch (x) {
      if (r.mode === "navigate") { const i = await c.match("index.html"); if (i) return i; }
      return Response.error();
    }
  }));
});
