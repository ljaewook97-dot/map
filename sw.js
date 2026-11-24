/* 딸깍 지도 서비스 워커 - 페이지는 인터넷이 되면 항상 새 것, 안 되면 저장본. 지도 그림은 본 만큼 저장. */
var V='ddm-page-v1',T='ddm-tiles-v1',MAXT=3000,puts=0;
var PAGE=new URL('./__page',self.registration.scope).href;
self.addEventListener('install',function(e){self.skipWaiting();e.waitUntil(caches.open(V).then(function(c){return c.addAll(['manifest.json','icon-192.png','icon-512.png','apple-touch-icon.png'])}).catch(function(){}))});
self.addEventListener('activate',function(e){e.waitUntil(caches.keys().then(function(ks){return Promise.all(ks.filter(function(k){return k!==V&&k!==T}).map(function(k){return caches.delete(k)}))}).then(function(){return self.clients.claim()}))});
function trim(c){c.keys().then(function(ks){var est=(self.navigator&&navigator.storage&&navigator.storage.estimate)?navigator.storage.estimate():Promise.resolve({});return est.then(function(e){var lim=MAXT;if(e&&e.quota&&e.usage/e.quota>.6)lim=Math.min(lim,500);else if(e&&e.usage>3e8)lim=Math.min(lim,700);if(ks.length>lim)ks.slice(0,ks.length-lim+Math.min(100,lim/5|0)).forEach(function(k){c.delete(k)})})}).catch(function(){})}
self.addEventListener('fetch',function(e){var r=e.request;if(r.method!=='GET')return;var u=new URL(r.url);
 if(u.origin===self.location.origin){
  if(r.mode==='navigate'||/(\/|index\.html)$/.test(u.pathname)){
   e.respondWith(fetch(r,{cache:'no-store'}).then(function(res){if(res&&res.ok){var cp=res.clone();caches.open(V).then(function(c){c.put(PAGE,cp)})}return res}).catch(function(){return caches.match(PAGE).then(function(m){return m||Response.error()})}));return}
  e.respondWith(caches.match(r).then(function(m){return m||fetch(r).then(function(res){if(res&&res.ok){var cp=res.clone();caches.open(V).then(function(c){c.put(r,cp)})}return res})}));return}
 if(r.destination==='image'){
  e.respondWith(caches.open(T).then(function(c){return c.match(r).then(function(m){
   var net=fetch(r).then(function(res){if(res&&(res.ok||res.type==='opaque')){c.put(r,res.clone());if(++puts%60===0)trim(c)}return res}).catch(function(){return m||Response.error()});
   return m||net})}))}
});
