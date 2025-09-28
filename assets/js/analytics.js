/**
 * Enhanced Google Analytics tracking for static site
 * Tracks page categories, reading progress, and user engagement
 */

// Wait for Google Analytics to load
function waitForGtag(callback) {
  if (typeof gtag !== 'undefined') {
    callback();
  } else {
    setTimeout(() => waitForGtag(callback), 100);
  }
}

// Enhanced page view tracking with metadata
function trackEnhancedPageView() {
  const pageData = {
    page_title: document.title,
    page_location: window.location.href,
    content_group1: getPageCategory(),
    content_group2: getContentType()
  };

  if (typeof gtag !== 'undefined') {
    gtag('event', 'page_view', pageData);
    gtag('event', 'enhanced_page_view', {
      event_category: 'Engagement',
      event_label: pageData.content_group1,
      custom_map: {
        custom_parameter_1: pageData.content_group2
      }
    });
  }
}

// Determine page category based on URL
function getPageCategory() {
  const path = window.location.pathname;

  if (path.includes('/blog/')) return 'Blog';
  if (path.includes('/portfolio/')) return 'Portfolio';
  if (path.includes('/thoughts/')) return 'Thoughts';
  if (path.includes('/reviews/')) return 'Reviews';
  if (path.includes('/contact')) return 'Contact';
  if (path.includes('/search')) return 'Search';
  if (path === '/' || path === '/index.html') return 'Homepage';

  return 'Other';
}

// Determine content type
function getContentType() {
  const bodyClasses = document.body.className;

  if (bodyClasses.includes('post')) return 'Post';
  if (bodyClasses.includes('page')) return 'Page';
  if (bodyClasses.includes('home')) return 'Home';

  return 'Unknown';
}

// Track reading progress for blog posts
function setupReadingProgress() {
  // Only track on blog posts
  if (!window.location.pathname.includes('/blog/')) return;

  const content = document.querySelector('.post-content, .content, article');
  if (!content) return;

  const milestones = [25, 50, 75, 100];
  const tracked = new Set();

  function checkProgress() {
    const scrollPercent = Math.round(
      (window.scrollY / (document.documentElement.scrollHeight - window.innerHeight)) * 100
    );

    milestones.forEach(milestone => {
      if (scrollPercent >= milestone && !tracked.has(milestone)) {
        tracked.add(milestone);

        if (typeof gtag !== 'undefined') {
          gtag('event', 'scroll_progress', {
            event_category: 'Engagement',
            event_label: `${milestone}%`,
            value: milestone,
            custom_map: {
              custom_parameter_1: document.title
            }
          });
        }
      }
    });
  }

  // Throttled scroll listener
  let ticking = false;
  window.addEventListener('scroll', () => {
    if (!ticking) {
      requestAnimationFrame(() => {
        checkProgress();
        ticking = false;
      });
      ticking = true;
    }
  });
}

// Track external link clicks
function setupLinkTracking() {
  document.addEventListener('click', (e) => {
    const link = e.target.closest('a');
    if (!link) return;

    const href = link.href;
    if (!href) return;

    // Track external links
    if (href.startsWith('http') && !href.includes(window.location.hostname)) {
      if (typeof gtag !== 'undefined') {
        gtag('event', 'click', {
          event_category: 'External Link',
          event_label: href,
          transport_type: 'beacon'
        });
      }
    }

    // Track social media links
    const socialDomains = ['twitter.com', 'linkedin.com', 'github.com', 'substack.com'];
    const isSocial = socialDomains.some(domain => href.includes(domain));

    if (isSocial) {
      const platform = socialDomains.find(domain => href.includes(domain)).split('.')[0];
      if (typeof gtag !== 'undefined') {
        gtag('event', 'social_click', {
          event_category: 'Social Media',
          event_label: platform,
          transport_type: 'beacon'
        });
      }
    }
  });
}

// Initialize analytics when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  waitForGtag(() => {
    trackEnhancedPageView();
    setupReadingProgress();
    setupLinkTracking();
  });
});