(() => {
    "use strict";
    window.captureAuditSources=[];
    if(!navigator.mediaDevices?.getDisplayMedia) return;
    const original=navigator.mediaDevices.getDisplayMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getDisplayMedia=(...arguments_)=>{
        const result=original(...arguments_);
        result.then(stream=>{
            for(const track of stream.getVideoTracks()) {
                const settings=track.getSettings();
                window.captureAuditSources.push({surface:settings.displaySurface || null,
                    width:settings.width, height:settings.height, frameRate:settings.frameRate});
            }
        },()=>{});
        return result;
    };
})();
