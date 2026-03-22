import { DeviceManager } from './deviceManager.js';

const PiStuff = (() => {
  const POLL_INTERVAL_MS = 2500;
  const PLAYER_SERVER = 'http://localhost:8765';
  const $ = s => document.querySelector(s);
  const $all = s => document.querySelectorAll(s);

  let deviceId, currentPlaylist, skipTimer, pollIntervalId;
  let consecutiveSkips = 0;
  let lastVideoId = null;
  let lastTsSeen = 0;
  let shuffleState = false;
  let shuffleCategory = 'all';
  let playlists = {};
  let currentCategoryKey = null;
  let qrVisible = false;
  let switchingPlaylist = false;
  let videoHistory = [];
  let currentTitle = '';
  let messageTimer = null;
  let streamRefreshTimer = null;
  let currentVideoId = null;
  let controlsTimer = null;

  function getVideo() {
    return document.getElementById('video-player');
  }

  function applyQueryParams() {
    const p = new URLSearchParams(location.search);
    const video = p.get('video') || p.get('v');
    const playlist = p.get('playlist') || p.get('list');
    const category = p.get('category') || p.get('cat');

    if (p.get('shuffle') === 'true') shuffleState = true;

    if (category && playlists[category]) {
      currentCategoryKey = category;
      $(`[data-category="${category}"]`)?.classList.add('selected');
      renderPlaylistsForCategory(category);
    }

    if (playlist) {
      let name = 'playlist';
      for (const cat of Object.values(playlists)) {
        const pl = cat.find(x => x.id === playlist);
        if (pl) { name = pl.name; break; }
      }
      currentPlaylist = playlist;
      loadPlaylist(playlist, name);
    } else if (video) {
      setTimeout(() => playVideo(video), 500);
    }

    if (shuffleState) $('#menu [data-action="shuffle"]')?.classList.add('selected');
  }

  function showMessage(text, type = 'info', duration = 3000) {
    const msgEl = $('#display-message');
    if (!msgEl) return;
    clearTimeout(messageTimer);
    msgEl.textContent = text;
    msgEl.className = 'display-message show ' + type;
    messageTimer = setTimeout(() => msgEl.classList.remove('show'), duration);
  }

  function setVideoTitle(title) {
    const el = document.getElementById('video-title-text');
    if (!el) return;
    el.textContent = title;
  }

  function showVideoTitle() {
    document.getElementById('video-title')?.classList.add('visible');
  }

  function hideVideoTitle() {
    document.getElementById('video-title')?.classList.remove('visible');
  }


  function showSpinner() {
    $('#player-container')?.classList.add('loading');
  }

  function hideSpinner() {
    $('#player-container')?.classList.remove('loading');
  }

  function hideMessage() {
    const msgEl = $('#display-message');
    if (msgEl) msgEl.classList.remove('show');
  }

  // ─── Video controls (progress bar + time) ────────────────────────────────────

  function showControls(persist = false) {
    const el = $('#video-controls');
    if (!el) return;
    clearTimeout(controlsTimer);
    el.classList.add('visible');
    if (!persist) {
      controlsTimer = setTimeout(() => el.classList.remove('visible'), 3000);
    }
  }

  function hideControls() {
    clearTimeout(controlsTimer);
    $('#video-controls')?.classList.remove('visible');
  }

  function formatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '0:00';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
  }

  function updateProgress() {
    const video = getVideo();
    if (!video) return;
    const current = video.currentTime || 0;
    const duration = video.duration || 0;
    const pct = duration > 0 ? (current / duration) * 100 : 0;
    const fill = $('#progress-fill');
    if (fill) fill.style.width = `${pct}%`;
    const elCurrent = $('#time-current');
    const elRemaining = $('#time-remaining');
    if (elCurrent) elCurrent.textContent = formatTime(current);
    if (elRemaining) elRemaining.textContent = duration > 0 ? `-${formatTime(duration - current)}` : '-0:00';
  }

  function initProgressBar() {
    const bar = $('#progress-bar');
    if (!bar) return;

    function scrubTo(e) {
      const video = getVideo();
      if (!video || !video.duration) return;
      const rect = bar.getBoundingClientRect();
      const x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
      video.currentTime = Math.max(0, Math.min(1, x / rect.width)) * video.duration;
    }

    bar.addEventListener('click', scrubTo);

    let dragging = false;
    bar.addEventListener('touchstart', () => { dragging = true; }, { passive: true });
    bar.addEventListener('touchmove', e => { if (dragging) scrubTo(e); }, { passive: true });
    bar.addEventListener('touchend', () => { dragging = false; });
  }

  // ─── Subtitles ───────────────────────────────────────────────────────────────

  let captionsEnabled = true;

  function appendSubtitleTrack(video, subtitleUrl) {
    const track = document.createElement('track');
    track.id = 'subtitle-track';
    track.kind = 'subtitles';
    track.srclang = 'en';
    track.label = 'English';
    track.src = `/proxy-subtitle?url=${encodeURIComponent(subtitleUrl)}`;
    track.default = true;
    track.addEventListener('load', () => {
      if (video.textTracks[0]) video.textTracks[0].mode = 'showing';
    });
    video.appendChild(track);
  }

  function setSubtitleTrack(subtitleUrl) {
    const video = getVideo();
    if (!video) return;
    const existing = document.getElementById('subtitle-track');
    if (existing) existing.remove();
    video.dataset.subtitleUrl = subtitleUrl || '';
    if (!subtitleUrl || !captionsEnabled) return;
    appendSubtitleTrack(video, subtitleUrl);
  }

  function initCCButton() {
    const btn = document.getElementById('cc-toggle');
    if (!btn) return;
    btn.classList.toggle('active', captionsEnabled);
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const video = getVideo();
      captionsEnabled = !captionsEnabled;
      btn.classList.toggle('active', captionsEnabled);
      const subtitleUrl = video?.dataset.subtitleUrl || '';
      const existing = document.getElementById('subtitle-track');
      if (existing) existing.remove();
      if (captionsEnabled && subtitleUrl) appendSubtitleTrack(video, subtitleUrl);
    });
  }

  function toggleQr(show) {
    const container = $('#qr-container');
    const playlistsEl = $('#playlists');
    if (!container) return;

    playlistsEl.hidden = show;
    container.hidden = !show;
    qrVisible = show;
  }

  async function regenerateQrCode() {
    if (!deviceId) return showMessage('Device ID not found', 'error');

    const button = $('[data-action="regenerate-qr"]');
    const qrImg = button?.querySelector('img');
    if (button) button.disabled = true;

    try {
      const formData = new URLSearchParams({ device_id: deviceId });

      const response = await fetch(window.REGENERATE_QR_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: formData,
      });

      if (!response.ok) throw new Error('Failed to regenerate QR code');

      const data = await response.json();
      if (qrImg && data.qr_code_b64) {
        qrImg.src = `data:image/png;base64,${data.qr_code_b64}`;
        showMessage(data.regenerated ? '✓ QR code regenerated!' : '✓ QR code is still valid!', 'success');
      }
    } catch (error) {
      console.error('QR regeneration error:', error);
      showMessage('Failed to regenerate QR code', 'error');
    } finally {
      if (button) button.disabled = false;
    }
  }

  // ─── Playback ───────────────────────────────────────────────────────────────
  // Replaces the YouTube iframe player. Instead of YT.Player, we use a native
  // <video> element fed stream URLs from player_server.py (port 8765).

  function setVideoSource(url, videoId, title = '', subtitleUrl = '') {
    const video = getVideo();
    if (!video) return;

    clearTimeout(skipTimer);
    clearTimeout(streamRefreshTimer);
    currentTitle = title;
    currentVideoId = videoId;
    setVideoTitle(title);
    setSubtitleTrack(subtitleUrl);
    video.src = url;
    video.load();
    video.play().catch(err => console.error('play() failed:', err));

    if (videoId) {
      if (lastVideoId) videoHistory.push(lastVideoId);
      if (videoHistory.length > 20) videoHistory.shift();
      lastVideoId = videoId;
    }
    consecutiveSkips = 0;
  }

  async function playVideo(videoId) {
    showSpinner();
    try {
      const r = await fetch(`${PLAYER_SERVER}/resolve-video?video_id=${encodeURIComponent(videoId)}`);
      const data = await r.json();
      if (data.url) {
        setVideoSource(data.url, videoId, data.title || '', data.subtitle_url || '');
      } else {
        showMessage('Could not play video', 'error');
      }
    } catch (e) {
      console.error('playVideo error:', e);
      showMessage('Player server not reachable', 'error');
    }
  }

  async function loadNextFromPlaylist(playlistId) {
    if (!playlistId) return;
    showSpinner();
    try {
      const params = new URLSearchParams({ playlist_id: playlistId });
      if (lastVideoId) params.set('exclude', lastVideoId);

      const r = await fetch(`${PLAYER_SERVER}/next?${params}`);
      const data = await r.json();

      if (data.url) {
        setVideoSource(data.url, data.video_id, data.title || '', data.subtitle_url || '');
      } else {
        skipUnplayable();
      }
    } catch (e) {
      console.error('loadNextFromPlaylist error:', e);
      skipUnplayable();
    }
  }

  function skipUnplayable() {
    consecutiveSkips++;
    if (consecutiveSkips > 8) {
      showMessage('Too many unplayable videos. Check playlist.', 'error', 5000);
      consecutiveSkips = 0;
      playRandom();
      return;
    }
    if (currentPlaylist) loadNextFromPlaylist(currentPlaylist);
  }

  function loadPlaylist(playlistId, playlistName) {
    currentPlaylist = playlistId;
    consecutiveSkips = 0;
    lastVideoId = null;
    switchingPlaylist = true;
    clearTimeout(skipTimer);

    loadNextFromPlaylist(playlistId).then(() => {
      switchingPlaylist = false;
    });

    return playlistName;
  }

  function renderPlaylistsForCategory(categoryKey) {
    const container = $('#playlists');
    if (!container) return;

    const list = playlists[categoryKey] || [];
    if (!list.length) {
      container.innerHTML = '<p class="no-playlists">No playlists in this category</p>';
      return;
    }

    container.innerHTML = list.map(p =>
      `<button data-playlist="${p.id}" class="playlist-button${p.id === currentPlaylist ? ' selected' : ''}">${p.name}</button>`
    ).join('');
  }

  function setInitialActiveStates() {
    if (!currentCategoryKey || !currentPlaylist) return;

    $(`[data-category="${currentCategoryKey}"]`)?.classList.add('selected');
    renderPlaylistsForCategory(currentCategoryKey);
  }

  function loadPlaylistsData() {
    const cats = window.CATEGORIES_DATA || [];
    playlists = {};
    cats.forEach(cat => {
      playlists[cat.name.toLowerCase()] = (cat.playlists || []).map(p => ({
        id: p.youtube_playlist_id,
        name: p.name
      }));
    });

    if (!currentPlaylist) {
      const playlistName = playRandom();
      if (playlistName) setTimeout(() => showMessage(`Playing ${playlistName}`, 'info', 3000), 500);
    }
  }

  function playRandom() {
    const cats = Object.keys(playlists);
    if (!cats.length) return false;

    const catKey = shuffleCategory === 'all'
      ? cats[Math.floor(Math.random() * cats.length)]
      : shuffleCategory;

    const list = playlists[catKey];
    if (!list?.length) return false;

    const randomPlaylist = list[Math.floor(Math.random() * list.length)];

    $all('[data-category]').forEach(b => b.classList.remove('selected'));
    $all('[data-playlist]').forEach(b => b.classList.remove('selected'));
    $(`[data-category="${catKey}"]`)?.classList.add('selected');

    currentCategoryKey = catKey;
    currentPlaylist = randomPlaylist.id;
    renderPlaylistsForCategory(catKey);

    setTimeout(() => {
      showMessage(`Playing ${randomPlaylist.name}...`, 'info', 2000);
    }, 100);
    return loadPlaylist(randomPlaylist.id, randomPlaylist.name);
  }

  function initMenu() {
    const menu = $('#menu');
    if (!menu) return;

    $('.menu-button')?.addEventListener('click', () => menu.hidden = false);

    menu.addEventListener('click', ev => {
      const t = ev.target;
      const { category, playlist, action } = t.dataset;
      const actualAction = action || t.closest('[data-action]')?.dataset.action;

      if (category && playlists[category]) {
        $all('[data-category]').forEach(b => b.classList.remove('selected'));
        t.classList.add('selected');
        currentCategoryKey = category;
        renderPlaylistsForCategory(category);
        toggleQr(false);
        return;
      }

      if (playlist) {
        $all('[data-playlist]').forEach(b => b.classList.remove('selected'));
        t.classList.add('selected');

        let playlistName = 'playlist';
        if (currentCategoryKey) {
          const playlistObj = playlists[currentCategoryKey]?.find(p => p.id === playlist);
          if (playlistObj) playlistName = playlistObj.name;
        }

        loadPlaylist(playlist, playlistName);
        menu.hidden = true;
        toggleQr(false);
        setTimeout(() => showMessage(`Playing ${playlistName}`, 'info', 2000), 100);
        return;
      }

      if (actualAction === 'qr') return toggleQr(true);
      if (actualAction === 'regenerate-qr') {
        ev.stopPropagation();
        return regenerateQrCode();
      }
      if (actualAction === 'reload') return location.reload();
      if (actualAction === 'random') {
        const playlistName = playRandom();
        if (!playlistName) return;
        menu.hidden = true;
        toggleQr(false);
        setTimeout(() => showMessage(`Playing ${playlistName}`, 'info', 2000), 100);
        return;
      }
      if (actualAction === 'shuffle') {
        const shuffleBtn = menu.querySelector('[data-action="shuffle"]');

        // Three states: off → on (all) → on (locked to category) → off
        if (!shuffleState) {
          shuffleState = true;
          shuffleCategory = 'all';
          shuffleBtn.classList.add('selected');
          shuffleBtn.classList.remove('locked');
          setTimeout(() => showMessage('Shuffle on', 'info', 2000), 100);
        } else if (shuffleCategory === 'all' && currentCategoryKey) {
          shuffleCategory = currentCategoryKey;
          shuffleBtn.classList.add('locked');
          setTimeout(() => showMessage(`Shuffle locked to ${currentCategoryKey}`, 'info', 2000), 100);
        } else {
          shuffleState = false;
          shuffleCategory = 'all';
          shuffleBtn.classList.remove('selected', 'locked');
          setTimeout(() => showMessage('Shuffle off', 'info', 2000), 100);
        }
        return;
      }
      if (actualAction === 'screen') {
        ev.stopPropagation();
        document.body.className = 'screen-off';
        menu.hidden = true;
        return;
      }
      if (actualAction === 'close') menu.hidden = true;
    });

    document.body.addEventListener('click', e => {
      if (document.body.className === 'screen-off' && !e.target.closest('.menu-button')) {
        document.body.className = '';
      }
    });

    setInitialActiveStates();
  }

  // Replaces loadYouTubeApi() + createPlayer(). Uses native <video> events
  // instead of the YT Player API state machine.
  function initVideoPlayer() {
    const video = getVideo();
    if (!video) return;

    const overlays = ['#player-overlay-left', '#player-overlay-right', '.menu-button'];
    document.querySelectorAll(overlays.join(',')).forEach(el => el.classList.add('overlay-highlight'));

    // Mirrors YT.PlayerState.ENDED handling
    video.addEventListener('ended', () => {
      if (switchingPlaylist) return;
      if (shuffleState) return playRandom();
      loadNextFromPlaylist(currentPlaylist);
    });

    // Mirrors onError: skipUnplayable
    video.addEventListener('error', () => {
      if (switchingPlaylist) return;
      hideSpinner();
      console.warn('Video error, skipping...');
      skipUnplayable();
    });

    // Mirrors the UNSTARTED skip timer — fires if video stalls for 15s
    video.addEventListener('waiting', () => {
      clearTimeout(skipTimer);
      skipTimer = setTimeout(() => {
        const v = getVideo();
        if (v && !v.paused && v.readyState < 3) {
          console.warn('Stalled, skipping...');
          skipUnplayable();
        }
      }, 15000);
    });

    video.addEventListener('playing', () => {
      clearTimeout(skipTimer);
      consecutiveSkips = 0;
      hideSpinner();
      hideVideoTitle();
      hideControls();
    });

    video.addEventListener('pause', () => {
      showVideoTitle();
      showControls(true);  // persist while paused
    });

    video.addEventListener('timeupdate', updateProgress);

    // Refresh stream URL before it expires for long videos (URLs last ~6h).
    // Reschedules itself after each swap so any length video is covered.
    video.addEventListener('loadedmetadata', () => {
      clearTimeout(streamRefreshTimer);

      const duration = video.duration;
      const REFRESH_INTERVAL = 5 * 60 * 60; // re-resolve every 5h
      if (!isFinite(duration) || duration < REFRESH_INTERVAL) return;

      async function scheduleRefresh() {
        const refreshIn = Math.max(0, REFRESH_INTERVAL - (video.currentTime % REFRESH_INTERVAL)) * 1000;
        streamRefreshTimer = setTimeout(async () => {
          if (!currentVideoId) return;
          const savedTime = video.currentTime;
          try {
            const r = await fetch(`${PLAYER_SERVER}/resolve-video?video_id=${encodeURIComponent(currentVideoId)}`);
            const data = await r.json();
            if (data.url) {
              video.src = data.url;
              video.load();
              video.currentTime = savedTime;
              video.play().catch(err => console.error('stream refresh play() failed:', err));
              scheduleRefresh(); // reschedule for the next 5h window
            }
          } catch (e) {
            console.error('Stream URL refresh failed:', e);
          }
        }, refreshIn);
      }

      scheduleRefresh();
    });
  }

  function customVideoControls() {
    const playerContainer = $('#player-container');
    if (!playerContainer) return;

    const doubleClickDelay = 300;
    const secondsToSkip = 20;

    function createOverlay(side) {
      const overlay = document.createElement('div');
      overlay.id = `player-overlay-${side}`;
      playerContainer.appendChild(overlay);

      let lastClickTime = 0;
      let isDouble = false;

      overlay.addEventListener('click', () => {
        if (document.body.className === 'screen-off') return;

        const video = getVideo();
        const now = Date.now();
        const timeSinceLastClick = now - lastClickTime;

        if (timeSinceLastClick < doubleClickDelay) {
          if (isDouble) {
            // Triple click: previous/next video
            if (side === 'left') {
              const prevId = videoHistory.pop();
              prevId ? playVideo(prevId) : loadNextFromPlaylist(currentPlaylist);
            } else {
              if (shuffleState) {
                playRandom();
              } else {
                loadNextFromPlaylist(currentPlaylist);
              }
            }
            showMessage(side === 'left' ? 'Previous video' : 'Next video', 'info', 1000);
            isDouble = false;
            lastClickTime = 0;
          } else {
            // Double click: skip forward/back 20 seconds
            if (video) {
              if (side === 'left') {
                video.currentTime = Math.max(0, video.currentTime - secondsToSkip);
                showMessage(`-${secondsToSkip}s`, 'info', 1000);
              } else {
                video.currentTime = Math.min(video.duration || Infinity, video.currentTime + secondsToSkip);
                showMessage(`+${secondsToSkip}s`, 'info', 1000);
              }
            }
            isDouble = true;
            lastClickTime = now;
          }
        } else {
          // First click: start timer for play/pause
          isDouble = false;
          lastClickTime = now;

          setTimeout(() => {
            if (lastClickTime === now && !isDouble) {
              if (!video) return;
              if (video.paused) {
                video.play();
                showMessage('Playing', 'info', 1500);
              } else {
                video.pause();
                showMessage('Paused', 'info', 1500);
              }
              showControls(!video.paused);  // persist if now paused, fade if playing
            }
          }, doubleClickDelay);
        }
      });
    }

    createOverlay('left');
    createOverlay('right');
  }

  async function fetchLatest() {
    if (!deviceId) return null;
    const r = await fetch(`${window.LATEST_URL || '/latest'}?device=${encodeURIComponent(deviceId)}`);
    return r.status === 204 ? null : await r.json();
  }

  async function startLatestPoller() {
    if (pollIntervalId) return;

    try {
      const j = await fetchLatest();
      if (j?.ts) lastTsSeen = j.ts;
    } catch (_) { }

    pollIntervalId = setInterval(async () => {
      try {
        const j = await fetchLatest();
        if (!j?.ts || j.ts === lastTsSeen) return;

        lastTsSeen = j.ts;

        if (j.type === 'playlist') {
          loadPlaylist(j.youtube_id, 'Submitted playlist');
          showMessage('✓ Playlist playing!', 'success');
        } else {
          playVideo(j.youtube_id);
          showMessage('✓ Video playing!', 'success');
        }

        if (qrVisible) toggleQr(false);
        $('#menu').hidden = true;
      } catch (_) { }
    }, POLL_INTERVAL_MS);
  }

  function init() {
    deviceId = DeviceManager.ensureDeviceIdInUrl();
    if (!deviceId) return;

    loadPlaylistsData();
    applyQueryParams();
    initMenu();
    customVideoControls();
    initVideoPlayer();
    initProgressBar();
    initCCButton();
    startLatestPoller();
  }

  return { init };
})();

document.addEventListener('DOMContentLoaded', () => PiStuff.init());

export default PiStuff;