var CACHE='callbot-b28'; // 2026-10-19 고원 로고·기능 점검 — 옛 캐시를 비운다. 실패 응답(404 등)은 담지 않는다.
var CORE=['/','/index.html','/manifest.json','/favicon.svg','/brand/aicc-app-icon.svg','/icon-192.png','/icon-512.png'];
self.addEventListener('install',function(e){self.skipWaiting();e.waitUntil(caches.open(CACHE).then(function(c){return c.addAll(CORE).catch(function(){});}));});
self.addEventListener('activate',function(e){e.waitUntil(caches.keys().then(function(ks){return Promise.all(ks.map(function(k){if(k!==CACHE)return caches.delete(k);}));}));self.clients.claim();});
self.addEventListener('fetch',function(e){var req=e.request;if(req.method!=='GET'){return;}var url=new URL(req.url);
  if(url.pathname.indexOf('/api/')===0){return;} // API는 항상 네트워크
  // 화면 이동: 네트워크 우선. 정상 응답만 그 주소 이름으로 담는다(예전엔 모든 화면을 '/'로 담아 오프라인 때 랜딩 대신 콘솔이 떴다)
  if(req.mode==='navigate'){e.respondWith(fetch(req).then(function(r){if(r&&r.ok){var cp=r.clone();caches.open(CACHE).then(function(c){c.put(url.pathname,cp);});}return r;}).catch(function(){return caches.match(url.pathname).then(function(m){return m||caches.match('/');});}));return;}
  e.respondWith(caches.match(req).then(function(m){return m||fetch(req).then(function(r){if(r&&r.ok){var cp=r.clone();caches.open(CACHE).then(function(c){c.put(req,cp);});}return r;}).catch(function(){return m;});}));
});
