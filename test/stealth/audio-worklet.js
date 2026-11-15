"use strict";
class AuditQuantum extends AudioWorkletProcessor {
    constructor() { super(); this.lastFrame = null; this.blocks = 0; this.sum = 0; this.samples = 0; this.gaps = 0; this.lastSize = 0; }
    process(inputs) {
        const channel = inputs[0]?.[0];
        if (this.lastFrame !== null && currentFrame - this.lastFrame !== this.lastSize) this.gaps++;
        this.lastFrame = currentFrame; this.lastSize = channel?.length || 128;
        if (channel) for (const value of channel) { this.sum += value * value; this.samples++; }
        if (++this.blocks % 32 === 0) {
            this.port.postMessage({frame: currentFrame, currentTime, sampleRate, blocks: this.blocks, gaps: this.gaps,
                samples: this.samples, rms: this.samples ? Math.sqrt(this.sum / this.samples) : null});
            this.sum = 0; this.samples = 0;
        }
        return true;
    }
}
registerProcessor("audit-quantum", AuditQuantum);
