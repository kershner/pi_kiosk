export const YouTubeSearch = (() => {
  let selectedIndex = -1;

  const SEARCH_ENDPOINT = '/api/youtube-search';
  
  function isYouTubeUrl(str) {
    return /^https?:\/\/(www\.)?(youtube\.com|youtu\.be)/.test(str);
  }

  async function searchVideos(query) {
    try {
      const response = await fetch(`${SEARCH_ENDPOINT}?q=${encodeURIComponent(query)}`);
      
      if (!response.ok) {
        console.error('Search failed:', response.status);
        return '<div class="search-error">Search failed. Please try again.</div>';
      }
      
      return await response.text();
      
    } catch (error) {
      console.error('Search failed:', error);
      return '<div class="search-error">Network error. Please try again.</div>';
    }
  }

  function selectResult(item, input) {
    const id = item.dataset.type === 'playlist' ? item.dataset.playlistId : item.dataset.videoId;
    input.value = item.dataset.type === 'playlist'
      ? `https://www.youtube.com/playlist?list=${id}`
      : `https://www.youtube.com/watch?v=${id}`;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function renderResults(html, container, input) {
    selectedIndex = -1;

    container.innerHTML = `<div class="search-results">${html}</div>`;
    container.classList.remove('hidden');

    // Get all result items from the rendered HTML
    const items = container.querySelectorAll('.search-result-item');
    if (items.length === 0) {
      return;
    }

    // Add click handlers and index for keyboard navigation
    items.forEach((item, index) => {
      item.dataset.index = index;
      
      item.addEventListener('click', () => selectResult(item, input));
    });
  }

  function handleKeyNavigation(e, dropdown, input) {
    const items = dropdown.querySelectorAll('.search-result-item');
    
    if (items.length === 0) return;
    
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      selectedIndex = Math.min(selectedIndex + 1, items.length - 1);
      updateSelection(items);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      selectedIndex = Math.max(selectedIndex - 1, -1);
      updateSelection(items);
    } else if (e.key === 'Enter' && selectedIndex >= 0) {
      e.preventDefault();
      selectResult(items[selectedIndex], input);
    } else if (e.key === 'Escape') {
      dropdown.classList.add('hidden');
      selectedIndex = -1;
    }
  }

  function updateSelection(items) {
    items.forEach((item, index) => {
      if (index === selectedIndex) {
        item.classList.add('selected');
        item.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      } else {
        item.classList.remove('selected');
      }
    });
  }

  async function performSearch(inputElement, containerElement) {
    const query = inputElement.value.trim();
    
    // If it's a URL, don't search
    if (isYouTubeUrl(query)) {
      return;
    }

    // Require at least 3 characters
    if (query.length < 3) {
      return;
    }

    containerElement.innerHTML = '<div class="search-results"><div class="search-loading">Searching YouTube...</div></div>';
    containerElement.classList.remove('hidden');
    
    const html = await searchVideos(query);
    renderResults(html, containerElement, inputElement);
  }

  function init(inputElement, resultsContainer, searchButton) {
    // Search button click handler
    searchButton.addEventListener('click', () => {
      performSearch(inputElement, resultsContainer);
    });

    // Enter key in input field triggers search
    inputElement.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !resultsContainer.classList.contains('hidden')) {
        // If results are open, handle navigation
        handleKeyNavigation(e, resultsContainer, inputElement);
      } else if (e.key === 'Enter') {
        // If results are closed, perform search
        e.preventDefault();
        performSearch(inputElement, resultsContainer);
      } else if (!resultsContainer.classList.contains('hidden')) {
        // Handle other navigation keys when results are open
        handleKeyNavigation(e, resultsContainer, inputElement);
      }
    });
  }

  return { init };
})();
