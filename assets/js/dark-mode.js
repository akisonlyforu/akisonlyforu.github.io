/**
 * Dark Mode Toggle System
 * Handles theme switching with localStorage persistence and system preference detection
 */
class DarkModeToggle {
  constructor() {
    this.storageKey = 'theme-preference';
    this.toggleButton = null;
    this.currentTheme = this.getStoredTheme() || this.getSystemTheme();

    this.init();
  }

  init() {
    // Apply theme immediately to prevent FOUC
    this.applyTheme(this.currentTheme);

    // Wait for DOM to be ready
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => this.setupToggle());
    } else {
      this.setupToggle();
    }

    // Listen for system theme changes
    this.watchSystemTheme();
  }

  setupToggle() {
    this.toggleButton = document.querySelector('.dark-mode-toggle');

    if (!this.toggleButton) {
      console.warn('Dark mode toggle button not found');
      return;
    }

    // Set initial icon
    this.updateToggleIcon();

    // Add click event listener
    this.toggleButton.addEventListener('click', () => this.toggleTheme());

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
      if (theme === this.getSystemTheme()) {
        // Remove stored preference if it matches system
        localStorage.removeItem(this.storageKey);
      } else {
        localStorage.setItem(this.storageKey, theme);
      }
    } catch (e) {
      console.warn('localStorage not available');
    }
  }

  applyTheme(theme) {
    // Add transitioning class to disable transitions during theme change
    document.documentElement.classList.add('theme-transitioning');

    // Apply theme
    if (theme === 'dark') {
      document.documentElement.setAttribute('data-theme', 'dark');
    } else {
      document.documentElement.removeAttribute('data-theme');
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

  watchSystemTheme() {
    if (!window.matchMedia) return;

    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');

    const handleChange = () => {
      // Only auto-switch if user hasn't set a manual preference
      if (!this.getStoredTheme()) {
        const systemTheme = mediaQuery.matches ? 'dark' : 'light';
        this.applyTheme(systemTheme);
        this.updateToggleIcon();
      }
    };

    // Modern browsers
    if (mediaQuery.addEventListener) {
      mediaQuery.addEventListener('change', handleChange);
    } else {
      // Fallback for older browsers
      mediaQuery.addListener(handleChange);
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

  // Reset to system preference
  resetToSystem() {
    try {
      localStorage.removeItem(this.storageKey);
    } catch (e) {
      console.warn('localStorage not available');
    }

    const systemTheme = this.getSystemTheme();
    this.applyTheme(systemTheme);
    this.updateToggleIcon();
  }
}

// Initialize dark mode toggle
const darkModeToggle = new DarkModeToggle();

// Make it globally accessible for debugging/API access
window.darkModeToggle = darkModeToggle;