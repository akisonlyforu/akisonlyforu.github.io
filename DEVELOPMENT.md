## Development Documentation

This information is only really useful for the site owner.

### Build, Test, Local-Dev

#### Install

`bundle install`

#### Run Locally

`bundle exec jekyll server`

### Substack Sync

Public posts from `https://akisonlyforu.substack.com/feed` are mirrored into
`collections/_posts` by `.github/workflows/sync-substack.yml`. The workflow runs
every 15 minutes and can also be started manually from GitHub's Actions tab.

Run the importer locally with:

`python3 scripts/sync_substack.py`

Imported posts use their Substack URL as the canonical URL. Edit imported content
in Substack; the next sync updates the generated Jekyll post.

### Library/Anti-Library

TODO
