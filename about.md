---
layout: about
permalink: /about/
title: A little bit about me.
tags: about
headshot: /images/headshot.jpg
---

### Where I'm from

<img src="https://flagcdn.com/w40/in.png" width="40" alt="India Flag" style="width: 19px; height: 12px;">  I was born and raised in the southeast suburbs of Victoria, Australia — a place almost perfecting the ['surbubia as giant nursery'](http://www.paulgraham.com/nerds.html) vision of urban development.

I was raised by a tireless single mother, kept up good grades, and played way too much Call of Duty. I never wanted to program computers, right up until around 23 years old when programming computers became all I wanted to do.
### What I do now

Currently, I help build [**modal.com**](https://modal.com), a serverless cloud platform built for developers and data scientists sick of wrestling Kubernetes.
Scroll down to see some personal dashboard stats powered by Modal!

### Where I'm at now

🗽 Today, I live in NYC. When I'm not working, you can find me walking a new part of the five boroughs, or stopped in a park to read. There's a lot to love in this vast, grubby city, and I hope to see all of it by foot.

### What I used to do

I spent 3.5 years at [Canva](https://www.canva.com/), joining when it had almost 300 engineers and leaving when it had about 1800. I joined as a graduate, hobbling around with a broken leg, and ended up as team lead
of ML Platform. The whole way through I grew under the mentorship of [Greg Roodt](https://www.linkedin.com/in/groodt/).

When not doing Data or ML Platform stuff, I spent quite a bit of time working with the [Bazel](https://bazel.build/) build system, and helped maintain the [Python rules](https://github.com/bazelbuild/rules_python) for Bazel. It's the future, get on it.

Previous to Canva I worked at [Zendesk](https://www.zendesk.com/) on their [Answer Bot](https://www.zendesk.com/answer-bot/) machine learning product, and at [Atlassian](https://www.atlassian.com) as an application developer intern in their reliability/monitoring team.

<script>
/**
 * @param {String} HTML representing a single element
 * @return {Element}
 */
function htmlToElement(html) {
    var template = document.createElement('template');
    /* Never return a text node of whitespace as the result */
    html = html.trim();
    template.innerHTML = html;
    return template.content.firstChild;
}

</script>

<style>
#stats {
  background-color: #f7f7f9;
  border-radius: 1rem; 
  padding: 1.5em;
  margin-top: 2.5em;
}

#dashboard {
  margin: 0rem;
}

#dashboard code {
  background-color: #f7f7f9;
}

#recent-finished-books {
    display: flex;
    flex-direction: row;
    align-items: flex-start;
    justify-content: center;
}

#recent-finished-books a {
    color: #111;
}

.book-item {
    margin-left: 0.4em;
    margin-right: 0.4em;
}

.book-item div {
    width: 200px;
}

.book-info h4 {
    color: #222;
}

.book-info p {
    color: #555;
}

.grow-me {
  border-radius: 4px;
  transition: all .2s ease-in-out;
}

.grow-me:hover {
  transform: scale(1.02);
}

#top-spotify-tracks {
    padding-left: 1em;
}

#top-spotify-tracks li {
    color: #888;
    border-bottom: 1px solid #ededed;
    margin-top: 1rem;
}

#top-spotify-tracks a {
    color: #111;
}

#top-spotify-tracks a:hover {
    color: #1DB954; /* Spotify green */
}

#top-spotify-tracks p {
    color: #555;
}

.hidden {
    display: none;
}

@media screen and (max-width: 900px) {


  #recent-finished-books {
    flex-direction: column;
    justify-content: center;
    align-items: center;
  }

  .book-item div {
    width: 400px;
  }

  .book-item {
    display: flex;
    flex-direction: column;
    align-items: center;
  }
  
  .cover-container, .book-info {
    display: flex;
    flex-direction: column;
    align-items: center;
    max-width: 80%;
  }

  #top-spotify-tracks {
    padding-left: 1.2em;
  }
}
</style>
