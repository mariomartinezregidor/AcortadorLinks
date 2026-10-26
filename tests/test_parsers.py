import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from resolve_links import (  # noqa: E402
    ResolutionCache,
    ResolveError,
    base_destination,
    cache_key,
    collect_urls,
    detect_action,
    extract_wrapper_target,
    filename_from_disposition,
    filename_from_url,
    parse_counter,
    read_urls_file,
    sanitize_filename,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ParserTests(unittest.TestCase):
    def test_extract_wrapper_target(self):
        url = (
            "https://web-api-13.vercel.app/?url="
            "https%3A%2F%2Fsofturl.in%2Ff1DPzuH"
        )
        self.assertEqual(extract_wrapper_target(url), "https://softurl.in/f1DPzuH")
        self.assertIsNone(
            extract_wrapper_target("https://example.com/?url=https://softurl.in/x")
        )

    def test_landing_form(self):
        html = (FIXTURES / "landing.html").read_text(encoding="utf-8")
        action = detect_action(html, "https://example.com/start")
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "post_form")
        self.assertEqual(action.url, "https://example.com/step")
        self.assertEqual(action.fields["go"], "abc")

    def test_adlinkfly_form(self):
        html = (FIXTURES / "adlinkfly.html").read_text(encoding="utf-8")
        action = detect_action(html, "https://short.example/abc?token=1")
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "xhr_post")
        self.assertEqual(action.url, "https://short.example/links/go")
        self.assertEqual(action.delay, 1.0)
        self.assertEqual(action.fields["_csrfToken"], "csrf123")
        self.assertEqual(action.fields["ad_form_data"], "xyz")
        self.assertIn("_Token[fields]", action.fields)

    def test_mediafire_button(self):
        html = (FIXTURES / "mediafire.html").read_text(encoding="utf-8")
        action = detect_action(html, "https://www.mediafire.com/file/abc/file.txt/file")
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "mediafire")
        self.assertEqual(
            action.url, "https://download123.mediafire.com/abc/file.txt"
        )
        self.assertEqual(action.filename, "file.txt")

    def test_meta_refresh(self):
        html = (FIXTURES / "meta_refresh.html").read_text(encoding="utf-8")
        action = detect_action(html, "https://example.com/start")
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "goto")
        self.assertEqual(action.url, "https://example.com/next")

    def test_sendnow_download_form(self):
        html = (FIXTURES / "sendnow.html").read_text(encoding="utf-8")
        action = detect_action(html, "https://send.now/iayv6819be9m")
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "post_form")
        self.assertEqual(action.url, "https://send.now/iayv6819be9m")
        self.assertEqual(action.fields["op"], "download2")
        self.assertEqual(action.fields["id"], "iayv6819be9m")

    def test_wp_safelink_with_linkr(self):
        payload = {
            "delay": "25",
            "linkr": "https://example.com/next?token=1",
        }
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        html = (
            '<html><body><form action="/step">'
            f'<input type="hidden" name="newwpsafelink" value="{encoded}"/>'
            "</form></body></html>"
        )
        action = detect_action(html, "https://example.com/start")
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "wait_get")
        self.assertEqual(action.delay, 25.0)
        self.assertEqual(action.url, "https://example.com/next?token=1")

    def test_wp_safelink_without_linkr(self):
        payload = {"delay": 5}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        html = (
            '<html><body><form action="/step">'
            f'<input type="hidden" name="newwpsafelink" value="{encoded}"/>'
            "</form></body></html>"
        )
        action = detect_action(html, "https://example.com/start")
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "wait_post_form")
        self.assertEqual(action.url, "https://example.com/step")
        self.assertEqual(action.delay, 5.0)

    def test_read_urls_file(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                "# comentario\n"
                "\n"
                "https://a.test/uno\n"
                "https://b.test/dos https://c.test/tres\n"
            )
            path = Path(handle.name)
        try:
            self.assertEqual(
                read_urls_file(path),
                [
                    "https://a.test/uno",
                    "https://b.test/dos",
                    "https://c.test/tres",
                ],
            )
            self.assertEqual(
                collect_urls([str(path), "https://d.test/cuatro"], None),
                [
                    "https://a.test/uno",
                    "https://b.test/dos",
                    "https://c.test/tres",
                    "https://d.test/cuatro",
                ],
            )
        finally:
            path.unlink(missing_ok=True)

    def test_collect_urls_rejects_invalid(self):
        with self.assertRaises(ResolveError):
            collect_urls(["no-es-una-url"], None)

    def test_cache_key_normalizes_wrapper(self):
        wrapper = "https://web-api-13.vercel.app/?url=https%3A%2F%2Fsofturl.in%2Fx"
        self.assertEqual(cache_key(wrapper), "https://softurl.in/x")
        self.assertEqual(cache_key("https://softurl.in/x"), "https://softurl.in/x")

    def test_resolution_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            cache = ResolutionCache(path, ttl=3600)
            self.assertIsNone(cache.get("k"))
            cache.put("k", "https://example.com/page", "file.txt")
            entry = cache.get("k")
            self.assertIsNotNone(entry)
            self.assertEqual(entry["page_url"], "https://example.com/page")
            self.assertEqual(entry["filename"], "file.txt")
            self.assertTrue(path.exists())
            cache.remove("k")
            self.assertIsNone(cache.get("k"))

    def test_resolution_cache_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            cache = ResolutionCache(path, ttl=0)
            cache.put("k", "https://example.com/page", "file.txt")
            time.sleep(0.01)
            self.assertIsNone(cache.get("k"))

    def test_base_destination(self):
        self.assertEqual(
            base_destination(None, "a.txt", True), Path("downloads") / "a.txt"
        )
        self.assertEqual(
            base_destination("out", "a.txt", True), Path("out") / "a.txt"
        )
        self.assertEqual(
            base_destination("out/file.txt", "a.txt", False), Path("out/file.txt")
        )

    def test_parse_counter(self):
        html = '<script>var app_vars = {"counter_value":"1"};</script>'
        self.assertEqual(parse_counter(html), 1.0)
        self.assertEqual(parse_counter('<span id="custom-timer">7</span>'), 7.0)

    def test_filename_from_disposition(self):
        self.assertEqual(
            filename_from_disposition('attachment; filename="foo bar.txt"'),
            "foo bar.txt",
        )
        self.assertEqual(
            filename_from_disposition("attachment; filename*=UTF-8''foo%20bar.txt"),
            "foo bar.txt",
        )
        self.assertIsNone(filename_from_disposition(""))

    def test_filename_from_url(self):
        self.assertEqual(
            filename_from_url("https://x.test/dir/SKY+FILE.txt"), "SKY FILE.txt"
        )

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename("../../a:b?.txt"), "a_b_.txt")
        self.assertEqual(sanitize_filename(""), "download.bin")


if __name__ == "__main__":
    unittest.main()
