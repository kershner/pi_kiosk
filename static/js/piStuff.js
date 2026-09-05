const $ = selector => document.querySelector(selector);
const $$ = selector => document.querySelectorAll(selector);
const PLAYER_API = '/api/player';
const CAPTIONS_KEY = 'pi-kiosk-captions-enabled';
const SHUFFLE_ALL = '*';
const PLAYER_REQUEST_TIMEOUT_MS = 10 * 60 * 1000;
const MEDIA_START_TIMEOUT_MS = 60 * 1000;
const RECOVERY_DELAY_MS = 2 * 60 * 1000;
const MAX_PLAYLIST_ERRORS = 3;
const MAX_TOTAL_ERRORS = 6;

const state = {
  deviceId: null,
  catalog: {},
  categoryNames: {},
  playlistIndex: new Map(),
  category: null,
  playlistId: null,
  playlistName: '',
  videoId: null,
  lastVideoId: null,
  title: '',
  queuedChoice: null,
  shuffleScope: null,
  errors: 0,
  loading: false,
  showCatalogContext: true,
  switchingPlaylist: false,
  started: false,
  resuming: false,
  resumeAfterMenu: false,
  latestTs: 0,
  request: null,
  captions: localStorage.getItem(CAPTIONS_KEY) !== 'false',
};

const timers = {};
const dom = {};

function clearTimer(name) {
  clearTimeout(timers[name]);
  timers[name] = null;
}

function schedule(name, callback, delay) {
  clearTimer(name);
  timers[name] = setTimeout(callback, delay);
}

function cacheDom() {
  Object.assign(dom, {
    video: $('#video-player'),
    menu: $('#menu'),
    menuButton: $('.menu-button'),
    playlists: $('#playlists'),
    qr: $('#qr-container'),
    loadingIndicator: $('#loading-indicator'),
    nowPlaying: $('#now-playing'),
    status: $('#now-playing-status'),
    context: $('#now-playing-context'),
    duration: $('#now-playing-duration'),
    title: $('#now-playing-title'),
    pausedControls: $('#paused-controls'),
    hud: $('#video-hud'),
    progress: $('#progress-bar'),
    progressFill: $('#progress-fill'),
    currentTime: $('#time-current'),
    remainingTime: $('#time-remaining'),
    captions: $('#cc-toggle'),
    playbackState: $('#playback-state'),
  });
}

function ensureDeviceId() {
  let id = localStorage.getItem('pi_device_id');
  if (!id) {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    id = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    localStorage.setItem('pi_device_id', id);
  }

  const params = new URLSearchParams(location.search);
  if (params.has('device_id')) return id;
  params.set('device_id', id);
  location.search = params.toString();
  return null;
}

function loadCatalog() {
  for (const category of window.CATEGORIES_DATA || []) {
    const key = category.name.toLowerCase();
    const playlists = (category.playlists || []).map(item => ({
      id: item.youtube_playlist_id,
      name: item.name,
      category: key,
    }));
    state.catalog[key] = playlists;
    state.categoryNames[key] = category.name;
    playlists.forEach(playlist => state.playlistIndex.set(playlist.id, playlist));
  }
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60).toString().padStart(2, '0');
  return hours
    ? `${hours}:${String(minutes % 60).padStart(2, '0')}:${secs}`
    : `${minutes}:${secs}`;
}

function showNowPlaying(status) {
  clearTimer('nowPlaying');
  dom.status.textContent = status;
  dom.status.hidden = !status;
  dom.nowPlaying.classList.add('visible');
}

function hideNowPlayingAfter(delay) {
  schedule('nowPlaying', () => {
    if (!dom.video.paused) dom.nowPlaying.classList.remove('visible');
  }, delay);
}

function hideNowPlayingImmediately() {
  clearTimer('nowPlaying');
  dom.nowPlaying.classList.remove('visible');
}

