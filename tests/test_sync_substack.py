import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sync_substack", ROOT / "scripts/sync_substack.py")
sync_substack = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = sync_substack
SPEC.loader.exec_module(sync_substack)


class SyncSubstackTests(unittest.TestCase):
    def setUp(self):
        fixture = (ROOT / "tests/fixtures/substack-feed.xml").read_bytes()
        self.posts = sync_substack.parse_feed(fixture, "akisonlyforu.substack.com")

    def test_parses_namespaced_content(self):
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0].title, 'A title: "with quotes"')
        self.assertIn("Hello", self.posts[0].body_html)

    def test_sync_is_idempotent_and_sanitizes_html(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            posts_dir = root / "posts"
            self.assertEqual(sync_substack.sync(self.posts, posts_dir, root / "images", False), 1)
            self.assertEqual(sync_substack.sync(self.posts, posts_dir, root / "images", False), 0)
            generated = next(posts_dir.glob("*.md")).read_text(encoding="utf-8")
            self.assertIn('canonical_url: "https://akisonlyforu.substack.com/p/test-post"', generated)
            self.assertIn('href="https://akisonlyforu.substack.com/p/another-post"', generated)
            self.assertNotIn("<script", generated)
            self.assertNotIn("onerror", generated)
            self.assertNotIn("srcset", generated)
            self.assertNotIn("javascript:", generated)
            self.assertNotIn("<meta", generated)
            self.assertNotIn("<link", generated)
            self.assertNotIn("<base", generated)
            self.assertNotIn("{{ site.data", generated)
            self.assertNotIn("{% include", generated)
            self.assertIn("&#123;&#123; site.data", generated)

    def test_update_uses_guid_without_overwriting_handwritten_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            posts_dir = root / "posts"
            posts_dir.mkdir()
            collision = posts_dir / "2026-07-11-test-post.md"
            collision.write_text("handwritten", encoding="utf-8")
            self.assertEqual(sync_substack.sync(self.posts, posts_dir, root / "images", False), 1)
            generated = list(posts_dir.glob("*test-post-*.md"))
            self.assertEqual(len(generated), 1)
            self.assertEqual(collision.read_text(encoding="utf-8"), "handwritten")

            updated = [sync_substack.FeedPost(**{**self.posts[0].__dict__, "title": "Updated title"})]
            self.assertEqual(sync_substack.sync(updated, posts_dir, root / "images", False), 1)
            self.assertIn("Updated title", generated[0].read_text(encoding="utf-8"))
            self.assertEqual(len(list(posts_dir.glob("*.md"))), 2)

    def test_rejects_external_post_urls(self):
        fixture = (ROOT / "tests/fixtures/substack-feed.xml").read_bytes()
        fixture = fixture.replace(b"akisonlyforu.substack.com/p/test-post", b"evil.example/p/test-post")
        with self.assertRaises(sync_substack.SyncError):
            sync_substack.parse_feed(fixture, "akisonlyforu.substack.com")

    def test_mirrors_images_from_bare_substack_cdn(self):
        with tempfile.TemporaryDirectory() as temporary:
            assets = Path(temporary) / "images" / "substack"
            with mock.patch.object(sync_substack, "fetch", return_value=(b"png-data", "image/png")):
                _, generated = sync_substack.render_post(self.posts[0], assets, True)
            images = list(assets.rglob("*.png"))
            self.assertEqual(len(images), 1)
            self.assertIn("/images/substack/test-post/", generated)

    def test_skips_placeholder_posts(self):
        placeholder = sync_substack.FeedPost(**{
            **self.posts[0].__dict__,
            "identifier": "placeholder-guid",
            "url": "https://akisonlyforu.substack.com/p/a",
            "body_html": "<p>A</p>",
            "description_html": "A",
        })
        self.assertTrue(sync_substack.is_placeholder(placeholder))
        self.assertFalse(sync_substack.is_placeholder(self.posts[0]))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            posts_dir = root / "posts"
            self.assertEqual(sync_substack.sync([placeholder], posts_dir, root / "images", False), 0)
            self.assertEqual(list(posts_dir.glob("*.md")), [])

    def test_parses_rss_proxy_response(self):
        item = self.posts[0]
        payload = json.dumps({
            "status": "ok",
            "items": [{
                "title": item.title,
                "link": item.url,
                "guid": item.identifier,
                "pubDate": "2026-07-11 10:00:00",
                "description": item.description_html,
                "content": item.body_html,
                "categories": ["engineering"],
            }],
        }).encode()
        posts = sync_substack.parse_json_feed(payload, "akisonlyforu.substack.com")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].identifier, item.identifier)
        self.assertIn("Hello", posts[0].body_html)


if __name__ == "__main__":
    unittest.main()
