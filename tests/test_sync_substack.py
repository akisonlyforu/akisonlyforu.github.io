import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
