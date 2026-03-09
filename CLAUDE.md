# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Jekyll-based personal website hosted on GitHub Pages. The site features blog posts, portfolio content, book reviews, and personal information. The architecture follows Jekyll's conventions with custom collections and layouts.

## Development Commands

### Installation
```bash
bundle install
```

### Local Development
```bash
bundle exec jekyll serve
```
This starts a local development server, typically at `http://localhost:4000`, with auto-regeneration enabled.

### Building for Production
The site is automatically built and deployed by GitHub Pages when changes are pushed to the `master` branch.

## Architecture & Structure

### Content Organization
- `collections/_posts/` - Technical blog posts in markdown format with YAML frontmatter
- `collections/_thoughts/` - Personal musings and reflections (displayed as "Musings" on site)
- `collections/_portfolio/` - Portfolio items and projects
- `collections/_musings/` - Additional personal content

### Layout System
- `_layouts/` - Jekyll layout templates (default.html, post.html, opinion.html, page.html, post-wide.html, etc.)
- `_includes/` - Reusable template components (navigation.html, footer.html, share_buttons.html, etc.)
- `_sass/` - Sass stylesheets for styling (20+ modular SCSS partials including _homepage.scss, _dark-mode.scss)
- `css/` - Compiled CSS output

### Configuration
- `_config.yml` - Main Jekyll configuration with site settings, plugins, and collections
- `Gemfile` - Ruby dependencies, primarily using github-pages gem for compatibility
- Collections are defined in `_config.yml` under `collections:` with custom output settings

### Major Features & Implementation

#### Homepage & Navigation

- Responsive tile-based homepage (`index.md`) with 3-column grid layout
- Fixed navigation with mobile hamburger menu (`_includes/navigation.html`)
- JavaScript-powered menu toggle with smooth animations
- Current page highlighting and dropdown support

#### Search System

- Client-side search functionality (`search.html`, `search.json`, `search/js/search.js`)
- Weighted scoring system: Title (3pts), Summary (2pts), Body (1pt)
- Real-time search with URL parameter support (`?q=searchterm`)
- Debug mode enabled for localhost development

#### Contact & Social Integration

- Google Apps Script-powered contact form (`contact.md`, `assets/js/contact.js`)
- Async form handling with loading states and success/error messaging
- Social media links with Font Awesome icons (`_includes/social_links.html`)
- Support for GitHub, Twitter, LinkedIn, email, RSS, and donation platforms

#### Content Management

- Blog pagination (12 posts per page) with 47 existing posts
- Custom collections system for posts, thoughts, portfolio, and musings
- Portfolio items use `post-wide` layout for visual projects
- Categories and tags for content organization
- Search functionality supports both posts and thoughts collections

#### Styling & Assets

- Modular SCSS architecture with 20+ partial files in `_sass/`
- Dark mode support with theme toggle (`_sass/_dark-mode.scss`, `assets/js/dark-mode.js`)
- Cache-busting with timestamp queries for CSS files
- Font optimization (Inter, Source Code Pro) with preloading
- Responsive design with mobile-first approach
- CSS variables for theming throughout the codebase

#### Performance & SEO

- Google Analytics integration with gtag
- Open Graph and Twitter Card metadata
- Canonical URLs and meta descriptions
- DNS prefetching for external resources
- Instant.page preloading for faster navigation
- AnchorJS for automatic heading anchors

#### Dynamic Features

- Daily rotating philosophical quotes in footer (45+ quotes from Jordan Peterson's 12 Rules for Life)
- Date-based quote selection for consistency across visits
- Mobile-responsive hamburger menu with smooth animations
- Instant.page preloading for faster page transitions

### Content Guidelines

- Blog posts use frontmatter with layout, title, date, summary, and categories
- File naming convention: `YYYY-MM-DD-title.md` for posts
- Categories and tags are used for content organization
- Images stored in `images/` directory
- Assets (JS, CSS) in respective `js/` and `css/` directories

### GitHub Pages Compatibility

The site uses the `github-pages` gem to ensure compatibility with GitHub Pages hosting. All plugins and configurations are restricted to what GitHub Pages supports natively.

## Important Implementation Details

### Search System Architecture
The search system ([search.html](search.html), [search.json](search.json), [search/js/search.js](search/js/search.js)) uses client-side JavaScript with a weighted scoring algorithm:
- Fetches all content from `/search.json` at page load
- Combines posts and thoughts collections into a single searchable array
- Title matches score 3 points, summary matches 2 points, body matches 1 point
- Supports URL query parameters (`?q=searchterm`) for direct search links
- Debug logging enabled for localhost development

### Contact Form Integration
The contact form uses Google Apps Script as a backend:
- Form action points to a Google Apps Script web app endpoint
- Uses reCAPTCHA v3 for spam protection
- Async submission with loading states and feedback messages
- No server-side code required in this repository

### Theme System
Dark mode implementation uses:
- CSS custom properties (variables) defined in SCSS files
- `data-theme` attribute on root element to toggle themes
- JavaScript in `assets/js/dark-mode.js` handles theme switching and persistence
- Theme preference stored in localStorage

### Navigation Structure
The fixed navigation bar ([_includes/navigation.html](_includes/navigation.html)):
- Uses Liquid templating to highlight current page
- Mobile hamburger menu with JavaScript toggle functionality
- Supports dropdown menus (though currently not in use)
- CSS handles both desktop and mobile layouts with media queries
