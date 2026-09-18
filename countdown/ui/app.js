'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="studio-token"]').content;
let state, activeStep = 1, activeKey = null, peaks = [], waveGeneration = 0;
let saveTimer = null, dirty = false, saveChain = Promise.resolve(), noticeTimer = null, noticeFadeTimer = null;
let pollTimer = null, authTimer = null, lastExportSignature = '', renderedAudio = '', transitionUrl = '', previewContextTimer = null;
let thumbnailPromise = null;
let clipRange = {start: 0, end: 10, duration: 10}, pendingRangeField = null, lengthLocked = true;
let editorFrames = [], editVersion = 0;
let dragState = null, stopAtClipEnd = true, visualDetailsOpen = true;
try { lengthLocked = localStorage.getItem('countdown-length-locked') !== 'false'; } catch {}
try { stopAtClipEnd = localStorage.getItem('countdown-stop-at-end') !== 'false'; } catch {}
const audio = $('song-audio');
const playback = ClipTiming.createPlayback(audio,
  () => { try { return proposedRange(); } catch { return clipRange; } },
  () => stopAtClipEnd, () => {
    $('playhead').textContent = `At ${time(audio.currentTime, true)}`;
    updateWorkspaceVisual();
  });

function notice(message, error = false) {
  clearTimeout(noticeTimer);
  clearTimeout(noticeFadeTimer);
  $('notice').classList.remove('is-dismissing');
  $('notice').classList.toggle('banner-error', error);
  $('notice').classList.toggle('banner-note', !error);
  $('notice-glyph').textContent = error ? '!' : '◈';
  $('notice-title').textContent = error ? 'Needs attention' : 'Saved locally';
  $('notice-message').textContent = message;
  $('notice').hidden = !message;
  if (message) noticeTimer = setTimeout(dismissNoticeWithFade, 5000);
}
function setSaveStatus(status) {
  const labels = {saving: ['Saving locally…', 'Saving…'], saved: ['Changes saved locally', 'Saved'], error: ['Changes not saved', 'Not saved']};
  const [headerLabel, editorLabel] = labels[status] || labels.saved;
  $('save-status').textContent = headerLabel;
  const indicator = $('editor-save-status');
  if (indicator) {
    indicator.hidden = false;
    indicator.textContent = editorLabel;
    indicator.classList.toggle('saving', status === 'saving');
    indicator.classList.toggle('error', status === 'error');
    indicator.classList.toggle('saved', status === 'saved');
  }
}
function dismissNoticeWithFade() {
  if ($('notice').hidden) return;
  $('notice').classList.add('is-dismissing');
  noticeFadeTimer = setTimeout(() => {
    $('notice').classList.remove('is-dismissing');
    $('notice').hidden = true;
    $('notice-message').textContent = '';
  }, 440);
}
function dismissNotice() {
  clearTimeout(noticeTimer);
  clearTimeout(noticeFadeTimer);
  $('notice').classList.remove('is-dismissing');
  $('notice').hidden = true;
  $('notice-message').textContent = '';
}
async function api(path, data, form = false) {
  const options = data === undefined ? {} : {method: 'POST', headers: {'X-Studio-Token': token}, body: form ? data : JSON.stringify(data)};
  if (data !== undefined && !form) options.headers['Content-Type'] = 'application/json';
  const response = await fetch('/api' + path, options);
  let result;
  try { result = await response.json(); } catch { throw new Error('The server could not complete that request. Check that Playlist to Countdown is still running.'); }
  if (!response.ok) {
    const error = new Error(result.error || 'Please connect to Spotify to continue.');
    error.data = result;
    throw error;
  }
  return result;
}
function setBusy(value) {
  ['album-artwork-url', 'save-album-artwork', 'album-artwork-upload', 'restore-album-artwork', 'performance-image-url', 'save-performance-image', 'performance-image-upload', 'remove-performance-image'].forEach(id => { if ($(id)) $(id).disabled = value; });
  document.querySelectorAll('.media-group').forEach(group => group.setAttribute('aria-busy', String(value)));
}
function track() { return state?.tracks.find(t => t.key === activeKey); }
function isYouTubeUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && ['youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'].includes(url.hostname);
  } catch { return false; }
}
function transitionNeighbors() {
  const index = state?.tracks.findIndex(t => t.key === activeKey);
  if (index === undefined || index < 0) return {previous: null, following: null};
  return {
    previous: state.tracks.slice(0, index).reverse().find(t => t.selected && t.ready) || null,
    following: state.tracks.slice(index + 1).find(t => t.selected && t.ready) || null,
  };
}
function time(value, precision = false) {
  value = Number.isFinite(value) ? Math.max(0, value) : 0;
  if (precision) value = Math.round(value * 100) / 100;
  const minutes = Math.floor(value / 60);
  const secs = precision ? (value % 60).toFixed(2) : Math.floor(value % 60).toString();
  return `${minutes}:${secs.padStart(precision ? 5 : 2, '0')}`;
}
function elapsed(value) {
  value = Math.max(0, Number(value) || 0);
  const minutes = Math.floor(value / 60), seconds = value - minutes * 60;
  return `${minutes}:${seconds.toFixed(1).padStart(4, '0')}`;
}
function inputSeconds(value) { return Number.isFinite(value) ? String(Math.round(value * 1000) / 1000) : '0'; }
function parseStart(value) {
  const parts = String(value || '0').trim().split(':');
  if (parts.length > 3 || parts.some(p => p.trim() === '' || !Number.isFinite(Number(p)) || Number(p) < 0)) throw new Error('Use a start time such as 1:23.5.');
  return parts.reduce((sum, p) => sum * 60 + Number(p), 0);
}
function duration() { return Number($('clip-duration').value || state.settings.duration); }
function element(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}
function artworkThumb(t, className = 'song-art') {
  const thumb = element('span', undefined, className);
  if (t.artwork_ready) {
    const image = element('img');
    image.src = `/api/artwork/${encodeURIComponent(t.key)}?revision=${encodeURIComponent(t.artwork_override_file || t.artwork_file || '')}`;
    image.alt = '';
    thumb.append(image);
  } else {
    thumb.append(element('span', 'No image', 'artwork-missing'));
  }
  return thumb;
}
function applyState(next, updateEditor = false) {
  state = next;
  if (!state.tracks.some(t => t.key === activeKey)) activeKey = state.tracks.find(t => !t.selected)?.key || state.tracks[0]?.key || null;
  renderSummary();
  renderSongs();
  renderDownloads();
  renderJob();
  renderExports();
  if (updateEditor && activeKey) renderEditor();
}
function renderSummary() {
  const total = state.tracks.length, ready = state.tracks.filter(t => t.ready).length, kept = state.tracks.filter(t => t.selected).length;
  const busy = state.job.running;
  $('workspace-project').textContent = 'Your countdown';
  $('workspace-total').textContent = `${total} songs`;
  $('step-1-detail').textContent = total ? `${total} songs loaded` : 'Enter a Spotify playlist URL';
  $('step-2-detail').textContent = total ? `${ready} of ${total} ready` : 'Download audio';
  $('step-3-detail').textContent = total ? `${kept} of ${total} included` : 'Select clip ranges';
  $('step-4-detail').textContent = state.exports.final ? 'Video available' : 'Build the video';
  document.querySelectorAll('[data-step]').forEach(button => {
    const n = Number(button.dataset.step);
    button.disabled = n > 1 && !total;
    button.classList.toggle('active', n === activeStep);
    button.classList.toggle('complete', (n === 1 && total > 0) || (n === 2 && total > 0 && ready === total) || (n === 3 && total > 0 && kept === total));
    if (n === activeStep) button.setAttribute('aria-current', 'step'); else button.removeAttribute('aria-current');
  });
  $('audio-count').textContent = `${ready} of ${total} songs ready.`;
  $('download-all').textContent = ready === total ? 'All audio is ready' : `Download ${total - ready} missing song${total - ready === 1 ? '' : 's'}`;
  $('download-all').disabled = busy || ready === total;
  $('selection-count').textContent = `${kept} / ${total}`;
  $('selection-progress').max = Math.max(1, total);
  $('selection-progress').value = kept;
  $('rank-range').textContent = total ? `#${state.tracks[0].rank} → #${state.tracks.at(-1).rank}` : '';
  const previewContext = state.settings.preview_context ?? 4;
  $('settings-summary').textContent = `${state.settings.duration}s clips · ${state.settings.crossfade}s crossfade · ${previewContext}s neighbor preview · video export`;
  if ($('video-animation')) $('video-animation').checked = state.settings.video_animation ?? false;
  if ($('video-zoom')) { $('video-zoom').value = Math.round((state.settings.video_zoom ?? .05) * 100); $('video-zoom-value').textContent = `${$('video-zoom').value}%`; }
  if ($('video-song-count')) $('video-song-count').value = state.settings.video_preview_count ?? 0;
  $('clip-duration').value = $('clip-duration').dataset.usesDefault === 'true' ? String(state.settings.duration) : $('clip-duration').value;
  const missingArtwork = state.tracks.filter(t => t.selected && t.ready && !t.artwork_ready);
  $('export-readiness').textContent = `${kept} of ${total} clips included · ${ready} of ${total} audio files ready` + (ready < total ? ` · ${total - ready} missing songs will be skipped` : '');
  const artworkWarning = $('artwork-warning');
  artworkWarning.hidden = !missingArtwork.length;
  artworkWarning.textContent = missingArtwork.length
     ? `${missingArtwork.length} included song${missingArtwork.length === 1 ? '' : 's'} ${missingArtwork.length === 1 ? 'has' : 'have'} no album artwork. Video cards will use a clean background: ${missingArtwork.map(t => `#${t.rank} ${t.title}`).join(', ')}.`
    : '';
  let estimate = 0;
  state.tracks.filter(t => t.ready).forEach((t, i) => { const length = t.duration || state.settings.duration; estimate += length - (i ? Math.min(state.settings.crossfade, Math.max(0, length - .001), Math.max(0, estimate - .001)) : 0); });
  $('runtime').textContent = time(estimate);
  if ($('build-video')) $('build-video').disabled = busy || !state.tracks.some(t => t.ready && t.selected);
  $('load-playlist').disabled = busy;
  $('new-project-note').textContent = total ? 'Loading a playlist starts fresh clip choices. Downloaded audio can be reused.' : 'Start fresh. Previously downloaded audio can be reused.';
  $('footer-count').textContent = total ? `${total} songs · ${kept} clips included` : '';
  ['clip-start', 'clip-end', 'clip-duration', 'length-lock', 'stop-at-end', 'source-url', 'upload-audio', 'replace-audio-button', 'performance-image-url', 'save-performance-image', 'performance-image-upload', 'remove-performance-image', 'default-duration', 'crossfade', 'card-title-input', 'card-artist-input', 'track-year', 'track-description', 'add-text-frame', 'album-artwork-url', 'save-album-artwork', 'album-artwork-upload', 'restore-album-artwork', 'preview-card', 'set-start', 'set-end', 'preview-clip', 'previous-track', 'next-track'].forEach(id => { if ($(id)) $(id).disabled = busy; });
  $('text-frame-list').querySelectorAll('input, textarea, button').forEach(control => { control.disabled = busy; });
  $('stop-at-end').checked = stopAtClipEnd;
  const transitionButton = $('preview-transition');
  if (transitionButton) {
    const neighbors = transitionNeighbors();
    const hasNeighbors = Boolean(neighbors.previous && neighbors.following);
    transitionButton.disabled = busy || !track()?.ready || !hasNeighbors;
    $('transition-hint').hidden = hasNeighbors;
    $('transition-hint').textContent = hasNeighbors
      ? `Uses ${previewContext} seconds of ${neighbors.previous.title} before and ${previewContext} seconds of ${neighbors.following.title} after with the saved crossfade.`
      : 'Available only when clips exist before and after the current clip.';
  }
  const replaceButton = $('replace-audio-button');
  if (replaceButton) {
    const current = track(), validLink = isYouTubeUrl(current?.source_url || '');
    replaceButton.disabled = busy || !validLink;
    replaceButton.textContent = current?.ready ? 'Use YouTube audio' : 'Download audio';
    $('source-url-hint').textContent = validLink
      ? 'Link saved locally. Select Use YouTube audio to replace the current audio.'
      : 'Paste a full YouTube link. It saves automatically, then use the button to replace the audio.';
  }
  if (track()) {
    $('editor-save-status').hidden = false;
    $('previous-track').hidden = false;
    $('next-track').hidden = false;
    const currentIndex = state.tracks.findIndex(t => t.key === activeKey);
    $('previous-track').disabled = busy || currentIndex <= 0;
    $('next-track').disabled = busy || currentIndex >= state.tracks.length - 1;
    $('preview-clip').disabled = busy || !track().ready;
    $('set-start').disabled = busy || !track().ready;
    $('set-end').disabled = busy || !track().ready;
  }
}
const songFilters = [
  ['all', 'All songs', () => true],
  ['needs_attention', 'Needs attention', t => !t.year || !t.description?.trim() || !t.performance_image_ready || !t.artwork_ready || !t.ready],
  ['missing_year', 'Missing year', t => !t.year],
  ['missing_description', 'Missing description', t => !t.description?.trim()],
  ['missing_performance', 'Missing performance image', t => !t.performance_image_ready],
  ['missing_artwork', 'Missing album artwork', t => !t.artwork_ready],
  ['missing_audio', 'Missing audio', t => !t.ready],
  ['included', 'Included', t => t.selected],
  ['excluded', 'Excluded', t => !t.selected],
];
function renderSongs() {
  const list = $('song-list'), query = $('song-search').value.toLocaleLowerCase();
  const selectedFilter = $('song-filter').value || 'all';
  const filterDefinition = songFilters.find(([value]) => value === selectedFilter) || songFilters[0];
  const options = songFilters.map(([value, label, matches]) => {
    const option = document.createElement('option');
    option.value = value; option.textContent = `${label} (${state.tracks.filter(matches).length})`;
    option.selected = value === filterDefinition[0];
    return option;
  });
  $('song-filter').replaceChildren(...options);
  const visible = state.tracks.filter(t => filterDefinition[2](t) &&
    `${t.artist} ${t.title} ${t.card_artist || ''} ${t.card_title || ''} ${t.year || ''} ${t.rank}`.toLocaleLowerCase().includes(query));
  list.replaceChildren();
  for (const t of visible) {
    const button = element('button', undefined, 'song' + (t.key === activeKey ? ' active' : ''));
    button.type = 'button'; button.dataset.key = t.key;
    button.title = `${t.title} — ${t.artist}`;
    button.setAttribute('aria-label', `Number ${t.rank}, ${t.title}, ${t.artist}, ${!t.ready ? 'audio missing' : t.selected ? 'included' : 'excluded'}`);
    if (t.key === activeKey) button.setAttribute('aria-current', 'true');
    const info = element('span', undefined, 'song-info');
    info.append(element('span', t.title, 'song-title'), element('small', t.artist));
     button.append(element('span', t.rank, 'rank'), artworkThumb(t), info, element('span', !t.ready ? '!' : t.selected ? '✓' : '○', 'song-status ' + (!t.ready ? 'missing' : t.selected ? 'ready' : 'muted')));
    button.addEventListener('click', () => guard(() => selectTrack(t.key)));
    list.append(button);
  }
  if (!list.children.length) list.append(element('p', 'No matching songs.', 'section-note muted'));
  $('filter-match-count').textContent = `${visible.length} ${visible.length === 1 ? 'song' : 'songs'} shown`;
}
function renderDownloads() {
  $('download-list').replaceChildren();
  for (const t of state.tracks) {
    const row = element('tr'), info = element('td');
    info.append(element('span', t.title), element('small', t.artist));
    const action = element('td'), button = element('button', t.ready ? 'Listen' : 'Add audio', 'secondary');
    button.addEventListener('click', () => guard(async () => { await selectTrack(t.key); await showStep(3); }));
    action.append(button);
    row.append(element('td', `#${t.rank}`), info, element('td', t.ready ? '✓ Ready' : 'Missing', t.ready ? 'ready' : 'missing'), action);
    $('download-list').append(row);
  }
}
function renderEditor() {
  const t = track();
  if (!t) return;
  stopTransitionPreview();
  $('track-title').textContent = t.title;
  $('track-artist').textContent = t.artist;
  const artwork = $('track-artwork'), artworkPlaceholder = $('record-art').querySelector('small');
  const artworkSource = t.artwork_ready ? `/api/artwork/${encodeURIComponent(t.key)}?revision=${encodeURIComponent(t.artwork_override_file || t.artwork_file || '')}` : '';
  artwork.hidden = !artworkSource;
  artworkPlaceholder.hidden = Boolean(artworkSource);
  artwork.alt = artworkSource ? `Album artwork for ${t.title}` : '';
  if (artwork.src !== new URL(artworkSource || '/', window.location.href).href) artwork.src = artworkSource;
  $('album-artwork-url').value = t.artwork_override_url || '';
  $('album-artwork-status').textContent = t.artwork_override_file ? 'Custom album cover saved locally.' : 'Using the existing album artwork.';
  $('restore-album-artwork').hidden = !t.artwork_override_file;
  $('card-title-input').value = t.card_title ?? t.title ?? '';
  $('card-artist-input').value = t.card_artist ?? t.primary_artist ?? t.artist?.split(',')[0].trim() ?? '';
  $('track-year').value = t.year || '';
  $('track-description').value = t.description || '';
  $('visual-frames-details').open = visualDetailsOpen;
  $('visual-frames-toggle').textContent = $('visual-frames-details').open ? 'Hide details' : 'Show details';
  $('audio-replacement-status').textContent = t.ready ? 'Current audio is ready. Replace it only if you need a different source.' : 'This song is missing audio. Add a local MP3 or use a YouTube link.';
  editorFrames = structuredClone(t.visual_frames || []);
  renderTextFrames();
  $('performance-image-url').value = t.performance_image_url || '';
  $('performance-image-status').textContent = t.performance_image_ready ? 'Performance image saved locally.' : 'No performance image saved.';
  $('remove-performance-image').hidden = !t.performance_image_ready;
  $('editor-rank').textContent = `RANK #${t.rank}`;
  for (const [id, src] of [['album-thumb', artworkSource], ['performance-thumb', t.performance_image_ready ? `/api/performance-image/${encodeURIComponent(t.key)}?revision=${state.revision}` : '']]) {
    const thumb = $(id); thumb.replaceChildren();
    thumb.classList.toggle('empty', !src);
    if (src) { const image = document.createElement('img'); image.src = src; image.alt = ''; thumb.append(image); }
    else thumb.textContent = id === 'performance-thumb' ? '' : '▧';
  }
  $('editor-save-status').hidden = false;
  $('audio-replacement').open = !t.ready;
   $('clip-start').value = inputSeconds(t.start);
  $('clip-duration').value = String(t.duration ?? state.settings.duration);
  $('clip-duration').dataset.usesDefault = String(t.duration == null);
  $('clip-start').step = $('clip-end').step = $('clip-duration').step = '0.001';
  clipRange = {start: t.start, end: t.start + (t.duration || state.settings.duration), duration: t.duration || state.settings.duration};
  pendingRangeField = null;
   $('clip-end').value = inputSeconds(clipRange.end);
  renderLengthLock();
  $('source-url').value = t.source_url || '';
  $('player-section').hidden = !t.ready;
  $('missing-audio').hidden = t.ready;
  const source = t.ready ? `/api/audio/${encodeURIComponent(t.key)}?revision=${t.audio_revision || 0}` : '';
  if (source !== renderedAudio) {
    playback.pause(); renderedAudio = source;
    if (source) audio.src = source; else audio.removeAttribute('src');
    audio.load(); peaks = []; const generation = ++waveGeneration;
    $('wave-status').hidden = false; $('wave-status').textContent = 'Loading waveform…';
    if (t.ready) api(`/wave/${encodeURIComponent(t.key)}`).then(data => {
      if (generation !== waveGeneration) return;
      peaks = data.peaks; $('wave-status').hidden = true; drawWave();
    }).catch(() => { if (generation === waveGeneration) $('wave-status').textContent = 'Waveform unavailable. Use the player below.'; });
  }
  updateClipLabel(); drawWave();
  refreshWorkspacePreview(); rebuildWorkspaceTimeline(); updateWorkspaceVisual();
}
function frameSeconds(value) { return Number.isFinite(Number(value)) ? (Math.round(Number(value) * 100) / 100).toFixed(2) : '0.00'; }
function renderTextFrames() {
  const list = $('text-frame-list'); if (!list) return;
  list.replaceChildren();
  const frames = editorFrames;
  if (!frames.length) { list.append(element('div', 'The song card is shown for this whole clip.', 'text-frame-empty')); return; }
  frames.forEach((frame, index) => {
    const item = element('div', undefined, 'text-frame-item');
    const textLabel = element('label', 'Text');
    const textarea = document.createElement('textarea'); textarea.rows = 3; textarea.maxLength = 2000; textarea.value = frame.text || ''; textarea.dataset.frameIndex = index; textarea.dataset.frameField = 'text';
    textLabel.append(textarea);
    const times = element('div', undefined, 'text-frame-times');
    for (const [field, label] of [['start', 'Starts at'], ['end', 'Ends at']]) {
      const timeLabel = element('label', label); const input = document.createElement('input'); input.type = 'number'; input.min = '0'; input.step = '.01'; input.inputMode = 'decimal'; input.value = frameSeconds(frame[field]); input.dataset.frameIndex = index; input.dataset.frameField = field; timeLabel.append(input); times.append(timeLabel);
    }
    const remove = element('button', '×', 'secondary'); remove.setAttribute('aria-label', `Remove text frame ${index + 1}`); remove.title = 'Remove'; remove.type = 'button'; remove.dataset.removeFrame = index; times.append(remove); item.append(textLabel, times); list.append(item);
  });
}
function editorTextFrames() { return structuredClone(editorFrames); }
function visualFrameValidationError(frames = editorTextFrames()) {
  let duration; try { duration = proposedRange().duration; } catch { duration = clipRange.duration; }
  const ordered = [...frames].sort((a, b) => Number(a.start) - Number(b.start));
  for (const frame of ordered) {
    if (!String(frame.text || '').trim()) return 'Add text before saving a text frame.';
    if (!Number.isFinite(frame.start) || !Number.isFinite(frame.end) || frame.start < 0 || frame.end > duration) {
      return `Text frame times must be between 0 and ${frameSeconds(duration)} seconds.`;
    }
    if (frame.end - frame.start < .01) return 'A text frame must be at least 0.01 seconds long.';
  }
  for (let i = 1; i < ordered.length; i++) {
    if (ordered[i].start < ordered[i - 1].end) return 'Text frames cannot overlap.';
  }
  return '';
}
function setVisualFrameError(message) {
  const field = $('visual-frames-error'); if (!field) return;
  field.textContent = message; field.hidden = !message;
}
function addTextFrame() {
  const t = track(); if (!t) return;
  commitRange();
  const duration = clipRange.duration;
  const existing = [...editorFrames].sort((a, b) => a.start - b.start); let start = 0;
  for (const frame of existing) { if (frame.start - start >= .01) break; start = Math.max(start, frame.end); }
  if (start >= duration - .01) throw new Error('There is no remaining time in this clip for another text frame.');
  const end = Math.min(duration, start + Math.min(3, duration - start));
  editorFrames = [...existing, {type: 'text', start: Math.round(start * 100) / 100, end: Math.round(end * 100) / 100, text: 'Add your text'}];
  renderTextFrames(); edited();
}
function renderJob() {
  const job = state.job;
  $('job-panel').classList.toggle('completed', !job.running && !job.errors.length);
  $('job-panel').classList.toggle('video-ready', !job.running && !job.errors.length && job.kind === 'video build' && /video ready:/.test(job.message));
  $('job-panel').hidden = !job.message;
  $('job-message').textContent = job.message;
  $('job-count').textContent = job.total ? `${job.done} / ${job.total}` : job.running ? 'Working…' : '';
  const timingLines = Object.entries(job.timings || {}).map(([label, seconds]) => `${label}: ${elapsed(seconds)}`);
  const cache = job.cache || {};
  const cacheLines = [];
  if (cache.video_hits || cache.video_misses) cacheLines.push(`Video: ${cache.video_hits || 0} reused · ${cache.video_misses || 0} encoded`);
  if (cache.clip_hits || cache.clip_misses) cacheLines.push(`Audio cache: ${cache.clip_hits || 0} reused · ${cache.clip_misses || 0} prepared`);
  if (cache.assembled_hits || cache.assembled_misses) cacheLines.push(`Assembled audio: ${cache.assembled_hits ? 'reused' : 'built'}`);
  if (cache.render_hits || cache.render_misses) cacheLines.push(`Visual cache: ${cache.render_hits || 0} reused · ${cache.render_misses || 0} rendered`);
  const total = job.running ? `Elapsed: ${elapsed(job.elapsed_seconds)}` : `Total build time: ${elapsed(job.elapsed_seconds)}`;
  $('job-detail').textContent = [total, ...timingLines, ...cacheLines].filter(Boolean).join(' · ');
  $('cancel-job').hidden = !job.running;
  $('cancel-job').disabled = job.message === 'Canceling…';
  if (job.running && !job.total) $('job-progress').removeAttribute('value');
  else { $('job-progress').max = job.total || 1; $('job-progress').value = job.running ? job.done : job.total || 1; }
  $('job-error-list').replaceChildren(...job.errors.map(error => element('li', error)));
  $('job-errors').hidden = !job.errors.length;
  if (!job.running && job.errors.length) $('job-errors').open = true;
  if (job.running && !pollTimer) pollTimer = setTimeout(poll, 900);
}
async function poll() {
  pollTimer = null;
  try {
    const wasRunning = state.job.running, kind = state.job.kind;
    const next = await api('/state');
    applyState(next, !next.job.running);
    if (wasRunning && !next.job.running && kind === 'playlist' && !next.job.errors.length) {
      $('song-search').value = ''; await showStep(2);
    }
    if (wasRunning && !next.job.running && activeStep === 4) await ensureThumbnail();
  } catch (error) { notice('Connection lost. Keep Playlist to Countdown running, then reload this page.', true); }
}
async function ensureThumbnail() {
  if (!state?.tracks.length || state.job.running) return;
  const thumbnail = state.exports.thumbnail;
  if (thumbnail && Number(thumbnail.revision) === Number(state.revision)) return;
  if (thumbnailPromise) return thumbnailPromise;
  thumbnailPromise = api('/build-thumbnail', {}).then(next => applyState(next)).catch(error => {
    notice(error.message || 'The thumbnail could not be created.', true);
  }).finally(() => { thumbnailPromise = null; });
  return thumbnailPromise;
}
function renderExports() {
  document.querySelectorAll('.export-stale').forEach(label => label.hidden = Number(label.dataset.revision) === state.revision);
  const signature = JSON.stringify(state.exports);
  if (signature === lastExportSignature) return;
  lastExportSignature = signature;
  $('export-files').replaceChildren();
  const thumbnailOutput = $('thumbnail-output');
  if (thumbnailOutput) {
    thumbnailOutput.replaceChildren();
    const thumbnail = state.exports.thumbnail;
    if (thumbnail) {
      const box = element('div', undefined, 'export-file thumbnail-result');
      const heading = element('div', undefined, 'row');
      heading.append(element('strong', 'Thumbnail'), element('span', `${thumbnail.covers} unique covers · ${thumbnail.width}×${thumbnail.height}`, 'muted fine'));
      const image = document.createElement('img');
      image.src = `/api/export-thumbnail?v=${thumbnail.created}`;
      image.alt = 'Generated album cover grid thumbnail';
      const link = element('a', 'Save PNG ↓', 'button secondary');
      link.href = `/api/export-thumbnail?download=1&v=${thumbnail.created}`;
      const stale = element('p', 'Your included songs or artwork may have changed. Create the thumbnail again to update it.', 'stale export-stale');
      stale.dataset.revision = thumbnail.revision; stale.hidden = thumbnail.revision === state.revision;
      box.append(heading, stale, image, link); thumbnailOutput.append(box);
    }
  }
  for (const mode of ['video_final']) {
    const item = state.exports[mode]; if (!item) continue;
    const box = element('div', undefined, 'export-file'), heading = element('div', undefined, 'row');
    heading.append(element('strong', 'Video'), element('span', `${item.clips} clips · ${time(item.seconds)}`, 'muted fine'));
    const isVideo = mode.startsWith('video_');
    const player = document.createElement(isVideo ? 'video' : 'audio'); player.controls = true; player.preload = 'none'; player.src = `${isVideo ? '/api/export-video/' + mode.slice(6) : '/api/export/' + mode}?v=${item.created}`;
    player.setAttribute('aria-label', mode + ' compilation');
    const link = element('a', 'Save MP4 ↓', 'button secondary'); link.href = `/api/export-video/${mode.slice(6)}?download=1&v=${item.created}`;
    const audioLink = element('a', 'Save MP3 ↓', 'button secondary'); audioLink.href = `/api/export/${mode.slice(6)}?download=1&v=${item.created}`;
    box.append(heading);
    if (item.skipped?.length) {
      const warning = element('details', undefined, 'stale');
      warning.append(element('summary', `Warning: ${item.skipped.length} song(s) skipped — missing audio`));
      const list = element('ul');
      for (const song of item.skipped) list.append(element('li', `#${song.rank} ${song.artist} — ${song.title}`));
      warning.append(list); box.append(warning);
    }
    const stale = element('p', 'Your clip choices have changed since this export. Rebuild to include them.', 'stale export-stale');
    stale.dataset.revision = item.revision; stale.hidden = item.revision === state.revision; box.append(stale);
    box.append(player, link, audioLink);
    if (item.tracks?.length) {
      const navigation = element('section', undefined, 'export-track-navigation');
      navigation.setAttribute('aria-label', `${mode} track navigation`);
      const label = element('label', 'Start from a song');
      const search = document.createElement('input'); search.type = 'search'; search.placeholder = 'Search song, artist, or rank…';
      search.setAttribute('aria-label', `Search ${mode} compilation songs`); label.append(search);
      const now = element('p', 'Choose a song to play from its entrance in the mix.', 'muted fine');
      const list = element('div', undefined, 'export-track-list');
      const buttons = [];
      const empty = element('p', 'No matching songs.', 'muted fine'); empty.hidden = true;
      for (const cue of item.tracks) {
        const button = element('button', undefined, 'song export-track'); button.type = 'button';
        button.setAttribute('aria-label', `Start from #${cue.rank}: ${cue.title} by ${cue.artist}`);
        const info = element('span', undefined, 'song-info'); info.append(element('span', cue.title, 'song-title'), element('small', cue.artist));
        button.append(element('span', `#${cue.rank}`, 'rank'), info, element('span', `${time(cue.start, true)} ▶`, 'cue-time'));
        button.addEventListener('click', () => guard(async () => {
          document.querySelectorAll('audio').forEach(other => { if (other !== player) other.pause(); });

          if (player.readyState < 1) {
            await new Promise((resolve, reject) => {
              const clear = () => { clearTimeout(timer); player.removeEventListener('loadedmetadata', loaded); player.removeEventListener('error', failed); };
              const loaded = () => { clear(); resolve(); };
              const failed = () => { clear(); reject(new Error('The compilation could not be loaded. Try rebuilding it.')); };
              const timer = setTimeout(failed, 15000);
              player.addEventListener('loadedmetadata', loaded); player.addEventListener('error', failed); player.load();
            });
          }
          player.currentTime = cue.start; await player.play();
        }));
        buttons.push({button, cue}); list.append(button);
      }
      search.addEventListener('input', () => {
        const query = search.value.trim().toLocaleLowerCase();
        buttons.forEach(({button, cue}) => button.hidden = !`#${cue.rank} ${cue.title} ${cue.artist}`.toLocaleLowerCase().includes(query));
        empty.hidden = buttons.some(({button}) => !button.hidden);
      });
      player.addEventListener('timeupdate', () => {
        let active = null;
        for (const cue of item.tracks) if (cue.start <= player.currentTime) active = cue;
        if (active) now.textContent = `In the mix: #${active.rank} ${active.title} · ${time(player.currentTime, true)}`;
        buttons.forEach(({button, cue}) => {
          button.classList.toggle('active', cue === active);
          if (cue === active) button.setAttribute('aria-current', 'true'); else button.removeAttribute('aria-current');
        });
      });
      navigation.append(label, now, list, empty, element('p', 'Song entrances include the crossfade from the previous track. This list matches this export, even after you edit your clips.', 'muted fine'));
      box.append(navigation);
    } else box.append(element('p', 'Rebuild this export once to add searchable song jump points.', 'muted fine'));
    $('export-files').append(box);
  }
}
async function showStep(step) {
  await flushEdits();
  if (step > 1 && !state.tracks.length) return;
  activeStep = step;
  document.querySelectorAll('.screen').forEach((screen, index) => screen.hidden = index !== step - 1);
  renderSummary();
  if (step === 3) { renderEditor(); requestAnimationFrame(drawWave); }
  if (step === 4) {
    $('default-duration').value = state.settings.duration; $('crossfade').value = state.settings.crossfade; $('preview-context').value = state.settings.preview_context ?? 4;
    await ensureThumbnail();
  }
}
async function selectTrack(key) {
  await flushEdits();
  activeKey = key; renderSongs(); renderEditor(); renderSummary();
}
function snapshot(selected) {
  const t = track(); if (!t) return null;
  commitRange();
  const start = clipRange.start;
  const durationField = $('clip-duration');
  const length = durationField.dataset.usesDefault === 'true' ? null : clipRange.duration;
  if (length !== null && (!Number.isFinite(length) || length < .5 || length > 120)) throw new Error('Clip length must be between 0.5 and 120 seconds.');
  const year = $('track-year').value.trim();
  if (year && !/^\d{4}$/.test(year)) throw new Error('Year must be four digits or blank.');
  if (t.ready && Number.isFinite(audio.duration) && start >= audio.duration) throw new Error('The clip must start before the end of the song.');
  return {key: t.key, version: editVersion, data: {start, duration: length, selected: selected ?? t.selected, source_url: $('source-url').value.trim(), card_title: $('card-title-input').value, card_artist: $('card-artist-input').value, year, description: $('track-description').value, visual_frames: editorTextFrames()}};
}
function queueSave(value) {
  if (!value) return Promise.resolve();
  setSaveStatus('saving');
  const operation = saveChain.catch(() => {}).then(async () => {
    const next = await api(`/track/${value.key}`, value.data);
    applyState(next);
    setSaveStatus(dirty || value.version !== editVersion ? 'saving' : 'saved');
  });
  saveChain = operation;
  return operation;
}
async function flushEdits(selected) {
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  if (dirty || selected !== undefined) {
    const visualError = visualFrameValidationError();
    if (visualError) { dirty = true; setVisualFrameError(visualError); return; }
    const value = snapshot(selected); dirty = false;
    try { await queueSave(value); } catch (error) {
      dirty = true;
      const currentVisualError = visualFrameValidationError();
      if (currentVisualError) { setVisualFrameError(currentVisualError); return; }
      setSaveStatus('error'); throw error;
    }
  } else await saveChain;
}
function edited(context = '') {
  editVersion++; dirty = true; setSaveStatus('saving');
  if (context === 'visual-frame') {
    const visualError = visualFrameValidationError();
    setVisualFrameError(visualError);
    if (visualError) { clearTimeout(saveTimer); saveTimer = null; return; }
  }
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => guard(() => flushEdits()), 450);
  updateClipLabel(); drawWave();
  refreshWorkspacePreview(); rebuildWorkspaceTimeline(); updateWorkspaceVisual();
}
function renderLengthLock() {
  $('length-lock').checked = lengthLocked;
  $('length-lock').setAttribute('aria-label', lengthLocked ? 'Keep clip length fixed while moving a boundary.' : 'Adjust start and end independently.');
}
function syncPreviewPlayback() {
  const playing = !audio.paused;
  $('source-play').classList.toggle('is-playing', playing);
  $('source-play').textContent = '';
  $('preview-toggle')?.classList.toggle('is-playing', playing);
  $('preview-toggle')?.setAttribute('aria-label', playing ? 'Pause preview' : 'Play preview');
}
window.togglePreviewPlayback = () => playback.toggle();
function syncClipPosition(range = clipRange) {
  const max = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : Math.max(1, range.end, range.start + duration());
  $('clip-position').max = String(Math.max(0, max - range.duration));
  $('clip-position').value = String(range.start);
}
function proposedRange() {
  if (!pendingRangeField) return clipRange;
  const value = pendingRangeField === 'duration' ? duration() : parseStart($('clip-' + pendingRangeField).value);
  return ClipRange.adjust(clipRange, pendingRangeField, value, lengthLocked);
}
function commitRange() {
  if (!pendingRangeField) return;
  const changedField = pendingRangeField;
  const next = proposedRange();
  if (track()?.ready && Number.isFinite(audio.duration) && next.start >= audio.duration) throw new Error('The clip must start before the end of the song.');
  // Linked moves retain an inherited default; independent edges create a custom length.
  if (changedField !== 'duration' && !lengthLocked) {
    $('clip-duration').value = next.duration;
    $('clip-duration').dataset.usesDefault = 'false';
  }
  clipRange = next;
  $('clip-start').value = inputSeconds(next.start);
  $('clip-end').value = inputSeconds(next.end);
  pendingRangeField = null;
  playback.rangeChanged();
}
function rangeEdited(field) {
  pendingRangeField = field;
  if (field === 'duration') $('clip-duration').dataset.usesDefault = String($('clip-duration').value === '');
  edited();
}
function updateClipLabel() {
  try {
    const {start, end} = proposedRange();
    $('clip-label').textContent = `${time(start, true)} → ${time(end, true)}`;
  } catch { $('clip-label').textContent = 'Enter a valid start time'; }
}
function drawWave() {
  const canvas = $('waveform'), width = canvas.clientWidth, height = canvas.clientHeight;
  if (!width) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * dpr); canvas.height = height * dpr;
  const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
  // Resolve theme-aware CSS colors through a computed property before canvas use.
  const ink = getComputedStyle($('track-artist')).color, accent = getComputedStyle($('editor-rank')).color;
  let range = clipRange; try { range = proposedRange(); } catch {}
  const total = Number.isFinite(audio.duration) ? audio.duration : 1;
  const startX = range.start / total * width, endX = range.end / total * width;
  ctx.fillStyle = accent; ctx.globalAlpha = .13; ctx.fillRect(startX, 0, endX - startX, height); ctx.globalAlpha = 1;
  if (peaks.length) {
    const bars = Math.min(peaks.length, Math.floor(width / 4));
    for (let i = 0; i < bars; i++) {
      const x = i / bars * width, peak = peaks[Math.floor(i / bars * peaks.length)];
      const barHeight = Math.max(3, peak * height * .8);
      ctx.fillStyle = x >= startX && x <= endX ? accent : ink;
      ctx.globalAlpha = x >= startX && x <= endX ? .95 : .42;
      ctx.fillRect(x, (height - barHeight) / 2, 2, barHeight);
    }
  }
  ctx.globalAlpha = 1; ctx.fillStyle = accent;
  ctx.fillRect(startX, 0, 2, height); ctx.fillRect(endX, 0, 2, height);
  ctx.fillStyle = getComputedStyle($('track-title')).color;
  ctx.fillRect(audio.currentTime / total * width, 0, 1.5, height);
}
async function guard(work) { try { await work(); } catch (error) { notice(error.message || 'Something went wrong. Please try again.', true); } }
async function runButtonBusy(button, label, work) {
  if (!button) return work();
  const original = button.textContent;
  button.disabled = true; button.setAttribute('aria-busy', 'true'); button.textContent = label;
  try { return await work(); }
  finally { button.disabled = false; button.removeAttribute('aria-busy'); button.textContent = original; }
}
function stopTransitionPreview() {
  const player = $('transition-audio');
  if (!player) return;
  player.pause(); player.removeAttribute('src'); player.hidden = true; player.load();
  if (transitionUrl) { URL.revokeObjectURL(transitionUrl); transitionUrl = ''; }
}
async function begin(path, data) { await flushEdits(); notice(''); playback.pause(); stopTransitionPreview(); applyState(await api(path, data)); }

