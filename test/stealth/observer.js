"use strict";
const $ = id => document.getElementById(id);
const token = location.hash.slice(1);
history.replaceState(null, "", location.pathname + location.search);
const session = crypto.randomUUID();
const evidence = [], queue = [], streams = [], rtcPeers = [];
let evictedEvents = 0;
let seq = 0, attempt = null, lastAttempt = null, busy = false, transportFailed = false;
let manifest, currentSpec, captureStream, microphoneStream, audioContext, analyser;
let baseline = null, positive = false, calibrated = false, frameCount = 0, lastFrame = 0;
let lastBeacon = null, beaconChanged = performance.now(), lastSaved = 0, lastFrameSample = 0;
let loopbackLoad = false, workerLoad = false, previousAnimation = performance.now();
let workerTimer = performance.now(), audioSamples = 0;
let registering = false, ending = false, calibrationEpoch = null, calibrationSignature = null;
const pendingFrames = new Set();
const captureCanvas = document.createElement("canvas");
const captureContext = captureCanvas.getContext("2d", {willReadFrequently: true});
const workloadCanvas = document.createElement("canvas");
workloadCanvas.width = 256; workloadCanvas.height = 256;
const workloadContext = workloadCanvas.getContext("2d");
const worker = new Worker("/worker.js");

