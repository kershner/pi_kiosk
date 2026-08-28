const form = document.getElementById('f');
const input = document.getElementById('search-input');
const searchButton = document.getElementById('search-button');
const results = document.getElementById('search-results-container');
const message = document.getElementById('display-message');
const submitAction = document.getElementById('submit-action');
const submitButton = form.querySelector('.submit-button');
const youtubeUrlPattern = /^https?:\/\/(?:www\.|m\.)?(?:youtube\.com|youtu\.be)\//i;
let messageTimer;

function showMessage(text, duration = 3000) {
  clearTimeout(messageTimer);
  message.textContent = text;
  message.className = 'display-message show';
  messageTimer = setTimeout(() => message.classList.remove('show'), duration);
}

function setSubmitReady(ready) {
  submitAction.hidden = !ready;
}

function selectItem(item) {
  const playlist = item.dataset.type === 'playlist';
  const id = playlist ? item.dataset.playlistId : item.dataset.videoId;
  input.value = playlist
    ? `https://www.youtube.com/playlist?list=${id}`
    : `https://www.youtube.com/watch?v=${id}`;
  setSubmitReady(true);
}

function normalizeSearchError() {
  const error = results.querySelector('.search-error');
  if (error && !error.textContent.includes('API key not configured')) {
    error.textContent = 'Search failed. Please try again.';
  }
}

async function search() {
  const query = input.value.trim();
  if (query.length < 3 || youtubeUrlPattern.test(query)) return;

  results.innerHTML = '<div class="search-loading">Searching YouTube...</div>';
  results.classList.remove('hidden');
  setSubmitReady(false);
  try {
    const response = await fetch(`/api/youtube-search?q=${encodeURIComponent(query)}`);
    if (!response.ok) throw new Error(`Search returned ${response.status}`);
    results.innerHTML = await response.text();
    normalizeSearchError();
  } catch (error) {
    console.error('Search failed:', error);
    results.innerHTML = '<div class="search-error">Search failed. Please try again.</div>';
  }
}

searchButton.addEventListener('click', search);
results.addEventListener('click', event => {
  const item = event.target.closest('.search-result-item');
  if (item) selectItem(item);
});
input.addEventListener('input', () => {
  setSubmitReady(youtubeUrlPattern.test(input.value.trim()));
});

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (submitAction.hidden) {
    await search();
    return;
  }
  submitButton.disabled = true;
  submitButton.textContent = 'Sending…';
  try {
    const response = await fetch('/api/play', {
      method: 'POST',
      body: new URLSearchParams(new FormData(form)),
    });
    const data = await response.json();
    if (!response.ok) {
      const errors = {
        invalid_or_expired: 'This QR code is invalid or expired. Please scan a new one.',
        not_youtube: 'Please provide a valid YouTube URL.',
        missing_token: 'Invalid request. Please scan the QR code again.',
        missing_device: 'Invalid request. Please scan the QR code again.',
      };
      throw new Error(errors[data.error] || data.message || 'Unable to send video.');
    }
    showMessage('✓ Video sent successfully!');
    form.reset();
    results.classList.add('hidden');
    setSubmitReady(false);
  } catch (error) {
    showMessage(error.message || 'Network error. Please try again.');
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = 'Send to display';
  }
});
