#!/usr/bin/env python3
"""Mirror public Substack RSS posts into the Jekyll posts collection."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable

DEFAULT_FEED_URL = "https://akisonlyforu.substack.com/feed"
RSS_PROXY_URL = "https://api.rss2json.com/v1/api.json?rss_url={}"
CONTENT_TAG = "{http://purl.org/rss/1.0/modules/content/}encoded"
GENERATED_MARKER = "<!-- Generated from Substack. Edit the Substack post instead. -->"
MAX_FEED_BYTES = 10 * 1024 * 1024
MIN_BODY_CHARS = 16
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGES_PER_POST = 40
BLOCKED_TAGS = {"script", "style", "iframe", "object", "embed", "form"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "code", "del", "details", "div", "em",
    "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
    "i", "img", "li", "ol", "p", "pre", "s", "span", "strong", "sub",
    "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
    "time", "tr", "u", "ul",
}
GLOBAL_ATTRIBUTES = {"aria-label", "class", "id", "role", "title"}
TAG_ATTRIBUTES = {
    "a": {"href", "rel", "target"},
    "img": {"alt", "height", "loading", "src", "width"},
    "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan", "scope"},
    "time": {"datetime"},
}
IMAGE_EXTENSIONS = {
    "image/avif": ".avif", "image/gif": ".gif", "image/jpeg": ".jpg",
    "image/png": ".png", "image/webp": ".webp",
}


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedPost:
    identifier: str
    title: str
    url: str
    published: datetime
    body_html: str
    description_html: str
    tags: tuple[str, ...]


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class SafeHtml(HTMLParser):
    """Preserve article HTML while removing executable and tracking markup."""

    def __init__(self, base_url: str, image_mirror: Callable[[str], str]) -> None:
        super().__init__(convert_charrefs=False)
        self.base_url = base_url
        self.image_mirror = image_mirror
        self.output: list[str] = []
        self.blocked_depth = 0
        self.first_image: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.blocked_depth:
            if tag in BLOCKED_TAGS:
                self.blocked_depth += 1
            return
        if tag in BLOCKED_TAGS:
            self.blocked_depth = 1
            return
        if tag not in ALLOWED_TAGS:
            return

        safe_attrs: list[tuple[str, str]] = []
        for name, value in attrs:
            name, value = name.lower(), value or ""
            if name not in GLOBAL_ATTRIBUTES and name not in TAG_ATTRIBUTES.get(tag, set()):
                continue
            if tag == "img" and name == "srcset":
                continue
            if name in {"href", "src"}:
                value = self._safe_url(value, image=(tag == "img" and name == "src"))
                if not value:
                    continue
            safe_attrs.append((name, value))
        rendered = "".join(f' {name}="{html.escape(value, quote=True)}"' for name, value in safe_attrs)
        self.output.append(f"<{tag}{rendered}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.blocked_depth:
            if tag in BLOCKED_TAGS:
                self.blocked_depth -= 1
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.blocked_depth:
            self.output.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self.blocked_depth:
            self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.blocked_depth:
            self.output.append(f"&#{name};")

    def _safe_url(self, value: str, image: bool) -> str:
        absolute = urllib.parse.urljoin(self.base_url, value.strip())
        parsed = urllib.parse.urlparse(absolute)
        if parsed.scheme not in {"http", "https", "mailto"}:
            return ""
        if image:
            if parsed.scheme != "https":
                return ""
            absolute = self.image_mirror(absolute)
            if self.first_image is None:
                self.first_image = absolute
        return absolute


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_host: Callable[[str], bool]) -> None:
        self.allowed_host = allowed_host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != "https" or not parsed.hostname or not self.allowed_host(parsed.hostname.lower()):
            raise SyncError(f"Refusing redirect to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(
    url: str,
    max_bytes: int,
    expected_type: str | None = None,
    allowed_host: Callable[[str], bool] | None = None,
) -> tuple[bytes, str]:
    parsed = urllib.parse.urlparse(url)
    host_check = allowed_host or (lambda host: host == (parsed.hostname or "").lower())
    if parsed.scheme != "https" or not parsed.hostname or not host_check(parsed.hostname.lower()):
        raise SyncError(f"Refusing URL outside the allowed HTTPS hosts: {url}")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            ),
            "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
        },
    )
    opener = urllib.request.build_opener(SafeRedirectHandler(host_check))
    try:
        with opener.open(request, timeout=30) as response:
            if urllib.parse.urlparse(response.geturl()).scheme != "https":
                raise SyncError(f"Refusing non-HTTPS response URL: {response.geturl()}")
            content_type = response.headers.get_content_type()
            if expected_type and not content_type.startswith(expected_type):
                raise SyncError(f"Unexpected content type {content_type!r} for {url}")
            data = response.read(max_bytes + 1)
    except (OSError, urllib.error.URLError) as error:
        raise SyncError(f"Unable to fetch {url}: {error}") from error
    if len(data) > max_bytes:
        raise SyncError(f"Response from {url} exceeds {max_bytes} bytes")
    return data, content_type


def parse_feed(data: bytes, publication_host: str) -> list[FeedPost]:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise SyncError("RSS containing DTD or entity declarations is not accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise SyncError(f"Invalid RSS XML: {error}") from error

    posts: list[FeedPost] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or url).strip()
        raw_date = (item.findtext("pubDate") or "").strip()
        parsed_url = urllib.parse.urlparse(url)
        if not title or not guid or not raw_date:
            raise SyncError("Every RSS item must contain a title, identifier, and publication date")
        if parsed_url.scheme != "https" or parsed_url.hostname != publication_host:
            raise SyncError(f"RSS item URL is outside {publication_host}: {url}")
        try:
            published = parsedate_to_datetime(raw_date)
        except (TypeError, ValueError) as error:
            raise SyncError(f"Invalid publication date for {url}: {raw_date}") from error
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        tags = tuple(value for value in ((node.text or "").strip() for node in item.findall("category")) if value)
        posts.append(FeedPost(
            guid, title, url, published,
            item.findtext(CONTENT_TAG) or item.findtext("description") or "",
            item.findtext("description") or "", tags,
        ))
    return posts


def parse_json_feed(data: bytes, publication_host: str) -> list[FeedPost]:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SyncError(f"Invalid RSS proxy JSON: {error}") from error
    if payload.get("status") != "ok" or not isinstance(payload.get("items"), list):
        raise SyncError(f"RSS proxy returned an error: {payload.get('message', 'unknown error')}")

    posts: list[FeedPost] = []
    for item in payload["items"]:
        title = str(item.get("title") or "").strip()
        url = str(item.get("link") or "").strip()
        guid = str(item.get("guid") or url).strip()
        raw_date = str(item.get("pubDate") or "").strip()
        parsed_url = urllib.parse.urlparse(url)
        if not title or not guid or not raw_date:
            raise SyncError("Every proxied RSS item must contain a title, identifier, and publication date")
        if parsed_url.scheme != "https" or parsed_url.hostname != publication_host:
            raise SyncError(f"Proxied RSS item URL is outside {publication_host}: {url}")
        try:
            published = datetime.strptime(raw_date, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError as error:
            raise SyncError(f"Invalid proxied publication date for {url}: {raw_date}") from error
        categories = item.get("categories") if isinstance(item.get("categories"), list) else []
        posts.append(FeedPost(
            guid, title, url, published,
            str(item.get("content") or item.get("description") or ""),
            str(item.get("description") or ""),
            tuple(str(value).strip() for value in categories if str(value).strip()),
        ))
    return posts


def slug_for(post: FeedPost) -> str:
    match = re.search(r"/p/([^/?#]+)", urllib.parse.urlparse(post.url).path)
    candidate = match.group(1) if match else post.title
    slug = re.sub(r"[^a-z0-9]+", "-", candidate.lower()).strip("-")
    return slug[:100] or hashlib.sha256(post.identifier.encode()).hexdigest()[:12]


def plain_description(source: str) -> str:
    parser = TextExtractor()
    parser.feed(source)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:240]


def is_placeholder(post: FeedPost) -> bool:
    """Skip empty or placeholder Substack drafts (e.g. a one-letter test post
    like "A"). These are drafts published by accident, not real articles."""
    body = plain_description(post.body_html or post.description_html)
    return len(body.strip()) < MIN_BODY_CHARS


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def existing_generated_posts(posts_dir: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    pattern = re.compile(r'^substack_id:\s*("(?:[^"\\]|\\.)*")\s*$', re.MULTILINE)
    for path in posts_dir.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        if GENERATED_MARKER not in text:
            continue
        match = pattern.search(text)
        if match:
            result[json.loads(match.group(1))] = path
    return result


def make_image_mirror(assets_root: Path, slug: str) -> Callable[[str], str]:
    count = 0

    def mirror(url: str) -> str:
        nonlocal count
        if count >= MAX_IMAGES_PER_POST:
            return url
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if (
            host != "substack-post-media.s3.amazonaws.com"
            and host != "substackcdn.com"
            and not host.endswith(".substackcdn.com")
        ):
            return url
        count += 1
        try:
            allowed_host = lambda candidate: (
                candidate == "substack-post-media.s3.amazonaws.com"
                or candidate == "substackcdn.com"
                or candidate.endswith(".substackcdn.com")
            )
            data, content_type = fetch(url, MAX_IMAGE_BYTES, "image/", allowed_host)
        except SyncError as error:
            print(f"warning: {error}; keeping remote image", file=sys.stderr)
            return url
        extension = IMAGE_EXTENSIONS.get(content_type)
        if not extension:
            print(f"warning: unsupported image type {content_type!r}; keeping remote image", file=sys.stderr)
            return url
        path = assets_root / slug / f"{hashlib.sha256(data).hexdigest()[:16]}{extension}"
        if not path.exists():
            atomic_write(path, data)
        return "/" + path.as_posix().lstrip("./")

    return mirror


def render_post(post: FeedPost, assets_root: Path, mirror_assets: bool) -> tuple[str, str]:
    slug = slug_for(post)
    image_mirror = make_image_mirror(assets_root, slug) if mirror_assets else (lambda url: url)
    sanitizer = SafeHtml(post.url, image_mirror)
    sanitizer.feed(post.body_html)
    fields: list[tuple[str, object]] = [
        ("layout", "post"), ("title", post.title), ("date", post.published.isoformat()),
        ("description", plain_description(post.description_html or post.body_html)),
        ("tags", list(dict.fromkeys(("substack", *post.tags)))),
        ("categories", ["substack"]), ("source", "substack"),
        ("substack_id", post.identifier), ("substack_url", post.url),
        ("canonical_url", post.url),
    ]
    if sanitizer.first_image:
        fields.append(("image", sanitizer.first_image))
    frontmatter = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields)
    body = "".join(sanitizer.output).strip()
    body = body.replace("{{", "&#123;&#123;").replace("{%", "&#123;%")
    return slug, f"---\n{frontmatter}\n---\n\n{GENERATED_MARKER}\n\n{body}\n"


def sync(posts: Iterable[FeedPost], posts_dir: Path, assets_root: Path, mirror_assets: bool) -> int:
    posts_dir.mkdir(parents=True, exist_ok=True)
    existing = existing_generated_posts(posts_dir)
    changes = 0
    for post in posts:
        if is_placeholder(post):
            print(f"skipping placeholder/empty post {post.url}", file=sys.stderr)
            continue
        slug, rendered = render_post(post, assets_root, mirror_assets)
        target = existing.get(post.identifier)
        if target is None:
            target = posts_dir / f"{post.published.date().isoformat()}-{slug}.md"
            if target.exists():
                suffix = hashlib.sha256(post.identifier.encode()).hexdigest()[:8]
                target = posts_dir / f"{post.published.date().isoformat()}-{slug}-{suffix}.md"
            existing[post.identifier] = target
        encoded = rendered.encode("utf-8")
        if not target.exists() or target.read_bytes() != encoded:
            atomic_write(target, encoded)
            changes += 1
            print(f"synced {post.url} -> {target}")
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed-url", default=DEFAULT_FEED_URL)
    parser.add_argument("--feed-file", type=Path)
    parser.add_argument("--posts-dir", type=Path, default=Path("collections/_posts"))
    parser.add_argument("--assets-dir", type=Path, default=Path("images/substack"))
    parser.add_argument("--no-assets", action="store_true")
    args = parser.parse_args()
    publication_host = urllib.parse.urlparse(args.feed_url).hostname
    if not publication_host:
        raise SyncError(f"Invalid feed URL: {args.feed_url}")
    if args.feed_file:
        posts = parse_feed(args.feed_file.read_bytes(), publication_host)
    else:
        separator = "&" if "?" in args.feed_url else "?"
        feed_request_url = f"{args.feed_url}{separator}sync={int(time.time()) // 900}"
        try:
            data = fetch(
                feed_request_url, MAX_FEED_BYTES, allowed_host=lambda host: host == publication_host
            )[0]
            posts = parse_feed(data, publication_host)
        except SyncError as direct_error:
            print(f"warning: direct RSS fetch failed ({direct_error}); using public RSS proxy", file=sys.stderr)
            proxy_url = RSS_PROXY_URL.format(urllib.parse.quote(feed_request_url, safe=""))
            try:
                data = fetch(
                    proxy_url, MAX_FEED_BYTES, "application/json",
                    allowed_host=lambda host: host == "api.rss2json.com",
                )[0]
                posts = parse_json_feed(data, publication_host)
            except SyncError as proxy_error:
                # Both the direct feed and the public proxy are unreachable
                # (Substack routinely 403s datacenter IPs; the proxy 500s under
                # load). No feed means nothing to sync -- treat as a clean no-op
                # rather than a red pipeline, and log both failures for triage.
                print(
                    f"warning: RSS proxy fetch also failed ({proxy_error}); "
                    "feed unreachable via direct and proxy, skipping this run",
                    file=sys.stderr,
                )
                print("Substack sync skipped: feed unreachable (0 changed file(s))")
                return 0
    changes = sync(posts, args.posts_dir, args.assets_dir, not args.no_assets)
    print(f"Substack sync complete: {len(posts)} feed post(s), {changes} changed file(s)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
