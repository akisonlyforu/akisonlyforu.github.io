source 'https://rubygems.org'

# A simple Ruby Gem to bootstrap dependencies for setting up and
# maintaining a local Jekyll environment in sync with GitHub Pages
# https://github.com/github/pages-gem
gem 'github-pages'

gem "webrick", "~> 1.8"

gem "jekyll", "~> 4.3.2"

group :jekyll_plugins do
  gem "jekyll-feed", "~> 0.12"
  gem "jekyll-seo-tag", "~> 2.8"
  gem "jekyll-sitemap", "~> 1.4"
end

# Windows and JRuby does not include zoneinfo files, so bundle the tzinfo-data gem
platforms :mingw, :x64_mingw, :mswin, :jruby do
  gem "tzinfo", ">= 1"
  gem "tzinfo-data"
end
