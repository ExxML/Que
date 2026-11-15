"use strict";
let handle = null, root = null, name = null, timer = null, memoryTimer = null, preparing = false;
const block = new Uint8Array(4096);
let randomState = 184731;
function random() { randomState ^= randomState << 13; randomState ^= randomState >>> 17; randomState ^= randomState << 5; return randomState >>> 0; }
async function stopStorage() {
    clearInterval(timer); timer = null;
    if (handle) { handle.close(); handle = null; }
    if (root && name) { await root.removeEntry(name); name = null; }
    postMessage({type: "storage-stopped"});
}
async function command(data) {
    try {
        if (data.command === "memory") {
            if (memoryTimer) { clearInterval(memoryTimer); memoryTimer = null; postMessage({type: "memory-stopped"}); return; }
            const data = new Uint32Array(8 * 1024 * 1024); // 32 MiB, confined to this worker.
            for (let i = 0; i < data.length; i += 16) data[i] = random();
            memoryTimer = setInterval(() => {
                let checksum = 0; const start = performance.now();
                for (let i = 0; i < data.length; i += 16) checksum ^= data[i];
                postMessage({type: "memory-sample", duration: performance.now() - start, bytes: data.byteLength, stride: 64, checksum});
            }, 250);
            postMessage({type: "memory-start", bytes: data.byteLength}); return;
        }
        if (preparing) throw new Error("Storage fixture preparation is still active");
        if (data.command === "stop") { await stopStorage(); return; }
        if (timer || handle) throw new Error("Stop the existing storage probe first");
        const mib = Number(data.mib);
        if (!Number.isInteger(mib) || mib < 1 || mib > 2048) throw new Error("Choose a storage budget from 1 to 2048 MiB");
        preparing = true;
        root = await navigator.storage.getDirectory();
        const estimate = await navigator.storage.estimate(), bytes = mib * 1024 * 1024;
        if (!estimate.quota || bytes > (estimate.quota - (estimate.usage || 0)) / 4) throw new Error("Storage budget exceeds one quarter of available origin quota");
        name = `que-audit-${crypto.randomUUID()}.bin`;
        handle = await (await root.getFileHandle(name, {create: true})).createSyncAccessHandle();
        const chunk = new Uint8Array(1024 * 1024);
        for (let i = 0; i < chunk.length; i++) chunk[i] = random() & 255;
        for (let offset = 0; offset < bytes; offset += chunk.length) {
            if (handle.write(chunk, {at: offset}) !== chunk.length) throw new Error("Incomplete fixture write");
        }
        handle.flush();
        postMessage({type: "storage-start", bytes, quota: estimate, scope: "Owned OPFS file only; cache bypass and same physical disk are unverified"});
        timer = setInterval(() => {
            try {
                const at = (random() % (bytes / 4096)) * 4096;
                const start = performance.now(); const read = handle.read(block, {at});
                if (read !== block.length) throw new Error("Short fixture read");
                postMessage({type: "storage-sample", duration: performance.now() - start, bytes: read, offset: at, fixture_bytes: bytes});
            } catch (error) { postMessage({type: "probe-error", probe: "storage", message: error.message}); clearInterval(timer); }
        }, 50);
    } catch (error) {
        postMessage({type: "probe-error", probe: "storage-memory", message: error.message});
        await stopStorage();
    } finally { preparing = false; }
}
let commands = Promise.resolve();
onmessage = event => { commands = commands.then(() => command(event.data)).catch(error => postMessage({type: "probe-error", probe: "resource-command", message: error.message})); };
