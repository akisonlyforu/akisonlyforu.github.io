---
layout: default
tags: home
title: Backend Engineering Deep Dives
description: Practical backend engineering deep dives on distributed systems, databases, performance, and software architecture.
---

# 👋 Hi there <br/>

### I break backend systems on purpose, then write down what I learned.

Backend engineer (full stack AI Vibecoder). I spend my time in the unglamorous middle of distributed systems: the races, the memory that won't come back, the query that got slow for reasons nobody logged. When something bites me, I reproduce it on a laptop, measure it, and write the post I wish I'd read first.


<div class="feature-tiles">
  <a href="{{ site.baseurl }}/blog" class="tile">
    <div class="tile-content">
      <h3>Tech Deep Dives</h3>
      <p>Thoughts on distributed systems, engineering, and technology</p>
      <span class="tile-arrow">→</span>
    </div>
  </a>
  
  <a href="{{ site.baseurl }}/thoughts" class="tile">
    <div class="tile-content">
      <h3>Musings</h3>
      <p>Observations, Reflections & Perspectives I gained along the journey</p>
      <span class="tile-arrow">→</span>
    </div>
  </a>

  <a href="{{ site.baseurl }}/library" class="tile">
    <div class="tile-content">
      <h3>Library</h3>
      <p>Books I've read/want to read and recommend</p>
      <span class="tile-arrow">→</span>
    </div>
  </a>

  <a href="{{ site.baseurl }}/bookmarks" class="tile">
    <div class="tile-content">
      <h3>Bookmarks</h3>
      <p>Collection of useful reads and references</p>
      <span class="tile-arrow">→</span>
    </div>
  </a>

  <a href="{{ site.baseurl }}/interview" class="tile">
    <div class="tile-content">
      <h3>Interview Prep</h3>
      <p>DS, system design, LLD &amp; multithreading references</p>
      <span class="tile-arrow">→</span>
    </div>
  </a>

  <a href="{{ site.baseurl }}/contact" class="tile">
    <div class="tile-content">
      <h3>Contact</h3>
      <p>Get in touch with me</p>
      <span class="tile-arrow">→</span>
    </div>
  </a>
</div>

<style>
.feature-tiles {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1.5rem;
  margin: 3rem auto;
  max-width: 1200px;
  padding: 0 1rem;
}

@media (max-width: 768px) {
  .feature-tiles {
    grid-template-columns: 1fr;
  }
}

.tile {
  position: relative;
  background: var(--tile-bg);
  border-radius: 12px;
  padding: 2rem;
  text-decoration: none;
  color: inherit;
  transition: all 0.3s ease;
  border: 1px solid var(--tile-border);
  overflow: hidden;
  min-height: 200px;
  display: flex;
  flex-direction: column;
}

.tile::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: var(--tile-gradient);
  opacity: 0;
  transition: opacity 0.3s ease;
  z-index: 1;
}

.tile:hover {
  transform: translateY(-5px);
  box-shadow: var(--tile-shadow-hover);
}

.tile:hover::before {
  opacity: 0.1;
}

.tile-content {
  position: relative;
  z-index: 2;
  flex: 1;
  display: flex;
  flex-direction: column;
}

.tile h3 {
  margin: 0 0 1rem 0;
  font-size: 1.5rem;
  color: var(--text-primary);
}

.tile p {
  margin: 0;
  font-size: 1rem;
  color: var(--text-secondary);
  line-height: 1.5;
  flex-grow: 1;
}

.tile-arrow {
  position: relative;
  font-size: 1.5rem;
  opacity: 0;
  transform: translateX(-10px);
  transition: all 0.3s ease;
  align-self: flex-end;
  margin-top: 1rem;
}

.tile:hover .tile-arrow {
  opacity: 1;
  transform: translateX(0);
}

.tile.disabled {
  cursor: not-allowed;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
}

.tile.disabled:hover {
  transform: none;
  box-shadow: none;
}

.tile.disabled:hover::before {
  opacity: 0;
}

.tile.disabled h3,
.tile.disabled p {
  color: var(--text-tertiary);
}

.tile-status {
  font-size: 0.9rem;
  color: var(--text-tertiary);
  margin-top: 1rem;
  align-self: flex-end;
}
</style>