function log(type, data = {}, context = {}) {
    const event = {seq: ++seq, type, data, session, attempt, wall_ms: Date.now(), monotonic_ms: performance.now(), ...context};
    evidence.push(event); queue.push(event);
    if (evidence.length > 20000) { evidence.splice(0, 10000); evictedEvents += 10000; }
    if (queue.length > 100000) {
        transportFailed = true;
        $("status").textContent = "Evidence queue overflow: stop the run; coverage is incomplete.";
    }
    return event;
}
async function api(path, body) {
    const response = await fetch(path, {method: body === undefined ? "GET" : "POST", headers: {
        "X-Audit-Token": token, "Content-Type": "application/json"
    }, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store"});
    if (!response.ok) throw new Error(`Evidence recorder HTTP ${response.status}`);
    return response.json();
}
async function flush() {
    if (busy || registering) return;
    busy = true;
    try {
        while (queue.length) {
            const batch = queue.slice(0, 1000);
            const result = await api("/api/events", {session, events: batch});
            if (result.ack < batch[batch.length - 1].seq) throw new Error("Incomplete acknowledgement");
            queue.splice(0, batch.length);
        }
    } catch (error) {
        transportFailed = true;
        $("status").textContent = `Logger interrupted: ${error.message}. Run cannot pass.`;
    } finally { busy = false; }
}
function onButton(id, action) {
    $(id).addEventListener("click", async () => {
        try { await action(); } catch (error) {
            log("probe-error", {probe: id, name: error.name, message: error.message});
            $("status").textContent = `${id}: ${error.message}`;
            if (id === "begin") $("begin").disabled = false;
        }
    });
}
function specChanged() {
    currentSpec = manifest.scenarios.find(item => item.id === $("scenario").value);
    $("instructions").textContent = `${currentSpec.action}\n\nCompletion: ${currentSpec.completion}\n\nObserve: ${currentSpec.observations}\n\nProtection: ${currentSpec.protection}\n\nCleanup: ${currentSpec.cleanup}`;
    $("proof").replaceChildren();
    for (const name of currentSpec.proof) {
        const label = document.createElement("label"), field = document.createElement("input");
        label.textContent = name; field.dataset.proof = name; field.placeholder = "receipt:path#sha256 or event:sequence";
        label.append(field); $("proof").append(label);
    }
    $("capture_source").value = currentSpec.proof.includes("frames") ? "monitor" : "none";
    if (currentSpec.family === "statistics") {
        $("browser").value = currentSpec.id.split(".")[1].split("-")[0];
        $("display").value = "single-100"; $("placement").value = "same-monitor";
        $("window_mode").value = "normal"; $("exclusion").value = "true"; $("repeat").value = "1";
    }
}
function config() {
    return {...Object.fromEntries(["browser", "display", "placement", "window_mode", "capture_source", "exclusion", "repeat"].map(key => [key, $(key).value])), cross_origin_isolated: crossOriginIsolated};
}
async function begin() {
    if (attempt) throw new Error("An interval is already active");
    if (!$("confirmed").checked || !$("config-notes").value.trim()) throw new Error("Confirm the physical configuration and record notes first");
    const c = config(), selected = currentSpec;
    const id = `${selected.id}|${c.browser}|${c.display}|${c.placement}|${c.window_mode}|${c.capture_source}|${c.exclusion === "true" ? "True" : "False"}|${c.repeat}`;
    const seconds = Number($("seconds").value);
    if (!Number.isFinite(seconds) || seconds < 5 || seconds > 600) throw new Error("Interval must be 5–600 seconds");
    $("begin").disabled = true; $("review-save").disabled = true; $("scenario").disabled = true;
    $("status").textContent = "Setup countdown: focus the browser input; measurement begins in 5 seconds.";
    await new Promise(resolve => setTimeout(resolve, 5000));
    if (!document.hasFocus() || document.visibilityState !== "visible") throw new Error("Focus the observer before starting");
    attempt = crypto.randomUUID(); lastAttempt = attempt; registering = true; ending = false;
    const marker = log("interval-start", {configuration: c, userAgent: navigator.userAgent, hasFocus: document.hasFocus(),
        visibility: document.visibilityState, fullscreen: document.fullscreenElement?.id || !!document.fullscreenElement,
        pointerLock: !!document.pointerLockElement, calibration_epoch: calibrationEpoch,
        capture: captureStream?.getVideoTracks()[0]?.getSettings(), cpu_control: workerLoad, network_control: loopbackLoad});
    try {
        await api("/api/begin", {id, session, attempt, start_ms: marker.wall_ms, configuration_confirmed: true, configuration_notes: $("config-notes").value});
    } finally { registering = false; }
    log("heartbeat", {hasFocus: document.hasFocus(), visibility: document.visibilityState});
    $("status").textContent = `MEASURING ${selected.id}: perform the physical action now (${seconds}s).`;
    await new Promise(resolve => setTimeout(resolve, seconds * 1000));
    ending = true;
    await Promise.all([...pendingFrames]);
    const endMarker = log("interval-end", {text: $("text-testing-area").textContent, frames: frameCount, calibrated, transportFailed});
    if (transportFailed) log("logger-error", {reason: "Transport interruption occurred"});
    const end = endMarker.wall_ms, finalSeq = seq, finishedAttempt = attempt;
    attempt = null;
    while (busy) await new Promise(resolve => setTimeout(resolve, 50));
    await flush();
    await api("/api/finish", {attempt: finishedAttempt, end_ms: end, final_seq: finalSeq});
    $("status").textContent = `Interval ended (${lastAttempt}). Review evidence and record completion before continuing.`;
    $("review-save").disabled = false;
}
async function review() {
    const proof = Object.fromEntries([...document.querySelectorAll("[data-proof]")].map(input => [input.dataset.proof, input.value.trim()]));
    await api("/api/review", {attempt: lastAttempt, proof, completion: $("completion").value.trim(), review: $("review").value.trim(),
        leaks: $("leaks").value.split("\n").map(value => value.trim()).filter(Boolean), unsupported: $("unsupported").value.trim()});
    $("status").textContent = "Review saved. The runner evaluates completeness and findings separately.";
    $("scenario").disabled = false;
    $("begin").disabled = false;
    for (const id of ["completion", "review", "leaks", "unsupported"]) $(id).value = "";
    document.querySelectorAll("[data-proof]").forEach(field => { field.value = ""; });
}
function trackStream(stream, prefix) {
    streams.push(stream);
    for (const track of stream.getTracks()) {
        log(`${prefix}-track`, {kind: track.kind, label: track.label, settings: track.getSettings(), capabilities: track.getCapabilities?.()});
        for (const event of ["mute", "unmute", "ended"]) track.addEventListener(event, () => log(`${track.kind === "audio" ? "audio" : prefix}-${event}`, {label: track.label}));
    }
}
function roi(id) {
    const values = $(id).value.split(",").map(Number);
    if (values.length !== 4 || values.some(value => !Number.isInteger(value) || value < 0) || !values[2] || !values[3]) throw new Error("Invalid pixel ROI");
    if (values[0] + values[2] > captureCanvas.width || values[1] + values[3] > captureCanvas.height) throw new Error("ROI exceeds capture dimensions");
    return values;
}
function drawCapture() {
    const video = $("capture-video");
    if (!video.videoWidth) throw new Error("No decoded capture frames");
    captureCanvas.width = video.videoWidth; captureCanvas.height = video.videoHeight;
    captureContext.drawImage(video, 0, 0);
}
function region() { drawCapture(); return captureContext.getImageData(...roi("roi")); }
function difference(first, second) {
    if (first.width !== second.width || first.height !== second.height) throw new Error("Capture geometry changed; recalibrate");
    let changed = 0, black = 0;
    for (let i = 0; i < first.data.length; i += 4) {
        if (Math.max(Math.abs(first.data[i] - second.data[i]), Math.abs(first.data[i+1] - second.data[i+1]), Math.abs(first.data[i+2] - second.data[i+2]), Math.abs(first.data[i+3] - second.data[i+3])) > 0) changed++;
        if (Math.max(second.data[i], second.data[i+1], second.data[i+2]) < 8) black++;
    }
    return {changed_pixels: changed, changed_fraction: changed / (first.width * first.height), black_fraction: black / (first.width * first.height)};
}
function captureSignature() {
    return JSON.stringify({track: captureStream?.getVideoTracks()[0]?.id, settings: captureStream?.getVideoTracks()[0]?.getSettings(),
        roi: roi("roi"), beacon: roi("beacon-roi"), width: captureCanvas.width, height: captureCanvas.height, devicePixelRatio,
        viewport: [innerWidth, innerHeight, scrollX, scrollY]});
}
async function saveCanvas(canvas, kind) {
    const originAttempt = attempt, captured_ms = Date.now(), epoch = calibrationEpoch;
    const png = canvas.toDataURL("image/png").split(",")[1];
    const data = await api("/api/frame", {session, attempt: originAttempt, png});
    log("saved-frame", {kind, captured_ms, calibration_epoch: epoch, ...data}, {attempt: originAttempt});
}
function retainFrame(kind) {
    if (registering || ending) return;
    if (pendingFrames.size >= 16) { log("observer-error", {reason: "Frame evidence backpressure; transient retention incomplete"}); return; }
    const promise = saveCanvas(captureCanvas, kind).catch(error => log("observer-error", {reason: error.message}));
    pendingFrames.add(promise); promise.finally(() => pendingFrames.delete(promise));
}
function captureTick(now, metadata = {}) {
    if (!captureStream?.active) return;
    if ($("capture-video").requestVideoFrameCallback) $("capture-video").requestVideoFrameCallback(captureTick);
    else requestAnimationFrame(captureTick);
    frameCount++;
    const interval = now - lastFrameSample; lastFrameSample = now;
    try {
        drawCapture();
        if (baseline && captureSignature() !== calibrationSignature) {
            calibrated = false; baseline = null;
            log("observer-error", {reason: "Source, footprint, dimensions or viewport changed; recalibrate"});
        }
        const beacon = captureContext.getImageData(...roi("beacon-roi"));
        if (lastBeacon && difference(lastBeacon, beacon).changed_pixels > 0) beaconChanged = now;
        lastBeacon = beacon;
        if (now - beaconChanged > 3500 && attempt) log("observer-error", {reason: "Frozen/wrong capture or unmapped beacon", gap_ms: now - beaconChanged});
        log("capture-frame", {interval_ms: interval, decoded: frameCount, presented_frames: metadata.presentedFrames,
            mediaTime: metadata.mediaTime ?? $("capture-video").currentTime, width: captureCanvas.width, height: captureCanvas.height,
            beacon_age_ms: now - beaconChanged, calibration_epoch: calibrationEpoch, callback: $("capture-video").requestVideoFrameCallback ? "video" : "animation"});
        if (baseline) {
            const delta = difference(baseline, captureContext.getImageData(...roi("roi")));
            log("capture-difference", {...delta, calibrated, roi: roi("roi"), calibration_epoch: calibrationEpoch});
            if (attempt && (delta.changed_pixels > 0 || now - lastSaved > 1000)) {
                lastSaved = now; retainFrame(delta.changed_pixels ? "changed-display" : "display");
            }
        }
        lastFrame = now;
    } catch (error) { calibrated = false; log("observer-error", {reason: error.message}); }
}
function audioTick() {
    if (!analyser) return;
    const data = new Float32Array(analyser.fftSize); analyser.getFloatTimeDomainData(data);
    let sum = 0; for (const value of data) sum += value * value;
    log("audio-sample", {rms: Math.sqrt(sum / data.length), contextTime: audioContext.currentTime, state: audioContext.state, output: audioContext.getOutputTimestamp?.(), baseLatency: audioContext.baseLatency, outputLatency: audioContext.outputLatency, sample: ++audioSamples});
}
async function webRTC() {
    if (!microphoneStream) throw new Error("Acquire microphone first");
    const left = new RTCPeerConnection({iceServers: []}), right = new RTCPeerConnection({iceServers: []});
    rtcPeers.push(left, right);
    left.onicecandidate = event => { if (event.candidate) right.addIceCandidate(event.candidate).catch(error => log("rtc-error", {name: error.name})); };
    right.onicecandidate = event => { if (event.candidate) left.addIceCandidate(event.candidate).catch(error => log("rtc-error", {name: error.name})); };
    microphoneStream.getTracks().forEach(track => left.addTrack(track, microphoneStream));
    await left.setLocalDescription(await left.createOffer()); await right.setRemoteDescription(left.localDescription);
    await right.setLocalDescription(await right.createAnswer()); await left.setRemoteDescription(right.localDescription);
    log("rtc-start", {scope: "owned local peer pair, no external ICE servers"});
}
async function capabilities() {
    const features = {getDisplayMedia: !!navigator.mediaDevices?.getDisplayMedia, getUserMedia: !!navigator.mediaDevices?.getUserMedia,
        directory: !!window.showDirectoryPicker, clipboard: !!navigator.clipboard?.readText, keyboardLock: !!navigator.keyboard?.lock,
        pointerLock: !!document.body.requestPointerLock, fullscreen: document.fullscreenEnabled,
        videoFrameCallback: !!HTMLVideoElement.prototype.requestVideoFrameCallback,
        sharedArrayBuffer: typeof SharedArrayBuffer !== "undefined", crossOriginIsolated,
        networkInformation: !!navigator.connection, serviceWorker: !!navigator.serviceWorker,
        bluetooth: !!navigator.bluetooth, usb: !!navigator.usb, serial: !!navigator.serial, hid: !!navigator.hid,
        geolocation: !!navigator.geolocation, midi: !!navigator.requestMIDIAccess, sensors: "Accelerometer" in window,
        screenDetails: !!window.getScreenDetails, localFonts: !!window.queryLocalFonts, presentation: !!navigator.presentation,
        userAgent: navigator.userAgent, platform: navigator.platform, hardwareConcurrency: navigator.hardwareConcurrency,
        language: navigator.language, devicePixelRatio, screen: {width: screen.width, height: screen.height}, permissions: {}};
    for (const name of ["camera", "microphone", "clipboard-read", "geolocation", "notifications", "midi", "local-network-access", "local-network", "loopback-network", "window-management", "idle-detection", "compute-pressure"]) {
        try { features.permissions[name] = (await navigator.permissions.query({name})).state; }
        catch { features.permissions[name] = "unsupported query"; }
    }
    if (navigator.userAgentData?.getHighEntropyValues) features.uaData = await navigator.userAgentData.getHighEntropyValues(["fullVersionList", "platformVersion", "architecture"]);
    $("capabilities").textContent = JSON.stringify(features, null, 2); log("capabilities", features);
}
for (const name of ["keydown", "keyup", "keypress", "beforeinput", "input", "compositionstart", "compositionupdate", "compositionend",
    "selectionchange", "pointermove", "pointerenter", "pointerleave", "pointerover", "pointerout", "pointerdown", "pointerup", "pointercancel",
    "gotpointercapture", "lostpointercapture", "click", "dblclick", "contextmenu", "wheel", "touchstart", "touchend", "touchmove", "touchcancel"]) {
    document.addEventListener(name, event => log(name, {key: event.key, code: event.code, repeat: event.repeat, isTrusted: event.isTrusted,
        ctrl: event.ctrlKey, alt: event.altKey, shift: event.shiftKey, meta: event.metaKey, altGraph: event.getModifierState?.("AltGraph"),
        inputType: event.inputType, data: event.data, composing: event.isComposing, x: event.clientX, y: event.clientY,
        button: event.button, buttons: event.buttons, deltaX: event.deltaX, deltaY: event.deltaY, target: event.target.id || event.target.tagName,
        selection: name === "selectionchange" ? document.getSelection()?.toString() : undefined,
        text: name === "input" ? $("text-testing-area").textContent : undefined}), {capture: true, passive: true});
}
for (const name of ["focus", "blur", "resize", "pagehide"]) window.addEventListener(name, () => log(name, {hasFocus: document.hasFocus(), width: innerWidth, height: innerHeight}));
document.addEventListener("visibilitychange", () => log("visibilitychange", {state: document.visibilityState}));
document.addEventListener("fullscreenchange", () => log(document.fullscreenElement ? "fullscreen-enter" : "fullscreen-exit"));
document.addEventListener("pointerlockchange", () => log(document.pointerLockElement ? "pointerlock-acquired" : "pointerlock-lost"));
window.addEventListener("error", event => log("observer-error", {message: event.message}));
window.addEventListener("unhandledrejection", event => log("observer-error", {message: String(event.reason)}));
navigator.mediaDevices?.addEventListener("devicechange", () => log("devicechange"));
$("text-testing-area").addEventListener("pointerdown", event => { if (event.shiftKey) event.target.setPointerCapture(event.pointerId); });
worker.onmessage = event => { const now = performance.now(); log("worker-timing", {...event.data, receive_interval_ms: now - workerTimer}); workerTimer = now; };
try { new PerformanceObserver(list => { for (const item of list.getEntries()) log("resource", {name: item.name, initiatorType: item.initiatorType, duration: item.duration, transferSize: item.transferSize}); }).observe({type: "resource", buffered: true}); } catch (error) { log("unsupported", {probe: "resource timing", name: error.name}); }
try { new PerformanceObserver(list => { for (const item of list.getEntries()) log("longtask", {duration: item.duration}); }).observe({type: "longtask"}); } catch { log("unsupported", {probe: "longtask"}); }
function animation(now) { log("animation", {interval_ms: now - previousAnimation}); previousAnimation = now; requestAnimationFrame(animation); }
requestAnimationFrame(animation);
let previousTimer = performance.now();
setInterval(() => { const now = performance.now(); log("heartbeat", {hasFocus: document.hasFocus(), visibility: document.visibilityState,
    timer_delay_ms: now - previousTimer - 1000, capture_age_ms: lastFrame ? now - lastFrame : null, network: navigator.connection ? {rtt: navigator.connection.rtt, downlink: navigator.connection.downlink, type: navigator.connection.effectiveType} : null});
    previousTimer = now; $("events").textContent = evidence.slice(-25).map(event => `${event.seq} ${event.type} ${JSON.stringify(event.data)}`).join("\n");
    $("beacon").style.background = Math.floor(now / 1000) % 2 ? "#ff00ff" : "#00ff00";
    if (captureStream && attempt && now - lastFrame > 3500) log("observer-error", {reason: "Capture frame timeout"}); flush(); }, 1000);
setInterval(audioTick, 100);
setInterval(() => {
    const start = performance.now();
    for (let i = 0; i < 300; i++) { workloadContext.fillStyle = i % 2 ? "#246" : "#ace"; workloadContext.fillRect(i % 128, (i * 7) % 128, 64, 64); }
    workloadContext.getImageData(0, 0, 1, 1);
    log("render-workload", {duration: performance.now() - start, rectangles: 300});
}, 1000);
setInterval(async () => {
    if (attempt) {
        const start = performance.now(); await api("/api/ping"); log("owned-endpoint", {duration: performance.now() - start});
    }
}, 1000);
setInterval(async () => { for (const peer of rtcPeers) { const stats = await peer.getStats(); stats.forEach(item => { if (["inbound-rtp", "outbound-rtp", "media-source", "candidate-pair"].includes(item.type)) log("webrtc-stats", item); }); } }, 1000);
setInterval(async () => { if (loopbackLoad) for (let i = 0; i < 10; i++) await api("/api/ping"); }, 250);
onButton("begin", begin); onButton("review-save", review);
onButton("capture", async () => { captureStream = await navigator.mediaDevices.getDisplayMedia({video: {frameRate: {ideal: 60}}, audio: true}); trackStream(captureStream, "capture"); $("capture-video").srcObject = captureStream; await $("capture-video").play(); baseline = null; calibrated = false; beaconChanged = performance.now(); captureTick(performance.now()); });
onButton("mic", async () => { microphoneStream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: false}}); trackStream(microphoneStream, "microphone"); audioContext = new AudioContext(); analyser = audioContext.createAnalyser(); analyser.fftSize = 2048; audioContext.createMediaStreamSource(microphoneStream).connect(analyser); await audioContext.resume(); });
onButton("camera", async () => { const stream = await navigator.mediaDevices.getUserMedia({video: true}); trackStream(stream, "camera"); $("camera-video").srcObject = stream; await $("camera-video").play(); });
onButton("devices", async () => log("devices", {devices: (await navigator.mediaDevices.enumerateDevices()).map(device => device.toJSON())}));
onButton("stop-media", async () => { streams.forEach(stream => stream.getTracks().forEach(track => track.stop())); rtcPeers.forEach(peer => peer.close()); rtcPeers.length = 0; analyser = null; if (audioContext) await audioContext.close(); log("media-stopped"); });
onButton("fullscreen", () => document.documentElement.requestFullscreen());
onButton("video-fullscreen", () => $("capture-video").requestFullscreen());
onButton("pointer-lock", () => $("text-testing-area").requestPointerLock());
onButton("keyboard-lock", () => navigator.keyboard.lock());
onButton("rtc", webRTC);
onButton("clipboard", async () => log("clipboard-read", {text: await navigator.clipboard.readText()}));
let permittedDirectory = null, directoryBusy = false;
async function readDirectory() {
    if (!permittedDirectory || directoryBusy) return;
    directoryBusy = true;
    let count = 0;
    async function visit(directory, prefix = "", depth = 0) {
        if (depth > 16) throw new Error("Directory depth exceeds recorded coverage; split the fixture");
        for await (const [name, handle] of directory.entries()) {
            if (++count > 512) throw new Error("Directory entry budget exceeded; split the fixture and repeat");
            const relative = prefix + name;
            if (handle.kind === "directory") { await visit(handle, relative + "/", depth + 1); continue; }
            const file = await handle.getFile();
            const bytes = await file.slice(0, 65536).arrayBuffer();
            const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(x => x.toString(16).padStart(2, "0")).join("");
            log("directory-read", {name: relative, bytes: file.size, lastModified: file.lastModified, sha256: hash, hash_scope: "first 65536 bytes", hashed_bytes: bytes.byteLength,
                text: file.size < 65536 && name !== "config.json" ? await file.text() : null,
                retention: name === "config.json" ? "metadata/hash only; do not export encrypted credentials" : "synthetic fixture"});
        }
    }
    try { await visit(permittedDirectory); } finally { directoryBusy = false; }
}
onButton("directory", async () => {
    const directory = await window.showDirectoryPicker({mode: "read"});
    const marker = await (await directory.getFileHandle(".que-audit-fixture")).getFile();
    if ((await marker.text()).trim() !== "Synthetic Que audit directory") throw new Error("Select the staged synthetic fixture/data directory only");
    permittedDirectory = directory; await readDirectory();
});
setInterval(() => { if (attempt && permittedDirectory) readDirectory().catch(error => log("probe-error", {probe: "directory", message: error.message})); }, 3000);

