"use strict";
let load = false, previous = performance.now();
self.onmessage = event => { load = !!event.data.load; };
setInterval(() => {
    const now = performance.now(), start = now;
    if (load) while (performance.now() - start < 35) Math.sqrt(Math.random());
    self.postMessage({interval_ms: now - previous, load, timer_resolution_sample: performance.now() - start});
    previous = now;
}, 50);
