var CACHE='callbot-b81';
var CORE=['/','/index.html','/manifest.json','/icon-192.png','/icon-512.png'];
self.addEventListener('install',function(e){self.skipWaiting();e.waitUntil(caches.open(CACHE).then(function(c){return c.addAll(CORE).catch(function(){});}));});
self.addEventListener('activate',function(e){e.waitUntil(caches.keys().then(function(ks){return Promise.all(ks.map(function(k){if(k!==CACHE)return caches.delete(k);}));}));self.clients.claim();});
self.addEventListener('fetch',function(e){var req=e.request;if(req.method!=='GET'){return;}var url=new URL(req.url);
  if(url.pathname.indexOf('/api/')===0){return;} // API는 항상 네트워크
  if(req.mode==='navigate'){e.respondWith(fetch(req).then(function(r){var cp=r.clone();caches.open(CACHE).then(function(c){c.put('/',cp);});return r;}).catch(function(){return caches.match('/');}));return;}
  e.respondWith(caches.match(req).then(function(m){return m||fetch(req).then(function(r){var cp=r.clone();caches.open(CACHE).then(function(c){c.put(req,cp);});return r;}).catch(function(){return m;});}));
});
