import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "download_papers.py"
SPEC = importlib.util.spec_from_file_location("download_papers", SCRIPT)
download_papers = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = download_papers
SPEC.loader.exec_module(download_papers)


class Response:
    def __init__(self, status=200, content=b"", headers=None, url="https://example.org/article"):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self.url = url
        self.ok = status < 400
        self.closed = False

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload

    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class DownloadPapersTests(unittest.TestCase):
    def test_markdown_only_receives_successful_pdfs_and_failure_keeps_pdf_results(self):
        fake_extractor = Mock()
        fake_extractor.extract_many.return_value = {"results": [], "failed": 1}
        fake_extractor.ManifestError = ValueError
        pdf_result = {"failed": 1, "results": [
            {"status": "downloaded", "path": "/tmp/new.pdf"},
            {"status": "already_present", "path": "/tmp/existing.pdf"},
            {"status": "needs_attention", "path": "/tmp/failed.pdf"}]}
        with patch.dict(sys.modules, {"download_papers_extract_markdown": fake_extractor}), \
                patch.object(download_papers, "download", return_value=pdf_result), \
                redirect_stdout(io.StringIO()) as stdout:
            exit_code = download_papers.main(["10.1234/example", "--output", "/tmp/papers", "--to-markdown"])
        fake_extractor.extract_many.assert_called_once_with(
            [Path("/tmp/new.pdf"), Path("/tmp/existing.pdf")], Path("/tmp/papers/markdown"),
            backend="mineru", timeout=900)
        delivered = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(delivered["results"][0]["status"], "downloaded")
        self.assertEqual(delivered["markdown"]["failed"], 1)

    def test_markdown_dry_run_never_loads_extractor_or_creates_files(self):
        fake_extractor = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "not-created"
            with patch.dict(sys.modules, {"download_papers_extract_markdown": fake_extractor}), \
                    redirect_stdout(io.StringIO()) as stdout:
                code = download_papers.main(["10.1234/example", "--output", str(out),
                                            "--to-markdown", "--dry-run"])
            fake_extractor.extract_many.assert_not_called()
            self.assertFalse(out.exists())
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["markdown"]["status"], "planned_after_download")

    def test_html_is_never_saved_as_pdf(self):
        session = Mock()
        session.get.return_value = Response(content=b"<html><body>article</body></html>",
                                            headers={"Content-Type": "text/html"})
        browser = Mock(return_value=(None, "dynamic_page_pdf_not_found"))
        with tempfile.TemporaryDirectory() as tmp:
            result = download_papers.download(["https://example.org/article"], Path(tmp),
                                              http_only=True, session=session, browser_fetch=browser)
            self.assertEqual(result["results"][0]["status"], "needs_attention")
            self.assertFalse(list(Path(tmp).glob("*.pdf")))
            browser.assert_not_called()

    def test_manifest_resume_skips_present_file_but_missing_file_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            paper = out / "paper.pdf"
            paper.write_bytes(b"%PDF-original")
            manifest = {"version": 1, "items": [{"identity": "https://example.org/paper",
                        "status": "downloaded", "path": str(paper), "source": "direct_url"}]}
            (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            session = Mock()
            result = download_papers.download(["https://example.org/paper"], out, session=session)
            self.assertEqual(result["results"][0]["status"], "already_present")
            session.get.assert_not_called()
            paper.unlink()
            session.get.return_value = Response(content=b"%PDF-recovered",
                                                headers={"Content-Type": "application/pdf"})
            result = download_papers.download(["https://example.org/paper"], out, session=session)
            self.assertEqual(result["results"][0]["status"], "downloaded")
            self.assertTrue(Path(result["results"][0]["path"]).is_file())

    def test_dry_run_has_no_network_or_filesystem_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "not-created"
            session = Mock()
            result = download_papers.download(["10.1234/example"], out, dry_run=True, session=session)
            self.assertEqual(result["results"][0]["status"], "planned")
            session.get.assert_not_called()
            self.assertFalse(out.exists())

    def test_403_does_not_fall_back_to_browser(self):
        session = Mock()
        session.get.return_value = Response(status=403)
        browser = Mock(return_value=(b"%PDF-should-not-run", "downloaded"))
        with tempfile.TemporaryDirectory() as tmp:
            result = download_papers.download(["https://example.org/article"], Path(tmp),
                                              session=session, browser_fetch=browser)
            self.assertEqual(result["results"][0]["status"], "needs_attention")
            self.assertIn("http_403_blocked", result["results"][0]["errors"])
            browser.assert_not_called()

    def test_unpaywall_version_and_host_are_recorded_in_manifest(self):
        session = Mock()

        def get(url, **kwargs):
            if "api.unpaywall.org" in url:
                response = Response()
                response.payload = {"best_oa_location": {"url_for_pdf": "https://repo.example/p.pdf",
                                   "version": "acceptedVersion", "host_type": "repository"}}
                return response
            if "api.crossref.org" in url:
                response = Response()
                response.payload = {"message": {}}
                return response
            return Response(content=b"%PDF-valid", headers={"Content-Type": "application/pdf"}, url=url)

        session.get.side_effect = get
        with tempfile.TemporaryDirectory() as tmp:
            result = download_papers.download(["10.1234/example"], Path(tmp), "reader@example.org", session=session)
            item = result["results"][0]
            self.assertEqual(item["status"], "downloaded")
            self.assertEqual(item["source"], "unpaywall")
            self.assertEqual(item["version"], "acceptedVersion")
            self.assertEqual(item["host_type"], "repository")
            saved = json.loads((Path(tmp) / "manifest.json").read_text())
            self.assertEqual(saved["items"][0]["version"], "acceptedVersion")

    def test_ua_pdf_stops_before_scihub_or_publisher(self):
        session = Mock()
        def get(url, **kwargs):
            response = Response(content=b"%PDF-ua", headers={"Content-Type": "application/pdf"}, url=url)
            if "api.unpaywall.org" in url:
                response.payload = {"best_oa_location": {"url_for_pdf": "https://repo.example/p.pdf"}}
            return response
        session.get.side_effect = get
        with tempfile.TemporaryDirectory() as tmp:
            result = download_papers.download(["10.1234/example"], Path(tmp), "reader@example.org", session=session)
        self.assertEqual(result["results"][0]["source"], "unpaywall")
        self.assertEqual(session.get.call_count, 2)
        self.assertFalse(any("sci-hub" in call.args[0] or "crossref" in call.args[0]
                             for call in session.get.call_args_list))

    def test_scihub_static_iframe_pdf_preserves_source_and_unknown_version(self):
        session = Mock()
        def get(url, **kwargs):
            if "sci-hub" in url and "/download/" not in url:
                return Response(content=b'<iframe id="pdf" src="/download/paper.pdf"></iframe>',
                                headers={"Content-Type": "text/html"}, url=url)
            return Response(content=b"%PDF-scihub", headers={"Content-Type": "application/pdf"}, url=url)
        session.get.side_effect = get
        with tempfile.TemporaryDirectory() as tmp:
            result = download_papers.download(["10.1234/example"], Path(tmp), session=session)
        item = result["results"][0]
        self.assertEqual(item["source"], "scihub")
        self.assertEqual(item["version"], "")
        self.assertFalse(any("crossref" in call.args[0] for call in session.get.call_args_list))

    def test_nested_scihub_html_keeps_source_and_skips_browser_when_ua_has_no_location(self):
        session = Mock()
        def get(url, **kwargs):
            if "api.unpaywall.org" in url:
                response = Response(url=url)
                response.payload = {"best_oa_location": None, "oa_locations": []}
                return response
            if "sci-hub" in url and url.endswith("10.1234/example"):
                return Response(content=b'<iframe id="pdf" src="/landing.html"></iframe>',
                                headers={"Content-Type": "text/html"}, url=url)
            if url.endswith("landing.html"):
                body = b'<meta name="citation_pdf_url" content="/paper.pdf">'
                return Response(content=body, headers={"Content-Type": "text/html"}, url=url)
            return Response(content=b"%PDF-nested", headers={"Content-Type": "application/pdf"}, url=url)
        session.get.side_effect = get
        browser = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            result = download_papers.download(["10.1234/example"], Path(tmp), "reader@example.org",
                                              session=session, browser_fetch=browser)
        item = result["results"][0]
        self.assertEqual(item["source"], "scihub")
        self.assertIn("unpaywall_no_oa_location", item["notes"])
        self.assertTrue(all(row["source"] == "scihub" for row in item["candidates"]))
        browser.assert_not_called()

    def test_scihub_escaped_download_button_resolves_to_cdn(self):
        session = Mock()
        page_url = "https://sci-hub.mk/10.1093/sf/sov044"
        pdf_url = "https://cdn.example/pdf/10.1093/sf/sov044.pdf?download=true"
        body = br'''<button onclick="location.href='\/\/cdn.example\/pdf\/10.1093\/sf\/sov044.pdf?download=true'">save</button>'''
        def get(url, **kwargs):
            if url == page_url:
                return Response(content=body, headers={"Content-Type": "text/html"}, url=url)
            self.assertEqual(url, pdf_url)
            return Response(content=b"%PDF-escaped-link", url=url)
        session.get.side_effect = get
        with tempfile.TemporaryDirectory() as tmp:
            result = download_papers.download(["10.1093/sf/sov044"], Path(tmp), session=session)
            item = result["results"][0]
            self.assertEqual(item["status"], "downloaded")
            self.assertEqual(item["source"], "scihub")
            self.assertEqual(item["actual_url"], pdf_url)
        self.assertEqual(session.get.call_count, 2)

    def test_scihub_blocked_skips_browser_and_falls_back_to_publisher(self):
        session = Mock()
        def get(url, **kwargs):
            if "sci-hub" in url:
                return Response(status=403, url=url)
            if "crossref" in url:
                response = Response(url=url)
                response.payload = {"message": {"link": [{"URL": "https://publisher.example/p.pdf"}]}}
                return response
            return Response(content=b"%PDF-publisher", headers={"Content-Type": "application/pdf"}, url=url)
        session.get.side_effect = get
        browser = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            result = download_papers.download(["10.1234/example"], Path(tmp), session=session,
                                              browser_fetch=browser)
        self.assertEqual(result["results"][0]["source"], "crossref_link")
        self.assertIn("scihub_http_403_blocked", result["results"][0]["notes"])
        browser.assert_not_called()

    def test_no_scihub_and_missing_email_are_recorded(self):
        session = Mock()
        def get(url, **kwargs):
            if "crossref" in url:
                response = Response(url=url)
                response.payload = {"message": {}}
                return response
            return Response(content=b"%PDF-publisher", headers={"Content-Type": "application/pdf"}, url=url)
        session.get.side_effect = get
        with tempfile.TemporaryDirectory() as tmp:
            result = download_papers.download(["10.1234/example"], Path(tmp), session=session, no_scihub=True)
        self.assertIn("unpaywall_lookup_skipped:no_email", result["results"][0]["notes"])
        self.assertIn("scihub_lookup_skipped:disabled", result["results"][0]["notes"])
        self.assertFalse(any("sci-hub" in call.args[0] for call in session.get.call_args_list))

    def test_scihub_base_url_rejects_credentials_query_and_fragment(self):
        for value in ("https://user:pw@example.org", "https://example.org/?x=1", "https://example.org/#x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                download_papers._validate_scihub_url(value)

    def test_casefold_dedup_and_explicit_pdf_url_identity(self):
        self.assertEqual(download_papers.normalize_doi("DOI:10.1234/AbC"), "10.1234/abc")
        self.assertEqual(download_papers.normalize_doi("https://doi.org/10.1234/AbC"), "10.1234/abc")
        explicit = "https://publisher.example/content/10.1234/AbC.pdf"
        self.assertIsNone(download_papers.normalize_doi(explicit))
        self.assertEqual(download_papers.identity_for(explicit), (explicit, "input_url"))
        session = Mock()
        session.get.return_value = Response(content=b"%PDF-content", url=explicit)
        with tempfile.TemporaryDirectory() as tmp:
            result = download_papers.download(["10.1234/AbC", "doi:10.1234/abc"], Path(tmp),
                                              session=session)
            self.assertEqual(len(result["results"]), 1)
            explicit_result = download_papers.download([explicit], Path(tmp), session=session)
            self.assertEqual(explicit_result["results"][0]["identity"], explicit)

    def test_crossref_empty_or_failure_has_doi_resolver_fallback(self):
        session = Mock()
        session.get.side_effect = RuntimeError("offline")
        candidates = download_papers.crossref_candidates("10.1234/example", session)
        self.assertEqual(candidates[0].source, "doi_resolver_fallback")
        self.assertEqual(candidates[0].url, "https://doi.org/10.1234/example")

    def test_filename_collision_never_overwrites_existing_files(self):
        identity = "https://example.org/paper"
        base = download_papers._safe_stem(identity)
        digest = __import__("hashlib").sha256(identity.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / f"{base}.pdf").write_bytes(b"user file one")
            (out / f"{base}-{digest}.pdf").write_bytes(b"user file two")
            session = Mock()
            session.get.return_value = Response(content=b"%PDF-new", url=identity)
            result = download_papers.download([identity], out, session=session)
            output_file = Path(result["results"][0]["path"])
            self.assertNotEqual(output_file.name, f"{base}.pdf")
            self.assertEqual((out / f"{base}.pdf").read_bytes(), b"user file one")
            self.assertEqual((out / f"{base}-{digest}.pdf").read_bytes(), b"user file two")
            self.assertEqual(output_file.read_bytes(), b"%PDF-new")

    def test_invalid_input_is_checkpointed_and_bad_manifest_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = download_papers.download(["not a DOI or URL"], out)
            self.assertEqual(result["failed"], 1)
            saved = json.loads((out / "manifest.json").read_text())
            self.assertEqual(saved["items"][0]["status"], "invalid_input")
            bad = out / "corrupt"
            bad.mkdir()
            manifest = bad / "manifest.json"
            manifest.write_text("{broken", encoding="utf-8")
            original = manifest.read_bytes()
            with self.assertRaises(download_papers.ManifestError):
                download_papers.download(["https://example.org/paper"], bad)
            self.assertEqual(manifest.read_bytes(), original)

    def test_http_final_url_is_recorded_and_response_closed(self):
        session = Mock()
        response = Response(content=b"%PDF-content", url="https://cdn.example/final.pdf")
        session.get.return_value = response
        with tempfile.TemporaryDirectory() as tmp:
            result = download_papers.download(["https://example.org/start.pdf"], Path(tmp), session=session)
            self.assertEqual(result["results"][0]["actual_url"], "https://cdn.example/final.pdf")
            self.assertTrue(response.closed)

    def test_invalid_content_length_is_ignored(self):
        response = Response(content=b"%PDF-content", headers={"Content-Length": "unknown"})
        session = Mock()
        session.get.return_value = response
        with tempfile.TemporaryDirectory() as tmp:
            result = download_papers.download(["https://example.org/paper.pdf"], Path(tmp), session=session)
            self.assertEqual(result["results"][0]["status"], "downloaded")

    @unittest.skipUnless(os.environ.get("RUN_DOWNLOAD_PAPERS_BROWSER_E2E") == "1",
                         "set RUN_DOWNLOAD_PAPERS_BROWSER_E2E=1 to run local Playwright E2E")
    def test_local_dynamic_html_to_pdf_headless_e2e(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/article":
                    body = (b"<html><head></head><body><script>"
                            b"const m=document.createElement('meta');"
                            b"m.name='citation_pdf_url';m.content='/paper.pdf';"
                            b"document.head.appendChild(m);</script></body></html>")
                    content_type = "text/html"
                else:
                    body = b"%PDF-1.7\nlocal-e2e\n"
                    content_type = "application/pdf"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            article_url = f"http://127.0.0.1:{server.server_port}/article"
            with tempfile.TemporaryDirectory() as tmp:
                result = download_papers.download([article_url], Path(tmp))
                item = result["results"][0]
                self.assertEqual(item["status"], "downloaded")
                self.assertEqual(item["actual_url"], f"http://127.0.0.1:{server.server_port}/paper.pdf")
                self.assertTrue(Path(item["path"]).read_bytes().startswith(b"%PDF-"))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
