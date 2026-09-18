// One media clock. Cue boundaries use nearest 30-fps frames (ties round up),
// matching studio.video_frame; elapsed time itself is never rounded.
(function(root) {
  const fps = 30;
  const frame = seconds => Math.floor(seconds * fps + .5 + 1e-9);
  function activeFrame(frames, relative, duration, origin = 0) {
    if (relative < 0 || relative >= duration) return null;
    const position = Math.floor((origin + relative) * fps + 1e-9);
    return frames.find(cue => frame(origin + cue.start) <= position && position < frame(origin + cue.end)) || null;
  }
  function compilationStart(tracks, key, defaultDuration, crossfade) {
    let total = 0, count = 0;
    for (const track of tracks) {
      if (!track.ready || !track.selected) continue;
      const length = Math.round((track.duration ?? defaultDuration) * 1000);
      const overlap = count ? Math.max(0, Math.min(Math.round(crossfade * 1000), length - 1, total - 1)) : 0;
      const start = total - overlap;
      if (track.key === key) return start / 1000;
      total = start + length; count++;
    }
    return 0;
  }
  function createPlayback(media, getRange, shouldStop, present) {
    let animation = null, boundary = null;
    function arm() {
      const end = Math.min(getRange().end, Number.isFinite(media.duration) ? media.duration : Infinity);
      boundary = shouldStop() && media.currentTime < end ? end : null;
    }
    function update() {
      if (!media.seeking && boundary !== null && media.currentTime >= boundary) {
        const end = boundary; boundary = null;
        media.pause(); media.currentTime = end;
      }
      present();
    }
    function tick() {
      animation = null; update();
      if (!media.paused && !media.ended) animation = requestAnimationFrame(tick);
    }
    function start() {
      if (animation === null) animation = requestAnimationFrame(tick);
    }
    function pause() { media.pause(); }
    async function play() {
      if (!media.src || media.readyState < 1) return;
      arm(); await media.play();
    }
    async function seek(seconds, autoplay = false) {
      if (!Number.isFinite(seconds) || media.readyState < 1) return;
      media.currentTime = Math.max(0, Math.min(seconds, Number.isFinite(media.duration) ? media.duration : seconds));
      arm(); present();
      if (autoplay) await play();
    }
    media.addEventListener('play', () => { arm(); start(); });
    media.addEventListener('playing', start);
    media.addEventListener('pause', () => { cancelAnimationFrame(animation); animation = null; present(); });
    media.addEventListener('seeking', () => { arm(); present(); });
    media.addEventListener('seeked', update);
    media.addEventListener('timeupdate', update); // Also enforce end in background tabs.
    media.addEventListener('ended', () => { boundary = null; update(); });
    media.addEventListener('emptied', () => { boundary = null; cancelAnimationFrame(animation); animation = null; present(); });
    document.addEventListener('visibilitychange', () => { update(); if (!media.paused) start(); });
    return {play, pause, seek, toggle: () => media.paused ? play() : pause(),
      rangeChanged: () => { arm(); update(); }};
  }
  const api = {fps, frame, activeFrame, compilationStart, createPlayback};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ClipTiming = api;
})(typeof window !== 'undefined' ? window : this);
