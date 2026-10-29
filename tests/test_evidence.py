import asyncio
import hashlib
import unittest
from unittest.mock import patch

import httpx

from app.evidence import MAX_BYTES, fetch_evidence


async def public_resolver(host, port):
    return ["93.184.216.34"]


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_text_entities_and_tls_pin(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text="""
              <html><head><title>研究 &amp; 证据</title><script>evil</script></head>
              <nav>导航不要</nav><article>次要文章</article><main><h1>研究正文</h1>
              <p>保留 &lt;假设&gt; 与 A&amp;B。</p><aside>推荐不要</aside>
              <style>css不要</style><form>表单不要</form><p>第二段。</p></main><footer>底部不要</footer></html>""")

        result = await fetch_evidence(["https://example.org/page#fragment"], transport=httpx.MockTransport(handler), resolver=public_resolver)
        self.assertEqual(result["errors"], [])
        source = result["sources"][0]
        self.assertEqual(source["title"], "研究 & 证据")
        self.assertIn("保留 <假设> 与 A&B。", source["excerpt"])
        self.assertIn("第二段。", source["excerpt"])
        for removed in ["evil", "导航不要", "次要文章", "推荐不要", "css不要", "表单不要", "底部不要"]:
            self.assertNotIn(removed, source["excerpt"])
        self.assertEqual(source["url"], "https://example.org/page")
        self.assertEqual(source["id"], "src-" + hashlib.sha256(source["url"].encode()).hexdigest()[:12])
        self.assertTrue(source["verified_content"])
        self.assertEqual(source["provenance"], "fetched_page")
        self.assertEqual(requests[0].url.host, "93.184.216.34")
        self.assertEqual(requests[0].headers["host"], "example.org")
        self.assertEqual(requests[0].extensions["sni_hostname"], "example.org")

    async def test_private_and_special_targets_never_connect(self):
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="not allowed")

        for url in ["http://127.0.0.1/", "http://10.1.2.3/", "http://169.254.169.254/", "http://[::1]/",
                    "http://[::ffff:127.0.0.1]/", "http://100.64.0.1/", "http://224.0.0.1/",
                    "file:///etc/passwd", "https://user:secret@example.org/", "https://example.org:8000/", "https://example.org:0/"]:
            with self.subTest(url=url):
                result = await fetch_evidence([url], transport=httpx.MockTransport(handler), resolver=public_resolver)
                self.assertFalse(result["sources"])
                self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(called, [])

    async def test_mixed_dns_answer_is_rejected(self):
        async def mixed(host, port):
            return ["8.8.8.8", "192.168.0.2"]

        def forbidden(request):
            self.fail("Mixed public/private DNS must never connect")

        result = await fetch_evidence(["https://example.org/"], resolver=mixed, transport=httpx.MockTransport(forbidden))
        self.assertEqual(result["errors"][0]["reason"], "blocked_non_public_address")

    async def test_redirect_to_private_address_is_rejected(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "http://127.0.0.1/metadata"})

        result = await fetch_evidence(["https://example.org/"], resolver=public_resolver, transport=httpx.MockTransport(handler))
        self.assertFalse(result["sources"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["errors"][0]["reason"], "blocked_non_public_address")

    async def test_relative_redirect_resolves_again_and_retains_public_url(self):
        dns_calls = []

        async def resolver(host, port):
            dns_calls.append(host)
            return ["8.8.8.8"]

        def handler(request):
            if request.url.path == "/first":
                return httpx.Response(302, headers={"location": "/final"})
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="实际正文")

        result = await fetch_evidence(["https://example.org/first"], resolver=resolver, transport=httpx.MockTransport(handler))
        self.assertEqual(result["sources"][0]["url"], "https://example.org/final")
        self.assertEqual(dns_calls, ["example.org", "example.org"])

    async def test_redirect_limit(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={"location": "/next"})

        result = await fetch_evidence(["https://example.org/"], resolver=public_resolver, transport=httpx.MockTransport(handler))
        self.assertEqual(len(calls), 4)
        self.assertEqual(result["errors"][0]["reason"], "too_many_redirects")

    async def test_stream_limit_without_content_length(self):
        class TooLarge(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(17):
                    yield b"x" * 65536

        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/plain"}, stream=TooLarge())

        result = await fetch_evidence(["https://example.org/"], resolver=public_resolver, transport=httpx.MockTransport(handler))
        self.assertEqual(result["errors"][0]["reason"], "content_too_large")

    async def test_content_length_limit_and_pdf_are_skipped(self):
        for headers, content, reason in [
            ({"content-type": "text/plain", "content-length": str(MAX_BYTES + 1)}, b"x", "content_too_large"),
            ({"content-type": "application/pdf"}, b"%PDF-1.0", "unsupported_content_type:application/pdf"),
            ({"content-type": "text/plain"}, b"%PDF-1.0", "unsupported_content_type:application/pdf"),
        ]:
            with self.subTest(reason=reason):
                result = await fetch_evidence(["https://example.org/"], resolver=public_resolver,
                                              transport=httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, content=content)))
                self.assertEqual(result["errors"][0]["reason"], reason)

    async def test_total_timeout_includes_dns(self):
        async def slow(host, port):
            await asyncio.sleep(2)
            return ["8.8.8.8"]

        with patch("app.evidence.TOTAL_TIMEOUT", 0.02):
            result = await fetch_evidence(["https://example.org/"], resolver=slow, transport=httpx.MockTransport(lambda request: None))
        self.assertEqual(result["errors"][0]["reason"], "total_timeout")

    async def test_excerpt_limit_and_source_count(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<article>" + "正文" * 9000 + "</article>")

        result = await fetch_evidence(["https://example.org/1", "https://example.org/1", "https://example.org/2", "https://example.org/3", "https://example.org/4"],
                                      max_sources=3, resolver=public_resolver, transport=httpx.MockTransport(handler))
        self.assertEqual(len(result["sources"]), 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(result["sources"][0]["excerpt"]), 12000)

    async def test_pathological_html_nesting_is_bounded(self):
        result = await fetch_evidence(["https://example.org/"], resolver=public_resolver,
                                      transport=httpx.MockTransport(lambda request: httpx.Response(
                                          200, headers={"content-type": "text/html"}, text="<div>" * 300 + "text")))
        self.assertEqual(result["errors"][0]["reason"], "html_nesting_too_deep")


if __name__ == "__main__":
    unittest.main()