document.querySelectorAll('[data-step]').forEach(button => button.addEventListener('click', () => guard(() => showStep(Number(button.dataset.step)))));
$('song-search').addEventListener('input', renderSongs);
$('song-filter').addEventListener('change', renderSongs);
$('notice-dismiss').addEventListener('click', dismissNotice);
$('source-play').addEventListener('click', () => guard(() => playback.toggle()));
$('preview-toggle')?.addEventListener('click', () => guard(() => window.togglePreviewPlayback()));
audio.addEventListener('play', () => { playback.rangeChanged(); $('source-play').setAttribute('aria-label', 'Pause source audio'); syncPreviewPlayback(); });
audio.addEventListener('pause', () => { $('source-play').setAttribute('aria-label', 'Play source audio'); syncPreviewPlayback(); });
$('clip-position').addEventListener('input', () => {
  const start = Number($('clip-position').value);
  clipRange = ClipRange.adjust(clipRange, 'start', start, true);
  pendingRangeField = null;
  $('clip-start').value = inputSeconds(clipRange.start); $('clip-end').value = inputSeconds(clipRange.end);
  syncClipPosition(); playback.rangeChanged(); edited();
});
$('clip-position').addEventListener('change', () => guard(flushEdits));
for (const field of ['start', 'end', 'duration']) {
  $('clip-' + field).addEventListener('input', () => rangeEdited(field));
  $('clip-' + field).addEventListener('change', () => guard(async () => { commitRange(); await flushEdits(); }));
}
$('source-url').addEventListener('input', edited);
for (const field of ['start', 'end']) $('set-' + field).addEventListener('click', () => guard(async () => {
  await flushEdits(); $('clip-' + field).value = inputSeconds(audio.currentTime); rangeEdited(field); await flushEdits();
}));
 $('length-lock').addEventListener('change', () => guard(async () => {
  await flushEdits(); lengthLocked = $('length-lock').checked;
  try { localStorage.setItem('countdown-length-locked', String(lengthLocked)); } catch {}
  renderLengthLock();
}));
$('stop-at-end').addEventListener('change', () => {
  stopAtClipEnd = $('stop-at-end').checked;
  try { localStorage.setItem('countdown-stop-at-end', String(stopAtClipEnd)); } catch {}
  playback.rangeChanged();
});
$('preview-clip').addEventListener('click', () => guard(async () => {
  await flushEdits(); const start = clipRange.start;
  await playback.seek(start, true);
}));
if ($('preview-card')) $('preview-card').addEventListener('click', () => guard(async () => {
  const preview = window.open('about:blank', '_blank');
  if (!preview) throw new Error('The card preview could not open. Allow pop-ups for Playlist to Countdown, then try again.');
  await flushEdits();
  preview.location.href = `/card-preview/${encodeURIComponent(activeKey)}?revision=${encodeURIComponent(state.revision)}`;
  preview.focus();
}));
const transitionButton = $('preview-transition');
if (transitionButton) transitionButton.addEventListener('click', () => guard(async () => {
  await runButtonBusy(transitionButton, 'Building preview…', async () => {
    await flushEdits();
    const neighbors = transitionNeighbors();
    if (!neighbors.previous || !neighbors.following) throw new Error('Include a clip before and after this song to preview the transition.');
    playback.pause();
    const response = await fetch(`/api/transition/${encodeURIComponent(activeKey)}?revision=${state.revision}`);
    let result;
    if (!response.ok) {
      try { result = await response.json(); } catch {}
      throw new Error(result?.error || 'The transition preview could not be built.');
    }
    if (transitionUrl) URL.revokeObjectURL(transitionUrl);
    transitionUrl = URL.createObjectURL(await response.blob());
    const player = $('transition-audio'); player.src = transitionUrl; player.hidden = false;
    await player.play();
  });
}));
$('next-track').addEventListener('click', () => guard(async () => {
  await flushEdits(); notice('');
  const index = state.tracks.findIndex(t => t.key === activeKey);
  const next = state.tracks[index + 1];
  if (next) { await selectTrack(next.key); $('song-list').querySelector('[aria-current]')?.scrollIntoView({block: 'nearest'}); }
}));

