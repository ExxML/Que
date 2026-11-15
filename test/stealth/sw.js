"use strict";
self.addEventListener("install", event => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", event => {
    if (new URL(event.request.url).pathname === "/api/ping") event.waitUntil(self.clients.matchAll().then(clients => {
        for (const client of clients) client.postMessage({type: "owned-fetch", url: event.request.url, wall_ms: Date.now()});
    }));
});