function setContext(category = state.category, playlistName = state.playlistName) {
  const categoryName = state.categoryNames[category] || category || '';
  const text = state.showCatalogContext
    ? [categoryName, playlistName].filter(Boolean).join('  •  ')
    : '';
  dom.context.textContent = text;
  dom.context.hidden = !text;
}

function setDuration(duration) {
  const valid = Number.isFinite(duration) && duration > 0;
  dom.duration.textContent = valid ? formatTime(duration) : '';
  dom.duration.hidden = !valid;
}

function setPlaybackState(label) {
  dom.playbackState.textContent = label;
}

function setPausedUi(paused) {
  setPlaybackState(paused ? 'Paused' : 'Playing');
  dom.pausedControls.hidden = !paused;
  dom.hud.classList.toggle('visible', paused);
  if (paused) updateProgress();
}

function beginLoading(message = 'Preparing video…') {
  state.loading = true;
  dom.loadingIndicator.hidden = false;
  dom.pausedControls.hidden = true;
  dom.hud.classList.remove('visible');
  setPlaybackState('Loading');
  dom.title.textContent = message;
  setDuration(null);
  showNowPlaying('Loading');
}

function endLoading() {
  clearTimer('mediaStart');
  state.loading = false;
  dom.loadingIndicator.hidden = true;
}

function failLoading(status) {
  endLoading();
  setPausedUi(dom.video.paused);
  dom.title.textContent = state.title;
  showNowPlaying(status);
}

function updateProgress() {
  const current = dom.video.currentTime || 0;
  const duration = dom.video.duration || 0;
  dom.progressFill.style.width = `${duration ? current / duration * 100 : 0}%`;
  dom.currentTime.textContent = formatTime(current);
  dom.remainingTime.textContent = duration ? `-${formatTime(duration - current)}` : '-0:00';
}

function initScrubber() {
  let dragging = false;
  const scrub = event => {
    if (!dom.video.duration) return;
    const rect = dom.progress.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    dom.video.currentTime = ratio * dom.video.duration;
    updateProgress();
  };

  dom.progress.addEventListener('pointerdown', event => {
    dragging = true;
    dom.progress.setPointerCapture(event.pointerId);
    scrub(event);
  });
  dom.progress.addEventListener('pointermove', event => {
    if (dragging) scrub(event);
  });
  ['pointerup', 'pointercancel'].forEach(type => {
    dom.progress.addEventListener(type, () => { dragging = false; });
  });
}

function addCaptionTrack(url) {
  const track = document.createElement('track');
  Object.assign(track, {
    id: 'subtitle-track',
    kind: 'subtitles',
    srclang: 'en',
    label: 'English',
    src: `/proxy-subtitle?url=${encodeURIComponent(url)}`,
    default: true,
  });
  track.addEventListener('load', () => {
    if (dom.video.textTracks[0]) dom.video.textTracks[0].mode = 'showing';
  });
  dom.video.appendChild(track);
}

function applyCaptions() {
  $('#subtitle-track')?.remove();
  const url = dom.video.dataset.subtitleUrl;
  if (state.captions && url) addCaptionTrack(url);
}

function setCaptionSource(url = '') {
  dom.video.dataset.subtitleUrl = url;
  applyCaptions();
}

function initCaptions() {
  dom.captions.classList.toggle('active', state.captions);
  dom.captions.addEventListener('click', event => {
    event.stopPropagation();
    state.captions = !state.captions;
    localStorage.setItem(CAPTIONS_KEY, state.captions);
    dom.captions.classList.toggle('active', state.captions);
    applyCaptions();
  });
}

function toggleQr(show) {
  dom.playlists.hidden = show;
  dom.qr.hidden = !show;
}

async function regenerateQr() {
  if (!state.deviceId) return;
  const button = $('[data-action="regenerate-qr"]');
  const image = button?.querySelector('img');
  if (button) button.disabled = true;
  try {
    const response = await fetch('/regenerate-qr', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ device_id: state.deviceId }),
    });
    if (!response.ok) throw new Error('QR request failed');
    const data = await response.json();
    if (image && data.qr_code_b64) image.src = `data:image/png;base64,${data.qr_code_b64}`;
  } catch (error) {
    console.error('QR regeneration failed:', error);
  } finally {
    if (button) button.disabled = false;
  }
}

