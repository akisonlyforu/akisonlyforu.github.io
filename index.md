---
layout: default
tags: home
---

# 👋 Hi there <br/>

### I'm a software engineer with a focus on data & platform engineering.

<div class="feature-tiles">
  <a href="{{ site.baseurl }}/blog" class="tile">
    <div class="tile-content">
      <h3>Blog</h3>
      <p>Thoughts on distributed systems, engineering, and technology</p>
      <span class="tile-arrow">→</span>
    </div>
  </a>
  
  <a href="{{ site.baseurl }}/library" class="tile">
    <div class="tile-content">
      <h3>Library</h3>
      <p>Books I've read and recommend</p>
      <span class="tile-arrow">→</span>
    </div>
  </a>
  
  <a href="{{ site.baseurl }}/about" class="tile">
    <div class="tile-content">
      <h3>About Me</h3>
      <p>My journey, work, and interests</p>
      <span class="tile-arrow">→</span>
    </div>
  </a>

  <a href="{{ site.baseurl }}/projects" class="tile">
    <div class="tile-content">
      <h3>Projects</h3>
      <p>Open source contributions and personal projects</p>
      <span class="tile-arrow">→</span>
    </div>
  </a>

  <a href="{{ site.baseurl }}/notes" class="tile">
    <div class="tile-content">
      <h3>Notes</h3>
      <p>Technical notes, snippets, and documentation</p>
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
  gap: 2rem;
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
  background: #ffffff;
  border-radius: 12px;
  padding: 2rem;
  text-decoration: none;
  color: inherit;
  transition: all 0.3s ease;
  border: 1px solid rgba(0, 0, 0, 0.1);
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
  background: linear-gradient(45deg, #dcf3ff, #aedbf9);
  opacity: 0;
  transition: opacity 0.3s ease;
  z-index: 1;
}

.tile:hover {
  transform: translateY(-5px);
  box-shadow: 0 10px 20px rgba(0, 0, 0, 0.1);
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
  color: #333;
}

.tile p {
  margin: 0;
  font-size: 1rem;
  color: #666;
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
</style>