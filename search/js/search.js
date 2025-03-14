//# sourceMappingURL=search.js.map

// Debug helper function
function debugLog(message, data = null) {
  if (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1') {
    console.log(`[Search Debug] ${message}`, data || '');
  }
}

// Debug state
const debugState = {
  posts: [],
  lastSearchTerm: '',
  lastResults: []
};

// Search result scoring
const SEARCH_SCORES = {
  TITLE_MATCH: 3,
  SUMMARY_MATCH: 2,
  BODY_MATCH: 1
};

document.addEventListener('DOMContentLoaded', function() {
  debugLog('Search script loaded');
  
  const searchInput = document.getElementById('search-input');
  const searchButton = document.getElementById('search-button');
  const resultsContainer = document.getElementById('search-results');
  
  if (!searchInput || !searchButton || !resultsContainer) {
    debugLog('Error: Required DOM elements not found', {
      searchInput: !!searchInput,
      searchButton: !!searchButton,
      resultsContainer: !!resultsContainer
    });
    return;
  }

  // Get the base URL for the site
  const baseUrl = window.location.origin;
  debugLog('Base URL:', baseUrl);

  // Fetch posts data using absolute URL
  fetch(baseUrl + '/search.json')
    .then(response => {
      debugLog('Response status:', response.status);
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      return response.text().then(text => {
        debugLog('Raw response:', text);
        try {
          return JSON.parse(text);
        } catch (e) {
          debugLog('JSON Parse Error:', e);
          throw new Error('Invalid JSON response');
        }
      });
    })
    .then(data => {
      debugLog('Parsed data:', data);
      if (!data || !data.posts) {
        throw new Error('Invalid data structure');
      }
      debugState.posts = data.posts;
      if (debugState.posts.length === 0) {
        resultsContainer.innerHTML = '<p>No blog posts found.</p>';
        return;
      }
      debugLog('Posts loaded:', debugState.posts.length);
      
      // If there's a search term in the URL, perform the search
      const urlParams = new URLSearchParams(window.location.search);
      const searchTerm = urlParams.get('q');
      if (searchTerm) {
        searchInput.value = searchTerm;
        performSearch(searchTerm);
      }
    })
    .catch(error => {
      debugLog('Error:', error);
      resultsContainer.innerHTML = `
        <div class="error">
          <p>Error loading search data: ${error.message}</p>
          <p>Please try again later or contact the site administrator.</p>
        </div>
      `;
    });

  function performSearch(searchTerm) {
    debugLog('Performing search:', searchTerm);
    debugState.lastSearchTerm = searchTerm;
    
    if (!searchTerm || searchTerm.length < 2) {
      resultsContainer.innerHTML = '';
      return;
    }

    searchTerm = searchTerm.toLowerCase();
    
    // Score and filter posts
    const scoredPosts = debugState.posts
      .map(post => {
        const title = (post.title || '').toLowerCase();
        const summary = (post.excerpt || '').toLowerCase();
        const body = (post.content || '').toLowerCase();
        
        let score = 0;
        let matchLocation = [];
        
        if (title.includes(searchTerm)) {
          score += SEARCH_SCORES.TITLE_MATCH;
          matchLocation.push('title');
        }
        if (summary.includes(searchTerm)) {
          score += SEARCH_SCORES.SUMMARY_MATCH;
          matchLocation.push('summary');
        }
        if (body.includes(searchTerm)) {
          score += SEARCH_SCORES.BODY_MATCH;
          matchLocation.push('body');
        }
        
        return {
          ...post,
          score,
          matchLocation
        };
      })
      .filter(post => post.score > 0)
      .sort((a, b) => b.score - a.score); // Sort by score in descending order

    debugState.lastResults = scoredPosts;
    debugLog('Search results:', {
      total: scoredPosts.length,
      scores: scoredPosts.map(p => ({ title: p.title, score: p.score, matches: p.matchLocation }))
    });
    
    displayResults(scoredPosts);
  }

  function displayResults(results) {
    debugLog('Displaying results:', results.length);
    
    if (results.length === 0) {
      resultsContainer.innerHTML = '<p>No results found.</p>';
      return;
    }

    const html = results.map(post => `
      <article class="search-result">
        <h2><a href="${post.url}" target="_blank">${post.title || 'Untitled'}</a></h2>
        <div class="post-meta">
          <span class="date">${post.date || 'No date'}</span>
          <span class="match-location">Matches in: ${post.matchLocation.join(', ')}</span>
        </div>
        <div class="post-excerpt">${post.excerpt || 'No excerpt available'}</div>
      </article>
    `).join('');

    resultsContainer.innerHTML = html;
  }

  // Event listeners
  searchInput.addEventListener('input', function() {
    debugLog('Input changed:', this.value);
    performSearch(this.value);
  });

  searchButton.addEventListener('click', function() {
    debugLog('Search button clicked');
    performSearch(searchInput.value);
  });

  searchInput.addEventListener('keypress', function(e) {
    if (e.key === 'Enter') {
      debugLog('Enter key pressed');
      e.preventDefault();
      performSearch(this.value);
    }
  });

  // Add debug info to window object for browser console access
  window.searchDebug = {
    state: debugState,
    performSearch,
    displayResults
  };
}); 