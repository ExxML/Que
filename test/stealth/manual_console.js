// Console helpers for guided stealth runs; see test/STEALTH_MANUAL_TESTING.md.
// Paste into an undocked DevTools console on the observer page (type "allow pasting"
// first if Edge asks). Edit SETUP_NOTES to describe the real setup before pasting.
// Never define a global named `roi`: the observer page uses that name.

const SETUP_NOTES = "REAL SETUP: <browser + version>, <displays and scaling>, <window mode, zoom>. Controls driven from undocked DevTools console.";
const CONFIG = {browser: "edge", display: "single-100", placement: "same-monitor", window_mode: "maximized", exclusion: "true", repeat: "1"};

const $q = id => document.getElementById(id);

// Pin the status bar and the instructions box to fixed heights. Their changing heights
// shift the page and silently discard the capture calibration (harness bug H-011).
$q("status").style.cssText += ";position:fixed;top:auto;bottom:0;left:0;right:0;height:60px";
$q("instructions").style.cssText += ";height:150px;overflow:auto";

// Fill the attempt's configuration form.
function cfg() {
    for (const [key, value] of Object.entries(CONFIG)) $q(key).value = value;
    $q("confirmed").checked = true;
    $q("config-notes").value = SETUP_NOTES;
}

// Set the footprint and beacon ROIs ("x,y,w,h" in captured-video pixels); resets the
// beacon comparison so a new beacon size does not break it (harness bug H-010).
function setroi(footprint, beacon) {
    $q("roi").value = footprint;
    $q("beacon-roi").value = beacon;
    lastBeacon = null;
    return [roi("roi"), roi("beacon-roi")];
}

// Click a calibration button: "baseline", "positive" or "negative".
function cal(button) { $q(button).click(); return "clicked " + button; }

// Report whether the calibration is current; must be {calibrated: true, same: true} before a capture attempt.
function chk() { return {calibrated, same: captureSignature() === calibrationSignature}; }

// Start an attempt. Capture attempts need a current calibration; pass false for key-only attempts.
function go(id, secs = 15, needCalibration = true) {
    $q("scenario").value = id;
    if ($q("scenario").value !== id) return "BAD ID: " + id;
    $q("scenario").dispatchEvent(new Event("change"));
    cfg();
    $q("capture_source").value = "monitor";
    $q("seconds").value = secs;
    if (needCalibration && !(calibrated && captureSignature() === calibrationSignature)) return "NOT CALIBRATED";
    $q("begin").click();
    return "COUNTDOWN: click the page (away from Que) now";
}

// Start an audio attempt; the page turns on its microphone at micAt s (null: never) and
// speaks its phrase at speakAt s, both counted from the start of MEASURING.
function aud(id, micAt, speakAt, secs = 45) {
    if (micAt !== null) setTimeout(() => $q("mic").click(), (5 + micAt) * 1000);
    setTimeout(() => $q("speak").click(), (5 + speakAt) * 1000);
    return go(id, secs);
}

// Save the (blank) evidence review after an attempt; the controls stay locked until you do.
function save() { $q("review-save").click(); return "saved"; }

// Return the status bar text.
function st() { return $q("status").textContent; }

// List scenario IDs containing a substring, e.g. ids("AutoResizeTextEdit").
function ids(part) { return [...$q("scenario").options].map(option => option.value).filter(value => value.includes(part)); }