onButton("speak", () => speechSynthesis.speak(new SpeechSynthesisUtterance("The green robot counts nine moons.")));
onButton("camera-frame", () => { const video = $("camera-video"), canvas = document.createElement("canvas"); canvas.width = video.videoWidth; canvas.height = video.videoHeight; canvas.getContext("2d").drawImage(video, 0, 0); return saveCanvas(canvas, "camera"); });
onButton("ping", async () => { const start = performance.now(); const result = await api("/api/ping"); log("owned-endpoint", {duration: performance.now() - start, ...result}); });
onButton("baseline", async () => {
    baseline = region(); positive = false; calibrated = false; calibrationEpoch = crypto.randomUUID(); calibrationSignature = captureSignature();
    log("capture-baseline", {roi: roi("roi"), beacon: roi("beacon-roi"), calibration_epoch: calibrationEpoch, signature: calibrationSignature});
    await saveCanvas(captureCanvas, "baseline");
});
onButton("dom-control", () => { $("pattern").classList.toggle("positive-control"); log("dom-control", {visible: $("pattern").classList.contains("positive-control")}); });
onButton("positive", async () => {
    if (!baseline || captureSignature() !== calibrationSignature) throw new Error("Save a current unobstructed baseline first");
    const delta = difference(baseline, region()); positive = delta.changed_pixels > 0;
    log("capture-positive", {...delta, positive, calibration_epoch: calibrationEpoch, control: $("pattern").classList.contains("positive-control") ? "DOM" : "native"});
    await saveCanvas(captureCanvas, "positive-control");
    if (!positive) throw new Error("Control not detected; use a DOM control for isolated tab/window capture and a native control for monitor capture");
});
onButton("negative", async () => {
    if (!positive || captureSignature() !== calibrationSignature) throw new Error("Detect a positive control in the current path first");
    const startSequence = seq;
    await new Promise(resolve => setTimeout(resolve, 1500));
    const frames = evidence.filter(e => e.seq > startSequence && e.type === "capture-difference");
    calibrated = frames.length >= 10 && frames.every(e => e.data.changed_pixels === 0) && performance.now() - beaconChanged < 3500;
    log("capture-calibration", {calibrated, stable_frames: frames.length, calibration_epoch: calibrationEpoch});
    await saveCanvas(captureCanvas, "restored-negative-control");
    if (!calibrated) throw new Error("Need ten identical baseline frames and a live beacon; inspect noise or mapping instead of relaxing the detector");
});
onButton("cpu-load", () => { workerLoad = !workerLoad; worker.postMessage({load: workerLoad}); log("cpu-control", {active: workerLoad}); });
onButton("network-load", () => { loopbackLoad = !loopbackLoad; log("network-control", {active: loopbackLoad, scope: "owned loopback only"}); });
onButton("download", () => { const url = URL.createObjectURL(new Blob([JSON.stringify({session, evidence, evicted_from_memory: evictedEvents, complete_timeline: "Use the durable browser/session.jsonl in the run directory; this download is a bounded recent window"}, null, 2)], {type: "application/json"})); const link = document.createElement("a"); link.href = url; link.download = `que-browser-${session}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); });
(async () => {
    manifest = await api("/api/manifest");
    for (const item of manifest.scenarios) { const option = document.createElement("option"); option.value = item.id; option.textContent = item.id; $("scenario").append(option); }
    const browser = new URLSearchParams(location.search).get("browser"); if (browser) $("browser").value = browser;
    $("scenario").addEventListener("change", specChanged); specChanged(); await capabilities();
    if (navigator.serviceWorker) {
        navigator.serviceWorker.addEventListener("message", event => log("service-worker", event.data));
        navigator.serviceWorker.register("/sw.js").then(() => navigator.serviceWorker.ready)
            .then(() => log("service-worker-ready", {controlled: !!navigator.serviceWorker.controller}))
            .catch(error => log("probe-error", {probe: "service-worker", message: error.message}));
    } else log("unsupported", {probe: "service-worker"});
    installResearchProbes(log, onButton, () => ({attempt, audioContext, microphoneStream}));
    log("observer-ready"); $("status").textContent = "Observer ready. Set up the selected scenario, then start measurement.";
})().catch(error => { log("logger-error", {message: error.message}); $("status").textContent = error.message; });
