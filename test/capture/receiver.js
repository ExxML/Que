async ({selector, canvasFPS}) => {
    const candidates = [...document.querySelectorAll(selector)].filter(element =>
        element instanceof HTMLCanvasElement || (element instanceof HTMLVideoElement && element.readyState >= 2 && element.videoWidth >= 450));
    if(candidates.length !== 1) throw new Error(`Receiver selector matched ${candidates.length} usable media surfaces; choose the shared content explicitly`);
    const element = candidates[0];
    const original = element instanceof HTMLVideoElement && element.srcObject instanceof MediaStream ? element.srcObject : null;
    const stream = original || element.captureStream(canvasFPS);
    if(!stream.getVideoTracks().length) throw new Error("Receiver has no video track");
    const codecs = stream.getAudioTracks().length ? ["video/webm;codecs=vp8,opus", "video/webm;codecs=vp9,opus"] : ["video/webm;codecs=vp8", "video/webm;codecs=vp9"];
    const mime = [...codecs,"video/webm"].find(value=>MediaRecorder.isTypeSupported(value));
    if(!mime) throw new Error("Receiver has no supported recording codec");
    const state = {mime, frames:0, skipped:0, muted:0, ended:0, chunks:0, bytes:0, errors:[], maxGap:0, canvas:element instanceof HTMLCanvasElement, tracks:stream.getTracks().map(t=>({kind:t.kind,settings:t.getSettings()}))};
    let pending = Promise.resolve(), previous=0, presented=0, stopped=false;
    const recorder = new MediaRecorder(stream, {mimeType:mime, videoBitsPerSecond:12000000});
    for(const track of stream.getTracks()) {
        track.addEventListener("mute",()=>state.muted++);
        track.addEventListener("ended",()=>state.ended++);
    }
    function frame(now, meta) {
        if(stopped) return;
        if(previous) state.maxGap=Math.max(state.maxGap,now-previous);
        if(presented && meta.presentedFrames>presented+1) state.skipped+=meta.presentedFrames-presented-1;
        previous=now; presented=meta.presentedFrames; state.frames++;
        element.requestVideoFrameCallback(frame);
    }
    if(element instanceof HTMLVideoElement && element.requestVideoFrameCallback) element.requestVideoFrameCallback(frame);
    recorder.addEventListener("error",event=>state.errors.push(event.error?.message || "Recorder error"));
    recorder.addEventListener("dataavailable",event=>{
        if(!event.data.size) return;
        state.chunks++; state.bytes+=event.data.size;
        pending=pending.then(async()=>{
            const buffer=new Uint8Array(await event.data.arrayBuffer());
            let binary="";
            for(let offset=0;offset<buffer.length;offset+=32768) binary+=String.fromCharCode(...buffer.subarray(offset,offset+32768));
            await window.captureAuditChunk(btoa(binary));
        }).catch(error=>{state.errors.push(error.message);});
    });
    window.captureAuditStop=async()=>{
        await new Promise(resolve=>{recorder.addEventListener("stop",resolve,{once:true});recorder.stop();});
        stopped=true; await pending;
        if(!original) stream.getTracks().forEach(track=>track.stop());
        return state;
    };
    recorder.start(1000);
    return {mime, canvas:state.canvas};
}