function cancelRequest() {
  state.request?.abort();
  state.request = null;
}

function setVideoSource(data) {
  clearTimer('streamRefresh');
  state.title = data.title || '';
  state.videoId = data.video_id;
  state.started = false;
  state.resuming = false;
  dom.title.textContent = state.title;
  setDuration(null);
  showNowPlaying('Starting playback');
  setCaptionSource(data.subtitle_url);

  // Force Chromium to release the previous decoder, network connection, and
  // media buffers before attaching the next direct stream.
  dom.video.pause();
  dom.video.removeAttribute('src');
  dom.video.load();
  dom.video.src = data.url;
  dom.video.load();
  dom.video.play().catch(error => {
    if (error.name === 'AbortError') return;
    console.error('play() failed:', error);
    if (error.name === 'NotAllowedError') {
      endLoading();
      setPausedUi(true);
      showNowPlaying('Paused');
    }
  });

  // Chromium does not always emit a media error when a direct stream stalls
  // during startup. Do not leave the kiosk in its loading state indefinitely.
  schedule('mediaStart', () => {
    if (!state.loading || state.started || state.videoId !== data.video_id) return;
    console.warn('Media did not start within the playback deadline; skipping video');
    skipUnplayable();
  }, MEDIA_START_TIMEOUT_MS);

  if (data.video_id) {
    state.lastVideoId = data.video_id;
  }
  if (state.shuffleScope) queueShufflePrefetch();
}

async function playerRequest(endpoint, params, signal) {
  const controller = new AbortController();
  let timedOut = false;
  const forwardAbort = () => controller.abort();
  signal?.addEventListener('abort', forwardAbort, { once: true });
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, PLAYER_REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(
      `${PLAYER_API}/${endpoint}?${new URLSearchParams(params)}`,
      { signal: controller.signal },
    );
    const data = await response.json();
    if (!response.ok || !data.url) throw new Error(data.error || 'No playable stream returned');
    return data;
  } catch (error) {
    if (timedOut) throw new Error('Video resolution timed out');
    throw error;
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener('abort', forwardAbort);
  }
}

function continueCurrentVideoWhileResolving() {
  if (!dom.video.currentSrc || dom.video.ended || dom.video.error) return;
  dom.video.play().catch(error => {
    if (error.name !== 'AbortError') {
      console.warn('Could not continue current video while resolving:', error);
    }
  });
}

async function resolveAndPlay({ videoId = null, playlistId = null }) {
  cancelRequest();
  const controller = new AbortController();
  state.request = controller;
  beginLoading(videoId ? 'Loading requested video…' : 'Finding the next video…');
  continueCurrentVideoWhileResolving();

  const direct = Boolean(videoId);
  const params = direct
    ? { video_id: videoId }
    : { playlist_id: playlistId };
  if (!direct && state.lastVideoId) params.exclude = state.lastVideoId;
  if (!direct && state.shuffleScope) params.prefetch = '0';

  try {
    const data = await playerRequest(direct ? 'resolve' : 'next', params, controller.signal);
    if (state.request !== controller) return false;
    setVideoSource(data);
    return true;
  } catch (error) {
    if (error.name === 'AbortError') return false;
    console.error('Video resolution failed:', error);
    if (playlistId) skipUnplayable();
    else {
      failLoading('Unable to play video');
      schedule('nowPlaying', () => dom.nowPlaying.classList.remove('visible'), 3000);
    }
    return false;
  } finally {
    if (state.request === controller) state.request = null;
  }
}

function playVideo(videoId) {
  state.showCatalogContext = false;
  state.playlistName = '';
  setContext(null, '');
  return resolveAndPlay({ videoId });
}

