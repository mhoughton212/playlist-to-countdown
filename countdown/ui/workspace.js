'use strict';
// Preview uses the export templates at production dimensions, scaled only at the frame.
let workspaceFrames, workspaceTimer, workspaceGeneration = 0;
let workspaceText = '', workspaceCaption = '';
let timelineDrag = null;
async function loadWorkspaceFrame(id, template) {
  const response = await fetch(`/ui/${template}`);
  if (!response.ok) throw new Error('Card template unavailable');
  let html = await response.text();
  if (template === 'video-card.html') {
    const demo = html.lastIndexOf('window.renderCard(');
    if (demo >= 0) html = html.slice(0, demo) + '</script></body></html>';
  }
  const fit = `<script>function fitWorkspaceCard(){const f=document.getElementById('frame');const s=Math.min(innerWidth/1920,innerHeight/1080);f.style.position='absolute';f.style.left=((innerWidth-1920*s)/2)+'px';f.style.top=((innerHeight-1080*s)/2)+'px';f.style.transformOrigin='top left';f.style.transform='scale('+s+')';}addEventListener('resize',fitWorkspaceCard);fitWorkspaceCard();</script>`;
  const frame = document.getElementById(id);
  const marker = `${id}-${Date.now()}-${Math.random()}`;
  const renderer = template === 'video-card.html' ? 'renderCard' : 'renderTextCard';
  // An iframe can emit its initial about:blank load before srcdoc is ready.
  // Verify the particular document and renderer, not just the load event.
  await new Promise((resolve, reject) => {
    const finish = error => {
      clearInterval(poll); clearTimeout(timeout);
      frame.removeEventListener('load', ready);
      error ? reject(error) : resolve();
    };
    const ready = () => {
      if (frame.contentWindow?.workspaceDocument === marker &&
          typeof frame.contentWindow[renderer] === 'function') {
        const document = frame.contentDocument;
        if (document && !document.body.dataset.workspaceClickBound) {
          document.body.dataset.workspaceClickBound = 'true';
          document.addEventListener('click', () => window.parent.togglePreviewPlayback?.());
        }
        finish();
      }
    };
    const poll = setInterval(ready, 40);
    const timeout = setTimeout(() => finish(new Error('Card preview did not load')), 10000);
    frame.addEventListener('load', ready);
    frame.srcdoc = html.replace('</body>', fit + `<script>window.workspaceDocument=${JSON.stringify(marker)}</script></body>`);
  });
  return frame;
}
function refreshWorkspacePreview() {
  clearTimeout(workspaceTimer);
  const generation = ++workspaceGeneration;
  workspaceTimer = setTimeout(async () => {
    const current = track();
    if (!current) return;
    const status = document.getElementById('workspace-preview-status');
    try {
      workspaceFrames ||= Promise.all([
        loadWorkspaceFrame('workspace-song-preview', 'video-card.html'),
        loadWorkspaceFrame('workspace-text-preview', 'text-card.html'),
      ]);
      const [song] = await workspaceFrames;
      if (generation !== workspaceGeneration) return;
      const sequence = state.tracks.filter(item => item.selected && item.ready);
      const trackIndex = Math.max(0, sequence.findIndex(item => item.key === current.key));
      await song.contentWindow.renderCard({
        rank: current.rank, layout: trackIndex % 2 ? 'alternate' : 'default', title: document.getElementById('card-title-input').value,
        artist: document.getElementById('card-artist-input').value,
        year: document.getElementById('track-year').value,
        description: document.getElementById('track-description').value,
        artwork: current.artwork_ready ? `/api/artwork/${encodeURIComponent(current.key)}?revision=${state.revision}` : '',
        performanceImage: current.performance_image_ready ? `/api/performance-image/${encodeURIComponent(current.key)}?revision=${state.revision}` : '',
      });
      if (generation !== workspaceGeneration) return;
      status.hidden = true;
      updateWorkspaceVisual();
    } catch {
      workspaceFrames = null;
      status.hidden = false;
      status.textContent = 'Preview unavailable. Your edits are still saved; use Open card to retry.';
    }
  }, 100);
}
function updateWorkspaceVisual() {
  const song = document.getElementById('workspace-song-preview');
  const text = document.getElementById('workspace-text-preview');
  if (!song || !text) return;
  let range;
  try { range = proposedRange(); } catch { range = clipRange; }
  const relative = audio.currentTime - range.start;
  const frames = editorFrames;
  const origin = state ? ClipTiming.compilationStart(state.tracks, activeKey, state.settings.duration, state.settings.crossfade) : 0;
  const active = ClipTiming.activeFrame(frames, relative, range.duration, origin);
  const showText = Boolean(active && text.contentWindow?.renderTextCard);
  if (song.hidden !== showText) song.hidden = showText;
  if (text.hidden === showText) text.hidden = !showText;
  if (showText && workspaceText !== active.text) {
    workspaceText = active.text;
    text.contentWindow.renderTextCard({text: active.text});
  }
  const caption = showText
    ? `Text frame · ${active.start.toFixed(2)}–${active.end.toFixed(2)}s into this clip`
    : 'Song card · scrub or play the excerpt to preview text frames';
  if (workspaceCaption !== caption) {
    workspaceCaption = caption;
    document.getElementById('workspace-preview-caption').textContent = caption;
  }
}
function rebuildWorkspaceTimeline() {
  let range; try { range = proposedRange(); } catch { range = clipRange; }
  const frames = editorFrames;
  const timeline = document.getElementById('visual-timeline');
  const ruler = document.getElementById('timeline-ruler');
  timeline.dataset.duration = String(range.duration);
  ruler.replaceChildren(...Array.from({length: 6}, (_, index) => {
    const label = document.createElement('span'); label.textContent = (range.duration * index / 5).toFixed(2) + (index === 5 ? 's' : ''); return label;
  }));
  timeline.replaceChildren();
  const valid = frames.filter(f => Number.isFinite(f.start) && Number.isFinite(f.end) && f.end > f.start)
    .sort((a, b) => a.start - b.start);
  let cursor = 0;
  function segment(start, end, isText, frameIndex = '') {
    if (end <= start) return;
    const item = document.createElement('div');
    item.className = 'visual-segment' + (isText ? ' text-segment' : '');
    item.style.flex = String(end - start);
    item.dataset.segmentStart = String(start);
    item.dataset.segmentEnd = String(end);
    item.dataset.frameIndex = String(frameIndex);
    item.title = `${isText ? 'Text frame' : 'Song card'}: ${start.toFixed(2)}–${end.toFixed(2)}s`;
    item.setAttribute('aria-label', item.title);
    const label = document.createElement('span'); label.textContent = isText ? 'Text' : 'Song card';
    item.append(label); timeline.append(item);
  }
  for (const frame of valid) {
    const start = Math.max(cursor, Math.min(range.duration, frame.start));
    const end = Math.min(range.duration, frame.end);
    segment(cursor, start, false); segment(start, end, true, frames.indexOf(frame)); cursor = Math.max(cursor, end);
  }
  segment(cursor, range.duration, false);
}

