"use strict";
// Headless harness self-test only. These clicks operate the observer's own probes, never Que or physical keys.
const socket = new WebSocket(process.argv[2]), pending = new Map();
let sequence = 0;
socket.onmessage = event => {
    const value = JSON.parse(event.data), callback = pending.get(value.id);
    if (callback) { pending.delete(value.id); value.error ? callback.reject(new Error(JSON.stringify(value.error))) : callback.resolve(value.result); }
};
function evaluate(expression) {
    return new Promise((resolve, reject) => {
        const id = ++sequence;
        pending.set(id, {resolve: result => result.exceptionDetails ? reject(new Error(JSON.stringify(result.exceptionDetails))) : resolve(result.result.value), reject});
        socket.send(JSON.stringify({id, method: "Runtime.evaluate", params: {expression, returnByValue: true, awaitPromise: true}}));
    });
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function main() {
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
    // A cold headless profile on a busy runner can take well over ten seconds to boot the page
    let boot, deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
        boot = await evaluate('({url: location.href, state: document.readyState, ready: typeof evidence !== "undefined" && evidence.some(e => e.type === "observer-ready"), errors: typeof evidence === "undefined" ? [] : evidence.filter(e => ["observer-error", "logger-error"].includes(e.type))})');
        if (boot.ready || boot.errors.length) break;
        await sleep(100);
    }
    if (!boot.ready || boot.errors.length) throw new Error(`Observer did not become ready: ${JSON.stringify(boot)}`);
    await evaluate('log("smoke-synthetic-controls", {scope: "observer worker self-test only"}); $("storage-mib").value = "1"; $("storage-probe").click(); $("memory-probe").click(); true');
    let samples;
    for (let index = 0; index < 150; index++) {
        samples = await evaluate('({storage: evidence.filter(e => e.type === "storage-sample").length, memory: evidence.filter(e => e.type === "memory-sample").length, errors: evidence.filter(e => ["observer-error", "probe-error"].includes(e.type))})');
        if (samples.errors.length || samples.storage >= 3 && samples.memory >= 3) break;
        await sleep(100);
    }
    if (samples.errors.length || samples.storage < 3 || samples.memory < 3) throw new Error(JSON.stringify(samples));
    await evaluate('$("storage-stop").click(); $("memory-probe").click(); true');
    let stopped = false;
    for (let index = 0; index < 50; index++) {
        stopped = await evaluate('evidence.some(e => e.type === "storage-stopped") && evidence.some(e => e.type === "memory-stopped")');
        if (stopped) break;
        await sleep(100);
    }
    if (!stopped) throw new Error("Owned probe cleanup did not finish");
    const remaining = await evaluate('(async () => { const names = []; for await (const name of (await navigator.storage.getDirectory()).keys()) names.push(name); return names.filter(name => name.startsWith("que-audit-")); })()');
    if (remaining.length) throw new Error("Owned storage fixture was not removed");
    await evaluate('flush().then(() => true)');
    console.log(JSON.stringify({pass: true, ...samples, owned_storage_removed: true, scope: "real headless OPFS/memory worker self-test, no Que detection conclusion"}));
    socket.close();
}
main().catch(error => { console.error(error); socket.close(); process.exitCode = 1; });
