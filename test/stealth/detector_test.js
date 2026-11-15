"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm"), path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "observer.js"), "utf8");
const difference = source.slice(source.indexOf("function difference("), source.indexOf("function captureSignature("));
const context = vm.createContext({});
vm.runInContext(difference, context);
const first = {width: 1000, height: 1000, data: new Uint8ClampedArray(4000000)}, second = {...first, data: first.data.slice()};
second.data[12345] = 1;
const delta = context.difference(first, second);
assert.equal(delta.changed_pixels, 1);
assert.equal(delta.changed_fraction, 0.000001);
assert.equal(context.difference(first, first).changed_pixels, 0);
second.data.fill(0); second.data[12347] = 1;
assert.equal(context.difference(first, second).changed_pixels, 1);
assert.throws(() => context.difference(first, {...second, width: 1}));
// Every decoded callback must be analyzed even while image retention is pending.
const tick = source.slice(source.indexOf("function captureTick("), source.indexOf("function audioTick("));
let requested = 0, frames = 0, retained = 0;
const pixels = {width: 1, height: 1, data: new Uint8ClampedArray(4)};
const tickContext = vm.createContext({captureStream: {active: true}, $: () => ({requestVideoFrameCallback: () => requested++}),
    frameCount: 0, lastFrameSample: 0, lastFrame: 0, baseline: pixels, calibrated: true,
    calibrationSignature: "same", captureSignature: () => "same", calibrationEpoch: "fixture",
    drawCapture: () => {}, captureContext: {getImageData: () => pixels}, captureCanvas: {width: 1, height: 1},
    roi: () => [0, 0, 1, 1], lastBeacon: pixels, beaconChanged: 0, attempt: "fixture", lastSaved: 0,
    difference: () => ({changed_pixels: 1}), retainFrame: () => { retained++; return new Promise(() => {}); },
    log: type => { if (type === "capture-frame") frames++; }});
vm.runInContext(tick, tickContext);
for (let index = 1; index <= 60; index++) tickContext.captureTick(index * 16, {mediaTime: index / 60, presentedFrames: index});
assert.equal(frames, 60); assert.equal(requested, 60); assert.equal(retained, 60);
console.log("Single-pixel and unthrottled decoded-frame detector checks passed.");