function timelineFrameItem(frameIndex) {
  return [...document.querySelectorAll('#visual-timeline .visual-segment')]
    .find(item => item.dataset.frameIndex === String(frameIndex));
}

function updateTimelineGeometry(drag, value) {
  for (const [item, edge] of [[drag.leftItem, 'end'], [drag.rightItem, 'start']]) {
    if (!item) continue;
    const start = Number(item.dataset.segmentStart);
    const end = Number(item.dataset.segmentEnd);
    if (edge === 'end') {
      item.dataset.segmentEnd = String(value);
      item.style.flex = String(Math.max(.01, value - start));
    } else {
      item.dataset.segmentStart = String(value);
      item.style.flex = String(Math.max(.01, end - value));
    }
  }
}

function timelineBoundary(event) {
  const timeline = document.getElementById('visual-timeline');
  const item = event.target.closest?.('.visual-segment');
  if (!timeline || !item) return null;
  const items = [...timeline.children];
  const index = items.indexOf(item);
  const rect = item.getBoundingClientRect();
  let leftItem, rightItem;
  if (event.clientX >= rect.right - 12) {
    leftItem = item; rightItem = items[index + 1];
  } else if (event.clientX <= rect.left + 12) {
    leftItem = items[index - 1]; rightItem = item;
  } else return null;
  if (!rightItem) return null;
  const leftIndex = leftItem?.dataset.frameIndex;
  const rightIndex = rightItem?.dataset.frameIndex;
  if (leftIndex === '' && rightIndex === '') return null;
  const currentTrack = typeof track === 'function' ? track() : null;
  return {timeline, leftItem, rightItem,
    leftIndex: leftIndex === '' ? null : Number(leftIndex),
    rightIndex: rightIndex === '' ? null : Number(rightIndex),
    duration: currentTrack?.visual_frames ? (typeof clipRange !== 'undefined' ? clipRange.duration : 0) : 0};
}

function timelineValue(event, timeline) {
  const track = timeline.getBoundingClientRect();
  const duration = Number(timeline.dataset.duration || 0);
  return Math.round(Math.max(0, Math.min(duration, (event.clientX - track.left) / track.width * duration)) * 100) / 100;
}

const timelineElement = document.getElementById('visual-timeline');
if (timelineElement) {
  timelineElement.addEventListener('pointerdown', event => {
    const boundary = timelineBoundary(event);
    const t = typeof track === 'function' ? track() : null;
    if (!boundary || !t) return;
    const left = boundary.leftIndex === null ? null : editorFrames[boundary.leftIndex];
    const right = boundary.rightIndex === null ? null : editorFrames[boundary.rightIndex];
    const min = left ? left.start + .01 : 0;
    const max = right ? right.end - .01 : (typeof clipRange !== 'undefined' ? clipRange.duration : 0);
    timelineDrag = {...boundary, min, max, pointerId: event.pointerId};
    timelineElement.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  timelineElement.addEventListener('pointermove', event => {
    if (!timelineDrag || timelineDrag.pointerId !== event.pointerId) return;
    const t = typeof track === 'function' ? track() : null;
    const value = Math.max(timelineDrag.min, Math.min(timelineDrag.max,
      timelineValue(event, timelineDrag.timeline)));
    if (timelineDrag.leftIndex !== null) editorFrames[timelineDrag.leftIndex].end = value;
    if (timelineDrag.rightIndex !== null) editorFrames[timelineDrag.rightIndex].start = value;
    updateTimelineGeometry(timelineDrag, value);
    updateWorkspaceVisual();
    event.preventDefault();
  });
  const finishTimelineDrag = event => {
    if (!timelineDrag || timelineDrag.pointerId !== event.pointerId) return;
    const t = typeof track === 'function' ? track() : null;
    timelineDrag = null;
    if (t && typeof renderTextFrames === 'function') {
      renderTextFrames();
      edited?.('visual-frame');
    }
  };
  timelineElement.addEventListener('pointerup', finishTimelineDrag);
  timelineElement.addEventListener('pointercancel', finishTimelineDrag);
}