function loadNext(playlistId = state.playlistId) {
  return playlistId ? resolveAndPlay({ playlistId }) : Promise.resolve(false);
}

function skipUnplayable() {
  clearTimer('mediaStart');
  state.errors += 1;
  if (state.errors <= MAX_PLAYLIST_ERRORS) return loadNext();
  if (state.errors <= MAX_TOTAL_ERRORS) {
    console.warn('Playlist is unhealthy; trying another playlist');
    const fallback = playRandom({ preserveErrors: true });
    if (fallback) return fallback;
  }

  console.error('Playback recovery limit reached; backing off before retrying');
  cancelRequest();
  failLoading('No playable videos — retrying soon');
  schedule('resolutionRetry', () => {
    state.errors = 0;
    if (!playRandom()) failLoading('No videos available');
  }, RECOVERY_DELAY_MS);
  return false;
}

function findChoice(playlistId) {
  return state.playlistIndex.get(playlistId) || null;
}

function loadPlaylist(
  playlistId,
  name = 'playlist',
  { showContext = true, preserveErrors = false } = {},
) {
  const choice = findChoice(playlistId);
  if (choice) {
    state.category = choice.category;
    if (!name || ['playlist', 'Submitted playlist'].includes(name)) name = choice.name;
  }
  state.queuedChoice = null;
  state.playlistId = playlistId;
  state.playlistName = name || 'playlist';
  state.showCatalogContext = showContext;
  if (!preserveErrors) state.errors = 0;
  state.lastVideoId = null;
  state.switchingPlaylist = true;
  setContext();
  loadNext().finally(() => { state.switchingPlaylist = false; });
  return state.playlistName;
}

function renderPlaylists(category = state.category) {
  const playlists = state.catalog[category] || [];
  dom.playlists.innerHTML = playlists.length
    ? playlists.map(item => (
      `<button data-playlist="${item.id}" class="playlist-button${item.id === state.playlistId ? ' selected' : ''}">${item.name}</button>`
    )).join('')
    : '<p class="no-playlists">No playlists in this category</p>';
}

function selectCategory(category) {
  if (!state.catalog[category]) return;
  state.category = category;
  $$('[data-category]').forEach(button => {
    button.classList.toggle('selected', button.dataset.category === category);
  });
  renderPlaylists(category);
  toggleQr(false);
}

function chooseRandomPlaylist() {
  const categories = Object.keys(state.catalog);
  if (!categories.length) return null;
  const category = state.shuffleScope && state.shuffleScope !== SHUFFLE_ALL
    ? state.shuffleScope
    : categories[Math.floor(Math.random() * categories.length)];
  const playlists = state.catalog[category] || [];
  const alternatives = playlists.filter(item => item.id !== state.playlistId);
  const choices = alternatives.length ? alternatives : playlists;
  return choices.length ? choices[Math.floor(Math.random() * choices.length)] : null;
}

function playChoice(choice, options = {}) {
  if (!choice) return false;
  state.resumeAfterMenu = false;
  selectCategory(choice.category);
  dom.menu.hidden = true;
  return loadPlaylist(choice.id, choice.name, { showContext: true, ...options });
}

function playRandom(options = {}) {
  return playChoice(chooseRandomPlaylist(), options);
}

function queueShufflePrefetch() {
  if (!state.shuffleScope || state.queuedChoice) return;
  const choice = chooseRandomPlaylist();
  if (!choice) return;
  state.queuedChoice = choice;
  const params = { playlist_id: choice.id };
  if (state.videoId) params.exclude = state.videoId;
  fetch(`${PLAYER_API}/prefetch?${new URLSearchParams(params)}`)
    .catch(error => console.warn('Shuffle prefetch failed:', error));
}

function playQueuedShuffle() {
  const choice = state.queuedChoice || chooseRandomPlaylist();
  state.queuedChoice = null;
  return playChoice(choice);
}

