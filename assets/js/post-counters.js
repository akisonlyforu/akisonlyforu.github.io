(function () {
  var API = window.COUNTERS_API;
  var PATH = window.COUNTERS_PATH;
  if (!API || !PATH) return;

  var viewCountEl = document.getElementById('view-count');
  var upBtn = document.getElementById('vote-up');
  var downBtn = document.getElementById('vote-down');
  var upCountEl = document.getElementById('vote-up-count');
  var downCountEl = document.getElementById('vote-down-count');

  function render(stats) {
    if (viewCountEl) viewCountEl.textContent = stats.views;
    if (upCountEl) upCountEl.textContent = stats.upvotes;
    if (downCountEl) downCountEl.textContent = stats.downvotes;
    if (upBtn) upBtn.classList.toggle('active', stats.userVote === 1);
    if (downBtn) downBtn.classList.toggle('active', stats.userVote === -1);
  }

  function hideWidgets() {
    var postStats = document.getElementById('post-stats');
    var viewStat = document.getElementById('view-stat');
    if (postStats) postStats.style.display = 'none';
    if (viewStat) viewStat.style.display = 'none';
  }

  function post(endpoint, body) {
    return fetch(API + endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (res) {
      if (!res.ok) throw new Error('counters api error');
      return res.json();
    });
  }

  function vote(v) {
    post('/vote', { path: PATH, vote: v }).then(render).catch(function () {});
  }

  post('/view', { path: PATH }).then(render).catch(hideWidgets);

  if (upBtn) {
    upBtn.addEventListener('click', function () {
      vote(upBtn.classList.contains('active') ? 0 : 1);
    });
  }
  if (downBtn) {
    downBtn.addEventListener('click', function () {
      vote(downBtn.classList.contains('active') ? 0 : -1);
    });
  }
})();