$('previous-track').addEventListener('click', () => guard(async () => {
  await flushEdits(); notice('');
  const index = state.tracks.findIndex(t => t.key === activeKey);
  const previous = state.tracks[index - 1];
  if (previous) {
    await selectTrack(previous.key);
    $('song-list').querySelector('[aria-current]')?.scrollIntoView({block: 'nearest'});
  }
}));
audio.addEventListener('loadedmetadata', () => { $('song-length').textContent = $('source-end').textContent = time(audio.duration); syncClipPosition(); drawWave(); });
audio.addEventListener('timeupdate', drawWave);
audio.addEventListener('error', () => { if (renderedAudio) notice('This audio could not be played. Try adding a replacement MP3 below.', true); });
$('waveform').addEventListener('pointerdown', event => {
  if (!Number.isFinite(audio.duration)) return;
  const rect = event.currentTarget.getBoundingClientRect();
  const x = Math.max(0, Math.min(rect.width, event.clientX - rect.left));
  const range = (() => { try { return proposedRange(); } catch { return clipRange; } })();
  const startX = range.start / audio.duration * rect.width;
  const endX = range.end / audio.duration * rect.width;
  if (x >= startX && x <= endX) {
    dragState = {pointerId: event.pointerId, originX: x, lastX: x, moved: false, originStart: range.start, length: range.end - range.start, width: rect.width, duration: audio.duration};
    event.currentTarget.setPointerCapture(event.pointerId);
    event.currentTarget.classList.add('dragging');
    event.preventDefault();
    return;
  }
  guard(() => playback.seek(x / rect.width * audio.duration, true));
  drawWave();
});
$('waveform').addEventListener('pointermove', event => {
  if (!dragState || event.pointerId !== dragState.pointerId) return;
  const rect = event.currentTarget.getBoundingClientRect();
  const x = Math.max(0, Math.min(rect.width, event.clientX - rect.left));
  dragState.lastX = x;
  if (!dragState.moved && Math.abs(x - dragState.originX) < 4) return;
  dragState.moved = true;
  const delta = (x - dragState.originX) / dragState.width * dragState.duration;
  const start = Math.max(0, Math.min(dragState.duration - dragState.length, dragState.originStart + delta));
  clipRange = ClipRange.adjust({start: dragState.originStart, end: dragState.originStart + dragState.length}, 'start', start, true);
  pendingRangeField = null;
   $('clip-start').value = inputSeconds(clipRange.start);
   $('clip-end').value = inputSeconds(clipRange.end);
  playback.seek(clipRange.start);
  playback.rangeChanged();
  updateClipLabel(); drawWave();
  event.preventDefault();
});
function finishWaveDrag(event) {
  if (!dragState || event.pointerId !== dragState.pointerId) return;
  try { event.currentTarget.releasePointerCapture(event.pointerId); } catch {}
  const rect = event.currentTarget.getBoundingClientRect();
  const x = Math.max(0, Math.min(rect.width, event.clientX - rect.left));
  const wasMoved = dragState.moved;
  const clickedTime = x / rect.width * (Number.isFinite(audio.duration) ? audio.duration : dragState.duration);
  dragState = null;
  event.currentTarget.classList.remove('dragging');
  if (wasMoved) { playback.rangeChanged(); edited(); }
  else {
    guard(() => playback.seek(clickedTime, true));
    drawWave();
  }
}
$('waveform').addEventListener('pointerup', finishWaveDrag);
$('waveform').addEventListener('pointercancel', finishWaveDrag);
new ResizeObserver(drawWave).observe($('waveform'));
$('go-clips').addEventListener('click', () => guard(() => showStep(3)));
$('missing-download').addEventListener('click', () => guard(() => showStep(2)));
$('review-export').addEventListener('click', () => guard(() => showStep(4)));
$('download-all').addEventListener('click', () => guard(() => begin('/download', {})));
const buildVideo = $('build-video');
if ($('video-zoom')) $('video-zoom').addEventListener('input', () => { $('video-zoom-value').textContent = `${$('video-zoom').value}%`; });
if (buildVideo) buildVideo.addEventListener('click', () => guard(() => begin('/build-video', {mode: 'final', duration: $('default-duration').value, crossfade: $('crossfade').value, count: $('video-song-count').value, video_animation: $('video-animation').checked, video_zoom: Number($('video-zoom').value)})));
const cancelJob = $('cancel-job');
if (cancelJob) cancelJob.addEventListener('click', () => guard(() => api('/cancel', {})));
$('settings-form').addEventListener('submit', event => event.preventDefault());
if ($('preview-context')) $('preview-context').addEventListener('input', () => {
  const value = Number($('preview-context').value);
  if (!Number.isFinite(value) || value < .5 || value > 10) return;
  state.settings.preview_context = value;
  renderSummary();
  clearTimeout(previewContextTimer);
  previewContextTimer = setTimeout(async () => {
    try { applyState(await api('/settings', {preview_context: value})); }
    catch (error) { notice(error.message || 'Neighbor preview could not be saved.', true); }
  }, 250);
});
['card-title-input', 'card-artist-input', 'track-year', 'track-description'].forEach(id => $(id).addEventListener('input', edited));
if ($('add-text-frame')) $('add-text-frame').addEventListener('click', async () => {
  $('visual-frames-details').open = true;
  try { await addTextFrame(); }
  catch (error) { setVisualFrameError(error.message || 'Invalid text frame.'); }
});
if ($('visual-frames-details')) $('visual-frames-details').addEventListener('toggle', event => {
  visualDetailsOpen = event.currentTarget.open;
  $('visual-frames-toggle').textContent = event.currentTarget.open ? 'Hide details' : 'Show details';
});
if ($('audio-replacement')) $('audio-replacement').addEventListener('toggle', event => {
  $('audio-replacement-toggle').textContent = event.currentTarget.open ? 'Hide controls' : 'Show controls';
});
if ($('text-frame-list')) $('text-frame-list').addEventListener('input', event => {
  if (!event.target.matches('[data-frame-index]')) return;
  const t = track(), index = Number(event.target.dataset.frameIndex), field = event.target.dataset.frameField;
  if (editorFrames[index]) editorFrames[index][field] = field === 'text' ? event.target.value : Number(event.target.value);
  edited('visual-frame');
});
if ($('text-frame-list')) $('text-frame-list').addEventListener('click', event => {
  const button = event.target.closest('[data-remove-frame]'); if (!button) return;
  const t = track(); if (!t) return;
  const index = Number(button.dataset.removeFrame);
  const removed = editorFrames[index];
  const next = editorFrames[index + 1];
  const previous = editorFrames[index - 1];
  if (next) next.start = removed.start;
  else if (previous) previous.end = removed.end;
  editorFrames.splice(index, 1);
  renderTextFrames(); edited('visual-frame');
});

