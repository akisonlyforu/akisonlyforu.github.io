/**
 * Dark Mode Toggle System
 * Handles theme switching with localStorage persistence and system preference detection
 */
class DarkModeToggle {
  constructor() {
    this.storageKey = 'theme-preference';
    this.toggleButton = null;

    // Always start with light theme by default
    this.currentTheme = 'light';
    const storedTheme = this.getStoredTheme();

    // Only use stored preference if user has explicitly set one before
    if (storedTheme) {
      this.currentTheme = storedTheme;
    }

    this.init();
  }

  init() {
    // Apply theme immediately to prevent FOUC - even before DOM is ready
    this.applyTheme(this.currentTheme);

    // Wait for DOM to be ready
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => {
        this.setupToggle();
        // Re-apply theme once DOM is ready to ensure all elements get it
        this.applyTheme(this.currentTheme);
      });
    } else {
      this.setupToggle();
      // Re-apply theme if DOM is already ready
      this.applyTheme(this.currentTheme);
    }

    // Note: No longer watching system theme changes - site defaults to light mode
  }

  setupToggle() {
    this.toggleButton = document.querySelector('.dark-mode-toggle');

    if (!this.toggleButton) {
      console.error('Dark mode toggle button not found');
      return;
    }

    // Set initial icon
    this.updateToggleIcon();

    // Add click event listener
    this.toggleButton.addEventListener('click', () => {
      this.toggleTheme();
    });

    // Add keyboard support
    this.toggleButton.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        this.toggleTheme();
      }
    });
  }

  getStoredTheme() {
    try {
      return localStorage.getItem(this.storageKey);
    } catch (e) {
      console.warn('localStorage not available');
      return null;
    }
  }

  getSystemTheme() {
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
      return 'dark';
    }
    return 'light';
  }

  setStoredTheme(theme) {
    try {
      // Always store user's explicit choice
      localStorage.setItem(this.storageKey, theme);
    } catch (e) {
      console.warn('localStorage not available');
    }
  }

  applyTheme(theme) {
    // Add transitioning class to disable transitions during theme change
    document.documentElement.classList.add('theme-transitioning');

    // Apply theme to both html and body
    if (theme === 'dark') {
      document.documentElement.setAttribute('data-theme', 'dark');
      if (document.body) {
        document.body.setAttribute('data-theme', 'dark');
      }
    } else {
      document.documentElement.removeAttribute('data-theme');
      if (document.body) {
        document.body.removeAttribute('data-theme');
      }
    }

    this.currentTheme = theme;

    // Remove transitioning class after a brief delay
    setTimeout(() => {
      document.documentElement.classList.remove('theme-transitioning');
    }, 50);

    // Update meta theme-color for mobile browsers
    this.updateMetaThemeColor(theme);

    // Dispatch custom event for other components to listen to
    window.dispatchEvent(new CustomEvent('themechange', {
      detail: { theme }
    }));
  }

  updateMetaThemeColor(theme) {
    let metaThemeColor = document.querySelector('meta[name="theme-color"]');

    if (!metaThemeColor) {
      metaThemeColor = document.createElement('meta');
      metaThemeColor.name = 'theme-color';
      document.head.appendChild(metaThemeColor);
    }

    metaThemeColor.content = theme === 'dark' ? '#1a1a1a' : '#ffffff';
  }

  updateToggleIcon() {
    if (!this.toggleButton) return;

    const icon = this.toggleButton.querySelector('.icon');
    if (!icon) return;

    // Add rotation class for animation
    this.toggleButton.classList.add('rotating');

    setTimeout(() => {
      icon.textContent = this.currentTheme === 'dark' ? '☀️' : '🌙';
      this.toggleButton.classList.remove('rotating');
    }, 150);

    // Update ARIA label
    const label = this.currentTheme === 'dark'
      ? 'Switch to light mode'
      : 'Switch to dark mode';
    this.toggleButton.setAttribute('aria-label', label);
  }

  toggleTheme() {
    const newTheme = this.currentTheme === 'dark' ? 'light' : 'dark';

    this.applyTheme(newTheme);
    this.setStoredTheme(newTheme);
    this.updateToggleIcon();

    // Track theme change for analytics if available
    if (typeof gtag !== 'undefined') {
      gtag('event', 'theme_change', {
        'event_category': 'UI',
        'event_label': newTheme
      });
    }
  }


  // Public API
  setTheme(theme) {
    if (theme === 'dark' || theme === 'light') {
      this.applyTheme(theme);
      this.setStoredTheme(theme);
      this.updateToggleIcon();
    }
  }

  getTheme() {
    return this.currentTheme;
  }

  // Reset to default light theme
  resetToDefault() {
    try {
      localStorage.removeItem(this.storageKey);
    } catch (e) {
      console.warn('localStorage not available');
    }

    this.applyTheme('light');
    this.updateToggleIcon();
  }
}

// Initialize dark mode toggle
const darkModeToggle = new DarkModeToggle();

// Make it globally accessible for debugging/API access
window.darkModeToggle = darkModeToggle;

// Debug functions for testing
window.debugDarkMode = {
  forceLight: () => darkModeToggle.setTheme('light'),
  forceDark: () => darkModeToggle.setTheme('dark'),
  getCurrentTheme: () => darkModeToggle.getTheme(),
  checkDataTheme: () => document.documentElement.getAttribute('data-theme'),
  listCSSVariables: () => {
    const styles = getComputedStyle(document.documentElement);
    const variables = {};
    for (let i = 0; i < styles.length; i++) {
      const name = styles[i];
      if (name.startsWith('--')) {
        variables[name] = styles.getPropertyValue(name).trim();
      }
    }
    return variables;
  }
};