async function startPlayback() {
  beginLoading();
  if (state.shuffleScope) {
    try {
      const response = await fetch(`${PLAYER_API}/ready`);
      if (response.status !== 204) {
        const data = await response.json();
        const choice = findChoice(data.playlist_id);
        if (data.url && choice) {
          state.category = choice.category;
          state.playlistId = choice.id;
          state.playlistName = choice.name;
          selectCategory(choice.category);
          setContext();
          setVideoSource(data);
          return;
        }
      }
    } catch (error) {
      console.warn('Warm startup unavailable:', error);
    }
  }
  if (!playRandom()) failLoading('No videos available');
}

function updateShuffleButton() {
  const button = $('[data-action="shuffle"]');
  button.classList.toggle('selected', Boolean(state.shuffleScope));
  button.classList.toggle('locked', Boolean(state.shuffleScope && state.shuffleScope !== SHUFFLE_ALL));
}

function cycleShuffle() {
  if (!state.shuffleScope) {
    state.shuffleScope = SHUFFLE_ALL;
  } else if (state.shuffleScope === SHUFFLE_ALL && state.category) {
    state.shuffleScope = state.category;
  } else {
    state.shuffleScope = null;
  }
  state.queuedChoice = null;
  updateShuffleButton();
  if (state.shuffleScope && state.videoId) queueShufflePrefetch();
}

function initMenu() {
  const openMenu = () => {
    state.resumeAfterMenu = !dom.video.paused && !state.loading;
    if (state.resumeAfterMenu) dom.video.pause();
    dom.menu.hidden = false;
  };

  const closeMenu = () => {
    dom.menu.hidden = true;
    if (!state.resumeAfterMenu) return;
    state.resumeAfterMenu = false;
    dom.video.play().catch(() => {});
  };

  const actions = {
    qr: () => toggleQr(true),
    'regenerate-qr': regenerateQr,
    reload: () => location.reload(),
    random: playRandom,
    shuffle: cycleShuffle,
    screen: () => {
      document.body.classList.add('screen-off');
      dom.menu.hidden = true;
    },
    close: closeMenu,
  };

  dom.menuButton.addEventListener('click', openMenu);
  dom.menu.addEventListener('click', event => {
    const target = event.target.closest('[data-category],[data-playlist],[data-action]');
    if (!target) return;
    if (target.dataset.category) return selectCategory(target.dataset.category);
    if (target.dataset.playlist) {
      const choice = findChoice(target.dataset.playlist);
      playChoice(choice || { id: target.dataset.playlist, name: 'playlist', category: state.category });
      toggleQr(false);
      return;
    }
    if (['regenerate-qr', 'screen'].includes(target.dataset.action)) event.stopPropagation();
    actions[target.dataset.action]?.();
  });

  document.body.addEventListener('click', event => {
    if (document.body.classList.contains('screen-off') && !event.target.closest('.menu-button')) {
      document.body.classList.remove('screen-off');
    }
  });
}

function scheduleStreamRefresh() {
  clearTimer('streamRefresh');
  const interval = 5 * 60 * 60;
  if (!Number.isFinite(dom.video.duration) || dom.video.duration < interval) return;
  const delay = Math.max(0, interval - dom.video.currentTime % interval) * 1000;
  schedule('streamRefresh', async () => {
    if (!state.videoId) return;
    const savedTime = dom.video.currentTime;
    try {
      const data = await playerRequest('resolve', { video_id: state.videoId });
      dom.video.src = data.url;
      dom.video.load();
      dom.video.currentTime = savedTime;
      await dom.video.play();
      scheduleStreamRefresh();
    } catch (error) {
      console.error('Stream URL refresh failed:', error);
    }
  }, delay);
}

function advanceVideo() {
  return state.shuffleScope ? playQueuedShuffle() : loadNext();
}

