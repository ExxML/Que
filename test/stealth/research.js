"use strict";

// All of these observations come from ordinary web APIs, never native diagnostics.
function installResearchProbes(log, onButton, state) {
    const features = {
        idle: "IdleDetector" in window, pressure: "PressureObserver" in window,
        eyedropper: "EyeDropper" in window, screens: !!window.getScreenDetails,
        worklet: "AudioWorkletNode" in window, webgpu: !!navigator.gpu,
        fileObserver: "FileSystemObserver" in window, isolated: crossOriginIsolated,
        performanceTypes: PerformanceObserver.supportedEntryTypes
    };
    log("research-capabilities", features);
    let idle, pressure, details, audioNode, gl, gpuTimer;
    const resourceWorker = new Worker("/storage-worker.js");
    resourceWorker.onmessage = event => { const {type, ...data} = event.data; log(type, data); };
    resourceWorker.onerror = event => log("probe-error", {probe: "storage-memory", message: event.message});
    onButton("storage-probe", () => resourceWorker.postMessage({command: "start", mib: Number(document.getElementById("storage-mib").value)}));
    onButton("storage-stop", () => resourceWorker.postMessage({command: "stop"}));
    onButton("memory-probe", () => resourceWorker.postMessage({command: "memory"}));
    function unavailable(probe) { log("unsupported", {probe}); throw new Error(`${probe} is unavailable in this configuration`); }
    onButton("idle-probe", async () => {
        if (!features.idle) return unavailable("idle");
        if (await IdleDetector.requestPermission() !== "granted") throw new Error("Idle permission denied");
        idle = new IdleDetector();
        const record = () => log("idle-state", {user: idle.userState, screen: idle.screenState, hasFocus: document.hasFocus(), threshold_ms: 60000});
        idle.addEventListener("change", record);
        await idle.start({threshold: 60000}); record();
    });
    onButton("pressure-probe", async () => {
        if (!features.pressure) return unavailable("pressure");
        pressure?.disconnect();
        pressure = new PressureObserver(records => records.forEach(record => log("pressure-sample", {source: record.source, state: record.state, time: record.time})));
        await pressure.observe("cpu", {sampleInterval: 1000}); log("pressure-start", {source: "cpu"});
    });
    onButton("eyedropper-probe", async () => {
        if (!features.eyedropper) return unavailable("eyedropper");
        log("eyedropper-open");
        const result = await new EyeDropper().open();
        log("eyedropper-result", {...result, instruction: "Correlate physical selection with retained baseline/control/action frames; this API does not reveal coordinates"});
    });
    onButton("screen-probe", async () => {
        if (!features.screens) return unavailable("screens");
        details = await getScreenDetails();
        const describe = screen => Object.fromEntries(["left", "top", "width", "height", "availLeft", "availTop", "availWidth", "availHeight", "devicePixelRatio", "isPrimary", "isInternal", "label"].map(key => [key, screen[key]]));
        const record = event => log("screen-details", {event: event?.type || "initial", screens: [...details.screens].map(describe), current: describe(details.currentScreen)});
        details.addEventListener("screenschange", record); details.addEventListener("currentscreenchange", record);
        details.screens.forEach(screen => screen.addEventListener("change", record)); record();
    });
    onButton("worklet-probe", async () => {
        const {audioContext, microphoneStream} = state();
        if (!audioContext || !microphoneStream) throw new Error("Acquire the real microphone first");
        if (!features.worklet) return unavailable("worklet");
        await audioContext.audioWorklet.addModule("/audio-worklet.js");
        audioNode?.disconnect(); audioNode = new AudioWorkletNode(audioContext, "audit-quantum");
        const sink = audioContext.createGain(); sink.gain.value = 0;
        audioContext.createMediaStreamSource(microphoneStream).connect(audioNode).connect(sink).connect(audioContext.destination);
        audioNode.port.onmessage = event => log("audio-quantum", {...event.data, received: performance.now(), state: audioContext.state, output: audioContext.getOutputTimestamp?.()});
        audioNode.onprocessorerror = () => log("probe-error", {probe: "worklet", reason: "Audio processor failed"});
        log("worklet-start", {sampleRate: audioContext.sampleRate});
    });
    onButton("gpu-probe", () => {
        if (gpuTimer) { clearInterval(gpuTimer); gpuTimer = null; log("gpu-stop"); return; }
        const canvas = document.createElement("canvas"); canvas.width = canvas.height = 128;
        gl = canvas.getContext("webgl2"); if (!gl) return unavailable("webgl2");
        const extension = gl.getExtension("EXT_disjoint_timer_query_webgl2");
        canvas.addEventListener("webglcontextlost", event => { event.preventDefault(); log("probe-error", {probe: "gpu", reason: "Context lost"}); });
        let query = null;
        gpuTimer = setInterval(() => {
            if (query) {
                if (gl.getParameter(extension.GPU_DISJOINT_EXT)) { log("probe-error", {probe: "gpu", reason: "Disjoint GPU clock"}); gl.deleteQuery(query); query = null; }
                else if (gl.getQueryParameter(query, gl.QUERY_RESULT_AVAILABLE)) { log("gpu-timer", {gpu_ms: gl.getQueryParameter(query, gl.QUERY_RESULT) / 1000000, clock: "EXT_disjoint_timer_query_webgl2"}); gl.deleteQuery(query); query = null; }
                else { log("gpu-pending"); return; }
            }
            const start = performance.now();
            if (extension) { query = gl.createQuery(); gl.beginQuery(extension.TIME_ELAPSED_EXT, query); }
            for (let i = 0; i < 200; i++) { gl.clearColor(i % 2, 0, 1, 1); gl.clear(gl.COLOR_BUFFER_BIT); }
            if (extension) gl.endQuery(extension.TIME_ELAPSED_EXT);
            gl.finish(); log("gpu-sample", {duration: performance.now() - start, clock: "page wall time including GPU synchronization", timerExtension: !!extension});
        }, 1000);
        log("gpu-start", {timerExtension: !!extension});
    });
    onButton("clipboard-formats", async () => {
        for (const item of await navigator.clipboard.read()) {
            for (const type of item.types) {
                const blob = await item.getType(type), bytes = await blob.arrayBuffer();
                const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(x => x.toString(16).padStart(2, "0")).join("");
                log("clipboard-read", {format: type, bytes: blob.size, sha256: hash, text: type.startsWith("text/") ? await blob.text() : null});
            }
        }
    });
    for (const type of ["event", "long-animation-frame", "layout-shift", "first-input"]) {
        if (PerformanceObserver.supportedEntryTypes.includes(type)) {
            new PerformanceObserver(list => list.getEntries().forEach(item => log(`performance-${type}`, item.toJSON()))).observe({type, buffered: true, durationThreshold: 16});
        } else log("unsupported", {probe: `performance-${type}`});
    }
    for (const type of ["resize", "scroll"]) visualViewport?.addEventListener(type, () => log("visual-viewport", {type, width: visualViewport.width, height: visualViewport.height, offsetLeft: visualViewport.offsetLeft, offsetTop: visualViewport.offsetTop, scale: visualViewport.scale}));
    setInterval(() => {
        log("focus-poll", {hasFocus: document.hasFocus(), activeElement: document.activeElement?.id, visibility: document.visibilityState});
        if (idle) log("idle-state", {user: idle.userState, screen: idle.screenState, hasFocus: document.hasFocus(), threshold_ms: 60000});
    }, 250);
}