async function savePerformanceImageUrl() {
  const url = $('performance-image-url').value.trim();
  if (!url) { notice('Paste a direct performance-image URL first.', true); return; }
  const button = $('save-performance-image');
  try { await flushEdits(); setBusy(true); button.setAttribute('aria-busy', 'true'); $('performance-image-status').textContent = 'Downloading image…'; applyState(await api(`/performance-image/${encodeURIComponent(activeKey)}/url`, {url}), true); $('performance-image-status').textContent = 'Performance image saved locally.'; notice('Performance image saved locally.'); }
  catch (error) { $('performance-image-status').textContent = error.message; notice(error.message, true); }
  finally { button.removeAttribute('aria-busy'); setBusy(false); }
}
async function saveAlbumArtworkUrl() {
  const url = $('album-artwork-url').value.trim();
  if (!url) { notice('Paste a direct album-artwork URL first.', true); return; }
  const button = $('save-album-artwork');
  try { await flushEdits(); setBusy(true); button.setAttribute('aria-busy', 'true'); $('album-artwork-status').textContent = 'Downloading image…'; applyState(await api(`/artwork/${encodeURIComponent(activeKey)}/url`, {url}), true); $('album-artwork-status').textContent = 'Custom album cover saved locally.'; notice('Album artwork saved locally.'); }
  catch (error) { $('album-artwork-status').textContent = error.message; notice(error.message, true); }
  finally { button.removeAttribute('aria-busy'); setBusy(false); }
}
async function uploadAlbumArtwork() {
  const file = $('album-artwork-upload').files[0]; if (!file) return;
  const form = new FormData(); form.append('image', file);
  try { await flushEdits(); setBusy(true); $('album-artwork-status').textContent = 'Saving image…'; applyState(await api(`/artwork/${encodeURIComponent(activeKey)}/upload`, form, true), true); $('album-artwork-status').textContent = 'Custom album cover saved locally.'; notice('Album artwork saved locally.'); }
  catch (error) { $('album-artwork-status').textContent = error.message; notice(error.message, true); }
  finally { setBusy(false); $('album-artwork-upload').value = ''; }
}
async function uploadPerformanceImage() {
  const file = $('performance-image-upload').files[0]; if (!file) return;
  const form = new FormData(); form.append('image', file);
  try { await flushEdits(); setBusy(true); $('performance-image-status').textContent = 'Saving image…'; applyState(await api(`/performance-image/${encodeURIComponent(activeKey)}/upload`, form, true), true); $('performance-image-status').textContent = 'Performance image saved locally.'; notice('Performance image saved locally.'); }
  catch (error) { $('performance-image-status').textContent = error.message; notice(error.message, true); }
  finally { setBusy(false); $('performance-image-upload').value = ''; }
}
async function restoreAlbumArtwork() {
  if (!confirm('Restore the original album artwork for this song?')) return;
  try { await flushEdits(); setBusy(true); applyState(await api(`/artwork/${encodeURIComponent(activeKey)}/restore`, {}), true); notice('Original album artwork restored.'); }
  catch (error) { notice(error.message, true); }
  finally { setBusy(false); }
}
async function removePerformanceImage() {
  if (!confirm('Remove the performance image for this song?')) return;
  try { await flushEdits(); setBusy(true); applyState(await api(`/performance-image/${encodeURIComponent(activeKey)}/remove`, {}), true); notice('Performance image removed.'); }
  catch (error) { notice(error.message, true); }
  finally { setBusy(false); }
}
$('save-performance-image').addEventListener('click', savePerformanceImageUrl);
$('performance-image-upload').addEventListener('change', uploadPerformanceImage);
$('save-album-artwork').addEventListener('click', saveAlbumArtworkUrl);
$('album-artwork-upload').addEventListener('change', uploadAlbumArtwork);
$('restore-album-artwork').addEventListener('click', restoreAlbumArtwork);
$('remove-performance-image').addEventListener('click', removePerformanceImage);
$('upload-audio').addEventListener('change', () => guard(async () => {
  const file = $('upload-audio').files[0]; if (!file) return;
  await flushEdits(); const key = activeKey;
  const data = new FormData(); data.append('audio', file);
  $('upload-audio').disabled = true; notice('Checking and adding your MP3…');
  try { $('audio-replacement-status').textContent = 'Saving audio…'; const next = await api('/upload/' + key, data, true); renderedAudio = ''; applyState(next, true); $('audio-replacement-status').textContent = 'Audio source saved locally.'; notice('MP3 added.'); }
  finally { $('upload-audio').value = ''; $('upload-audio').disabled = false; }
}));
const replaceAudioButton = $('replace-audio-button');
if (replaceAudioButton) replaceAudioButton.addEventListener('click', () => guard(async () => {
  await flushEdits();
  notice('Replacing this song’s audio with the YouTube video…');
  $('audio-replacement-status').textContent = 'Replacing audio…';
  applyState(await api('/replace/' + activeKey, {}));
  $('audio-replacement-status').textContent = 'Audio source saved locally.';
}));
async function loadPlaylist() {
  await flushEdits(); notice('');
  const playlistUrl = $('playlist-url').value.trim();
  try {
    const next = await api('/playlist', {url: playlistUrl});
    try { sessionStorage.removeItem('countdown-pending-playlist'); } catch {}
    $('auth-panel').hidden = true; applyState(next);
  } catch (error) {
    if (error.data?.error === 'Reload the dashboard before continuing.') {
      try { sessionStorage.setItem('countdown-pending-playlist', playlistUrl); } catch {}
      notice('Refreshing this dashboard tab, then retrying the playlist…');
      setTimeout(() => location.reload(), 150);
      return;
    }
    if (!error.data?.auth_url) throw error;
    $('auth-panel').hidden = false;
    $('spotify-login').href = error.data.auth_url;
    $('auth-setup').hidden = !error.data.setup_required;
    $('auth-redirect').textContent = error.data.redirect_uri || '';
    $('auth-panel').scrollIntoView({block: 'nearest', behavior: 'smooth'});
    clearTimeout(authTimer);
    authTimer = setTimeout(checkAuth, 1500);
  }
}
async function checkAuth() {
  try {
    const result = await api('/auth/status');
    if (result.connected) { $('auth-panel').hidden = true; await loadPlaylist(); }
    else if (!$('auth-panel').hidden) authTimer = setTimeout(checkAuth, 1500);
  } catch { /* Manual redirect entry remains available. */ }
}
$('playlist-form').addEventListener('submit', event => { event.preventDefault(); guard(loadPlaylist); });
$('auth-form').addEventListener('submit', event => { event.preventDefault(); guard(async () => {
  await api('/auth', {url: $('auth-url').value}); $('auth-url').value = ''; clearTimeout(authTimer); await loadPlaylist();
}); });
window.addEventListener('beforeunload', event => { if (dirty || $('save-status').textContent === 'Saving locally…') { event.preventDefault(); event.returnValue = ''; } });
guard(async () => {
  applyState(await api('/state'), true);
  $('default-duration').value = state.settings.duration;
  $('crossfade').value = state.settings.crossfade;
  const pendingPlaylist = (() => { try { return sessionStorage.getItem('countdown-pending-playlist') || ''; } catch { return ''; } })();
  if (state.playlist_id) $('playlist-url').value = `https://open.spotify.com/playlist/${state.playlist_id}`;
  else if (pendingPlaylist) $('playlist-url').value = pendingPlaylist;
  await showStep(state.tracks.length ? 3 : 1);
  if (pendingPlaylist && !state.tracks.length) {
    try { sessionStorage.removeItem('countdown-pending-playlist'); } catch {}
    await loadPlaylist();
  }
});
