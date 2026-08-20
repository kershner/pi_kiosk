import { DeviceManager } from './deviceManager.js';

const PiStuff = (() => {
  const POLL_INTERVAL_MS = 2500;
  const PLAYER_SERVER = 'http://localhost:8765';
  const CAPTIONS_STORAGE_KEY = 'pi-kiosk-captions-enabled';
  const $ = s => document.querySelector(s);
  const $all = s => document.querySelectorAll(s);

  let deviceId, currentPlaylist, skipTimer, pollIntervalId;
  let consecutiveSkips = 0;
  let lastVideoId = null;
  let lastTsSeen = 0;
  let shuffleState = false;
  let shuffleCategory = 'all';
  let playlists = {};
  let categoryNames = {};
  let currentCategoryKey = null;
  let currentPlaylistName = '';
  let qrVisible = false;
  let switchingPlaylist = false;
  let videoHistory = [];
  let currentTitle = '';
  let messageTimer = null;
  let streamRefreshTimer = null;
  let currentVideoId = null;
  let controlsTimer = null;
  let loadAbortController = null;
  let captionsEnabled = localStorage.getItem(CAPTIONS_STORAGE_KEY) !== 'false';
  let queuedShufflePlaylist = null;
  let nowPlayingTimer = null;
  let videoLoading = false;

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
      for (const [catKey, cat] of Object.entries(playlists)) {
        const pl = cat.find(x => x.id === playlist);
        if (pl) {
          name = pl.name;
          currentCategoryKey = catKey;
          break;
        }
      }
      currentPlaylist = playlist;
      loadPlaylist(playlist, name);
    } else if (video) {
      setTimeout(() => playVideo(video), 500);
    } else {
      startInitialPlayback();
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
    const largeTitle = document.getElementById('now-playing-title');
    if (largeTitle) largeTitle.textContent = title;
  }

  function setVideoDuration(duration) {
    const durationEl = document.getElementById('now-playing-duration');
    if (!durationEl) return;
    const valid = isFinite(duration) && duration > 0;
    durationEl.textContent = valid ? formatTime(duration) : '';
    durationEl.hidden = !valid;
  }

  function setNowPlayingContext(categoryKey, playlistName) {
    const context = document.getElementById('now-playing-context');
    if (!context) return;
    const categoryName = categoryKey ? (categoryNames[categoryKey] || categoryKey) : '';
    const text = [categoryName, playlistName].filter(Boolean).join('  •  ');
    context.textContent = text;
    context.hidden = !text;
  }

  function showNowPlaying(status = 'Now playing', loading = false) {
    clearTimeout(nowPlayingTimer);
    const overlay = document.getElementById('now-playing');
    const statusEl = document.getElementById('now-playing-status');
    if (statusEl) statusEl.textContent = status;
    overlay?.classList.toggle('loading', loading);
    overlay?.classList.add('visible');
  }

  function hideNowPlayingAfterDelay() {
    clearTimeout(nowPlayingTimer);
    nowPlayingTimer = setTimeout(() => {
      const video = getVideo();
      if (!video || !video.paused) {
        document.getElementById('now-playing')?.classList.remove('visible');
      }
    }, 5000);
  }

  function showLoadingState() {
    videoLoading = true;
    showNowPlaying(currentTitle ? 'Loading next video' : 'Loading video', true);
  }

  function hideLoadingState() {
    document.getElementById('now-playing')?.classList.remove('loading');
  }

  function hideMessage() {
    const msgEl = $('#display-message');
    if (msgEl) msgEl.classList.remove('show');
  }

  // ─── Video controls (progress bar + time) ────────────────────────────────────

  function showControls(persist = false) {
    const el = $('#video-hud');
    if (!el) return;
    clearTimeout(controlsTimer);
    el.classList.add('visible');
    if (!persist) {
      controlsTimer = setTimeout(() => el.classList.remove('visible'), 3000);
    }
  }

  function hideControls() {
    clearTimeout(controlsTimer);
    $('#video-hud')?.classList.remove('visible');
  }

  function formatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '0:00';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    if (h) return `${h}:${(m % 60).toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
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
      localStorage.setItem(CAPTIONS_STORAGE_KEY, String(captionsEnabled));
      btn.classList.toggle('active', captionsEnabled);
      const subtitleUrl = video?.dataset.subtitleUrl || '';
      const existing = document.getElementById('subtitle-track');
      if (existing) existing.remove();
      if (captionsEnabled && subtitleUrl) appendSubtitleTrack(video, subtitleUrl);
    });
  }

  function cancelLoad() {
    if (loadAbortController) {
      loadAbortController.abort();
      loadAbortController = null;
    }
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
    setVideoDuration(null);
    showNowPlaying('Starting video', true);
    setSubtitleTrack(subtitleUrl);
    video.src = url;
    video.load();
    video.play().catch(err => console.error('play() failed:', err));

    if (videoId) {
      if (lastVideoId) videoHistory.push(lastVideoId);
      if (videoHistory.length > 20) videoHistory.shift();
      lastVideoId = videoId;
    }
    if (shuffleState) queueShufflePrefetch();
  }

  async function playVideo(videoId) {
    cancelLoad();
    currentPlaylistName = '';
    setNowPlayingContext(null, '');
    const controller = new AbortController();
    loadAbortController = controller;
    showLoadingState();
    try {
      const r = await fetch(`${PLAYER_SERVER}/resolve-video?video_id=${encodeURIComponent(videoId)}`, { signal: controller.signal });
      const data = await r.json();
      if (data.url) {
        setVideoSource(data.url, videoId, data.title || '', data.subtitle_url || '');
      } else {
        showMessage('Could not play video', 'error');
      }
    } catch (e) {
      if (e.name === 'AbortError') return;
      console.error('playVideo error:', e);
      showMessage('Player server not reachable', 'error');
    }
  }

  async function loadNextFromPlaylist(playlistId) {
    if (!playlistId) return;
    cancelLoad();
    const controller = new AbortController();
    loadAbortController = controller;
    showLoadingState();
    try {
      const params = new URLSearchParams({ playlist_id: playlistId });
      if (lastVideoId) params.set('exclude', lastVideoId);
      if (shuffleState) params.set('prefetch', '0');

      const r = await fetch(`${PLAYER_SERVER}/next?${params}`, { signal: controller.signal });
      const data = await r.json();

      if (data.url) {
        setVideoSource(data.url, data.video_id, data.title || '', data.subtitle_url || '');
      } else {
        skipUnplayable();
      }
    } catch (e) {
      if (e.name === 'AbortError') return;
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
    const knownPlaylist = findPlaylistChoice(playlistId);
    if (knownPlaylist) {
      currentCategoryKey = knownPlaylist.catKey;
      if (!playlistName || playlistName === 'playlist' || playlistName === 'Submitted playlist') {
        playlistName = knownPlaylist.name;
      }
    }
    queuedShufflePlaylist = null;
    currentPlaylist = playlistId;
    currentPlaylistName = playlistName || 'playlist';
    setNowPlayingContext(currentCategoryKey, currentPlaylistName);
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
    categoryNames = {};
    cats.forEach(cat => {
      const categoryKey = cat.name.toLowerCase();
      categoryNames[categoryKey] = cat.name;
      playlists[categoryKey] = (cat.playlists || []).map(p => ({
        id: p.youtube_playlist_id,
        name: p.name
      }));
    });

  }

  function findPlaylistChoice(playlistId) {
    for (const [catKey, list] of Object.entries(playlists)) {
      const playlist = list.find(item => item.id === playlistId);
      if (playlist) return { ...playlist, catKey };
    }
    return null;
  }

  async function startInitialPlayback() {
    showLoadingState();
    if (shuffleState) {
      try {
        const response = await fetch(`${PLAYER_SERVER}/ready`);
        if (response.status !== 204) {
          const data = await response.json();
          const choice = findPlaylistChoice(data.playlist_id);
          if (data.url && choice) {
            const { catKey } = choice;
            currentCategoryKey = catKey;
            currentPlaylist = choice.id;
            currentPlaylistName = choice.name;
            setNowPlayingContext(catKey, choice.name);
            renderPlaylistsForCategory(catKey);
            setInitialActiveStates();
            setVideoSource(data.url, data.video_id, data.title || '', data.subtitle_url || '');
            return;
          }
        }
      } catch (error) {
        console.warn('Warm startup unavailable:', error);
      }
    }

    playRandom();
  }

  function chooseRandomPlaylist() {
    const cats = Object.keys(playlists);
    if (!cats.length) return null;

    const catKey = shuffleCategory === 'all'
      ? cats[Math.floor(Math.random() * cats.length)]
      : shuffleCategory;

    const list = playlists[catKey];
    if (!list?.length) return null;

    const alternatives = list.filter(p => p.id !== currentPlaylist);
    const candidates = alternatives.length ? alternatives : list;
    const playlist = candidates[Math.floor(Math.random() * candidates.length)];
    return { ...playlist, catKey };
  }

  function playRandomChoice(choice) {
    if (!choice) return false;
    const { catKey, ...randomPlaylist } = choice;

    $all('[data-category]').forEach(b => b.classList.remove('selected'));
    $all('[data-playlist]').forEach(b => b.classList.remove('selected'));
    $(`[data-category="${catKey}"]`)?.classList.add('selected');

    currentCategoryKey = catKey;
    currentPlaylist = randomPlaylist.id;
    renderPlaylistsForCategory(catKey);

    return loadPlaylist(randomPlaylist.id, randomPlaylist.name);
  }

  function playRandom() {
    return playRandomChoice(chooseRandomPlaylist());
  }

  function queueShufflePrefetch() {
    if (!shuffleState || queuedShufflePlaylist) return;
    const choice = chooseRandomPlaylist();
    if (!choice) return;
    queuedShufflePlaylist = choice;
    const params = new URLSearchParams({ playlist_id: choice.id });
    if (currentVideoId) params.set('exclude', currentVideoId);
    fetch(`${PLAYER_SERVER}/prefetch?${params}`).catch(err => {
      console.warn('Shuffle prefetch failed:', err);
    });
  }

  function playQueuedShuffle() {
    const choice = queuedShufflePlaylist;
    queuedShufflePlaylist = null;
    return playRandomChoice(choice || chooseRandomPlaylist());
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
        queuedShufflePlaylist = null;
        if (shuffleState && currentVideoId) queueShufflePrefetch();
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
      showNowPlaying('Loading next video', true);
      if (shuffleState) return playQueuedShuffle();
      loadNextFromPlaylist(currentPlaylist);
    });

    // Mirrors onError: skipUnplayable
    video.addEventListener('error', () => {
      if (switchingPlaylist) return;
      hideLoadingState();
      console.warn('Video error, skipping...', {
        code: video.error?.code,
        message: video.error?.message,
        networkState: video.networkState,
        readyState: video.readyState,
        videoId: currentVideoId,
      });
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
      videoLoading = false;
      hideLoadingState();
      hideControls();
      showNowPlaying('Now playing');
      hideNowPlayingAfterDelay();
    });

    video.addEventListener('pause', () => {
      if (videoLoading) return;
      showNowPlaying('Paused');
      showControls(true);  // persist while paused
    });

    video.addEventListener('timeupdate', updateProgress);

    // Refresh stream URL before it expires for long videos (URLs last ~6h).
    // Reschedules itself after each swap so any length video is covered.
    video.addEventListener('loadedmetadata', () => {
      clearTimeout(streamRefreshTimer);

      const duration = video.duration;
      setVideoDuration(duration);
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

    const multiTapDelay = 220;
    const secondsToSkip = 20;

    function createOverlay(side) {
      const overlay = document.createElement('div');
      overlay.id = `player-overlay-${side}`;
      playerContainer.appendChild(overlay);

      let lastTapTime = 0;
      let tapCount = 0;
      let resetTapTimer = null;
      let initialPaused = false;

      function restoreInitialPlayback(video) {
        if (initialPaused && !video.paused) {
          video.pause();
        } else if (!initialPaused && video.paused) {
          video.play().catch(() => {});
        }
      }

      function resetTapStateAfterDelay() {
        clearTimeout(resetTapTimer);
        resetTapTimer = setTimeout(() => {
          tapCount = 0;
          lastTapTime = 0;
        }, multiTapDelay);
      }

      overlay.addEventListener('pointerup', event => {
        if (event.pointerType === 'touch') event.preventDefault();
        if (document.body.className === 'screen-off') return;

        const video = getVideo();
        if (!video) return;
        const now = Date.now();
        const continuesGesture = now - lastTapTime < multiTapDelay;

        if (continuesGesture) {
          if (tapCount === 2) {
            // Triple click: previous/next video
            clearTimeout(resetTapTimer);
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
            tapCount = 0;
            lastTapTime = 0;
          } else {
            // Double click: skip forward/back 20 seconds
            restoreInitialPlayback(video);
            if (side === 'left') {
              video.currentTime = Math.max(0, video.currentTime - secondsToSkip);
              showMessage(`-${secondsToSkip}s`, 'info', 1000);
            } else {
              video.currentTime = Math.min(video.duration || Infinity, video.currentTime + secondsToSkip);
              showMessage(`+${secondsToSkip}s`, 'info', 1000);
            }
            tapCount = 2;
            lastTapTime = now;
            resetTapStateAfterDelay();
          }
        } else {
          // First tap: respond immediately; a second tap rolls this back before seeking.
          initialPaused = video.paused;
          if (initialPaused) {
            video.play().catch(() => {});
          } else {
            video.pause();
          }
          showControls(!initialPaused);
          tapCount = 1;
          lastTapTime = now;
          resetTapStateAfterDelay();
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