function initVideoEvents() {
  dom.video.addEventListener('ended', () => {
    if (!state.switchingPlaylist && !state.loading) advanceVideo();
  });
  dom.video.addEventListener('error', () => {
    if (state.switchingPlaylist || state.request) return;
    console.warn('Fatal media error; skipping video', dom.video.error);
    skipUnplayable();
  });
  dom.video.addEventListener('playing', () => {
    if (state.loading && state.request) return;
    const firstPlayback = !state.started;
    const resumed = state.resuming;
    state.started = true;
    state.resuming = false;
    state.errors = 0;
    endLoading();
    setPausedUi(false);
    if (firstPlayback) {
      showNowPlaying('Playing');
      hideNowPlayingAfter(5000);
    } else if (resumed) hideNowPlayingImmediately();
  });
  dom.video.addEventListener('pause', () => {
    if (state.loading) return;
    state.resuming = true;
    setPausedUi(true);
    showNowPlaying('Paused');
  });
  dom.video.addEventListener('loadedmetadata', () => {
    setDuration(dom.video.duration);
    updateProgress();
    scheduleStreamRefresh();
  });
}

function initPlaybackControls() {
  const seekSeconds = 20;

  for (const side of ['left', 'right']) {
    const overlay = document.createElement('div');
    overlay.id = `player-overlay-${side}`;
    $('#player-container').appendChild(overlay);

    overlay.addEventListener('pointerup', event => {
      if (event.pointerType === 'touch') event.preventDefault();
      if (document.body.classList.contains('screen-off') || state.loading || dom.video.paused) return;
      dom.video.pause();
    });
  }

  dom.pausedControls.addEventListener('click', event => {
    const button = event.target.closest('[data-paused-action]');
    if (!button) return;

    const action = button.dataset.pausedAction;
    if (action === 'play') {
      dom.video.play().catch(() => {});
      return;
    }

    if (action === 'back' || action === 'forward') {
      const delta = action === 'back' ? -seekSeconds : seekSeconds;
      const duration = Number.isFinite(dom.video.duration) ? dom.video.duration : Infinity;
      dom.video.currentTime = Math.max(0, Math.min(duration, dom.video.currentTime + delta));
      updateProgress();
      return;
    }

    if (action === 'next') {
      if (!state.shuffleScope && !state.playlistId) return;
      advanceVideo();
    }
  });

}

async function fetchLatest() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch(
      `/latest?device=${encodeURIComponent(state.deviceId)}`,
      { signal: controller.signal },
    );
    return response.status === 204 ? null : response.json();
  } finally {
    clearTimeout(timeout);
  }
}

function initRemotePoller() {
  const poll = async (playNew = true) => {
    try {
      const data = await fetchLatest();
      if (!data?.ts || data.ts === state.latestTs) return;
      state.latestTs = data.ts;
      if (!playNew) return;
      if (data.type === 'playlist') {
        state.resumeAfterMenu = false;
        loadPlaylist(data.youtube_id, 'Submitted playlist', { showContext: false });
      } else {
        state.resumeAfterMenu = false;
        playVideo(data.youtube_id);
      }
      toggleQr(false);
      dom.menu.hidden = true;
    } catch (_) { }
    finally {
      schedule('remotePoll', () => poll(), 2500);
    }
  };

  poll(false);
}

function applyQuery() {
  const params = new URLSearchParams(location.search);
  state.shuffleScope = params.get('shuffle') === 'true' ? SHUFFLE_ALL : null;
  const category = params.get('category') || params.get('cat');
  if (category) selectCategory(category);
  updateShuffleButton();

  const playlist = params.get('playlist') || params.get('list');
  const video = params.get('video') || params.get('v');
  if (playlist) return loadPlaylist(playlist, findChoice(playlist)?.name || 'playlist');
  if (video) return playVideo(video);
  return startPlayback();
}

function init() {
  cacheDom();
  state.deviceId = ensureDeviceId();
  if (!state.deviceId || !dom.video) return;
  loadCatalog();
  initMenu();
  initScrubber();
  initCaptions();
  initPlaybackControls();
  initVideoEvents();
  initRemotePoller();
  applyQuery();
}

document.addEventListener('DOMContentLoaded', init);
