(function() {
  'use strict';

  // --- Table of Contents ---
  function initTableOfContents() {
    var postContent = document.querySelector('.post-content');
    if (!postContent) return;

    var headings = postContent.querySelectorAll('h2, h3');
    if (headings.length < 3) return;

    // Ensure all headings have IDs for linking
    headings.forEach(function(heading, i) {
      if (!heading.id) {
        heading.id = 'heading-' + i;
      }
    });

    // Calculate reading time per section
    var headingArr = Array.prototype.slice.call(headings);
    var sectionTimes = [];
    for (var s = 0; s < headingArr.length; s++) {
      var start = headingArr[s];
      var end = s + 1 < headingArr.length ? headingArr[s + 1] : null;
      var wordCount = 0;
      var node = start.nextElementSibling;
      while (node && node !== end) {
        if (node.textContent) {
          wordCount += node.textContent.trim().split(/\s+/).filter(function(w) { return w.length > 0; }).length;
        }
        node = node.nextElementSibling;
      }
      var mins = Math.round(wordCount / 180);
      sectionTimes.push(mins);
    }

    // Build TOC HTML
    var tocHTML = '<nav class="toc" aria-label="Table of Contents">';
    tocHTML += '<div class="toc-header">';
    tocHTML += '<span class="toc-title">On this page</span>';
    tocHTML += '<button class="toc-toggle" aria-expanded="true" aria-label="Toggle table of contents">';
    tocHTML += '<span class="toc-toggle-icon"></span>';
    tocHTML += '</button>';
    tocHTML += '</div>';
    tocHTML += '<ul class="toc-list">';

    headings.forEach(function(heading, idx) {
      var level = heading.tagName === 'H3' ? 'toc-item-nested' : '';
      var timeLabel = sectionTimes[idx] >= 1 ? ' <span class="toc-time">' + sectionTimes[idx] + 'm</span>' : '';
      tocHTML += '<li class="toc-item ' + level + '">';
      tocHTML += '<a class="toc-link" href="#' + heading.id + '">' + heading.textContent + timeLabel + '</a>';
      tocHTML += '</li>';
    });

    tocHTML += '</ul></nav>';

    // Insert TOC before first child of post-content
    postContent.insertAdjacentHTML('afterbegin', tocHTML);

    // Toggle functionality
    var toggle = postContent.querySelector('.toc-toggle');
    var list = postContent.querySelector('.toc-list');

    toggle.addEventListener('click', function() {
      var expanded = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', !expanded);
      list.classList.toggle('toc-list-collapsed');
    });

    // Smooth scroll with nav offset
    postContent.querySelector('.toc').addEventListener('click', function(e) {
      var link = e.target.closest('.toc-link');
      if (!link) return;
      e.preventDefault();
      var target = document.querySelector(link.getAttribute('href'));
      if (target) {
        var offset = 80; // account for fixed nav
        var top = target.getBoundingClientRect().top + window.pageYOffset - offset;
        window.scrollTo({ top: top, behavior: 'smooth' });
      }
    });

    // Highlight current section based on scroll position
    var tocLinks = postContent.querySelectorAll('.toc-link');
    var tocTicking = false;

    function updateActiveLink() {
      var scrollPos = window.pageYOffset + 100; // offset for fixed nav
      var current = null;

      // Find the last heading above the scroll position
      for (var i = 0; i < headingArr.length; i++) {
        if (headingArr[i].offsetTop <= scrollPos) {
          current = headingArr[i];
        } else {
          break;
        }
      }

      tocLinks.forEach(function(link) { link.classList.remove('toc-link-active'); });
      if (current) {
        var activeLink = postContent.querySelector('.toc-link[href="#' + current.id + '"]');
        if (activeLink) activeLink.classList.add('toc-link-active');
      }
      tocTicking = false;
    }

    window.addEventListener('scroll', function() {
      if (!tocTicking) {
        requestAnimationFrame(updateActiveLink);
        tocTicking = true;
      }
    });

    updateActiveLink();
  }

  // --- Reading Progress Bar ---
  function initProgressBar() {
    var bar = document.createElement('div');
    bar.className = 'reading-progress';
    bar.innerHTML = '<div class="reading-progress-fill"></div>';
    document.body.appendChild(bar);

    var fill = bar.querySelector('.reading-progress-fill');
    var ticking = false;

    function updateProgress() {
      var scrollTop = window.pageYOffset;
      var docHeight = document.documentElement.scrollHeight - window.innerHeight;
      var progress = docHeight > 0 ? (scrollTop / docHeight) * 100 : 0;
      fill.style.width = Math.min(progress, 100) + '%';
      ticking = false;
    }

    window.addEventListener('scroll', function() {
      if (!ticking) {
        requestAnimationFrame(updateProgress);
        ticking = true;
      }
    });

    updateProgress();
  }

  // --- Copy Code Button ---
  function initCopyCode() {
    var codeBlocks = document.querySelectorAll('.post-content pre');

    codeBlocks.forEach(function(pre) {
      var btn = document.createElement('button');
      btn.className = 'copy-code-btn';
      btn.textContent = 'Copy';
      btn.setAttribute('aria-label', 'Copy code to clipboard');
      pre.appendChild(btn);

      btn.addEventListener('click', function() {
        // Get code text, excluding line numbers (user-select: none elements)
        var code = pre.querySelector('code');
        var text = '';

        if (code) {
          // If code has anchor-wrapped lines (line numbers), extract just the text
          var lines = code.querySelectorAll('a');
          if (lines.length > 0) {
            var parts = [];
            lines.forEach(function(line) {
              // textContent includes line number pseudo-element text, but
              // we want just the visible code. Clone and remove pseudo elements.
              parts.push(line.textContent);
            });
            text = parts.join('\n');
          } else {
            text = code.textContent;
          }
        } else {
          text = pre.textContent;
        }

        // Remove the "Copy" / "Copied!" button text that might be in textContent
        text = text.replace(/Copy$|Copied!$/, '').trim();

        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function() {
            showCopied(btn);
          }).catch(function() {
            fallbackCopy(text, btn);
          });
        } else {
          fallbackCopy(text, btn);
        }
      });
    });

    function fallbackCopy(text, btn) {
      var textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      try {
        document.execCommand('copy');
        showCopied(btn);
      } catch (e) {
        // silently fail
      }
      document.body.removeChild(textarea);
    }

    function showCopied(btn) {
      btn.textContent = 'Copied!';
      btn.classList.add('copy-code-btn-success');
      setTimeout(function() {
        btn.textContent = 'Copy';
        btn.classList.remove('copy-code-btn-success');
      }, 2000);
    }
  }

  // --- Image Lightbox ---
  function initLightbox() {
    var images = document.querySelectorAll('.post-content img');
    if (images.length === 0) return;

    // Create overlay
    var overlay = document.createElement('div');
    overlay.className = 'lightbox-overlay';
    var lightboxImg = document.createElement('img');
    lightboxImg.className = 'lightbox-img';
    overlay.appendChild(lightboxImg);
    document.body.appendChild(overlay);

    function closeLightbox() {
      overlay.classList.remove('lightbox-open');
    }

    images.forEach(function(img) {
      img.style.cursor = 'zoom-in';
      img.addEventListener('click', function() {
        lightboxImg.src = img.src;
        lightboxImg.alt = img.alt;
        overlay.classList.add('lightbox-open');
      });
    });

    overlay.addEventListener('click', function(e) {
      if (e.target !== lightboxImg) {
        closeLightbox();
      }
    });

    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') closeLightbox();
    });
  }

  // --- Lazy Loading & Responsive Images ---
  function initLazyImages() {
    var images = document.querySelectorAll('.post-content img');
    images.forEach(function(img) {
      // Lazy loading
      if (!img.hasAttribute('loading')) {
        img.setAttribute('loading', 'lazy');
      }

      // Async decoding for non-blocking render
      if (!img.hasAttribute('decoding')) {
        img.setAttribute('decoding', 'async');
      }

      // Set explicit dimensions to prevent CLS (once loaded)
      if (!img.hasAttribute('width') && !img.hasAttribute('height')) {
        if (img.complete && img.naturalWidth > 0) {
          img.setAttribute('width', img.naturalWidth);
          img.setAttribute('height', img.naturalHeight);
        } else {
          img.addEventListener('load', function() {
            if (img.naturalWidth > 0) {
              img.setAttribute('width', img.naturalWidth);
              img.setAttribute('height', img.naturalHeight);
            }
          }, { once: true });
        }
      }

      // Wrap images with alt text in figure/figcaption for captions
      if (img.alt && img.alt.trim() && !img.closest('figure')) {
        var parent = img.parentNode;
        // Only wrap if parent is a <p> (markdown-generated) or a direct child
        if (parent && parent.tagName === 'P' && parent.children.length === 1) {
          var figure = document.createElement('figure');
          var figcaption = document.createElement('figcaption');
          figcaption.textContent = img.alt;
          parent.parentNode.insertBefore(figure, parent);
          figure.appendChild(img);
          figure.appendChild(figcaption);
          // Remove the now-empty <p>
          if (parent.childNodes.length === 0) {
            parent.parentNode.removeChild(parent);
          }
        }
      }
    });
  }

  // --- Back to Top ---
  function initBackToTop() {
    var btn = document.createElement('button');
    btn.className = 'back-to-top';
    btn.innerHTML = '<i class="fas fa-arrow-up"></i>';
    btn.setAttribute('aria-label', 'Back to top');
    document.body.appendChild(btn);

    var ticking = false;

    function checkScroll() {
      if (window.pageYOffset > 300) {
        btn.classList.add('back-to-top-visible');
      } else {
        btn.classList.remove('back-to-top-visible');
      }
      ticking = false;
    }

    window.addEventListener('scroll', function() {
      if (!ticking) {
        requestAnimationFrame(checkScroll);
        ticking = true;
      }
    });

    btn.addEventListener('click', function() {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });

    checkScroll();
  }

  // Initialize — script loads at end of <body>, DOM is ready
  var postContent = document.querySelector('.post-content');
  if (postContent) {
    initTableOfContents();
    initProgressBar();
    initCopyCode();
    initLightbox();
    initLazyImages();
  }
  initBackToTop();
})();
