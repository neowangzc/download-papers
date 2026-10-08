#!/usr/bin/env python3
"""Download papers from DOI records, OA locations, or explicit article URLs."""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import os
import re
import sys
import tempfile
from html.parser import HTMLParser
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse

import requests

USER_AGENT = "download-papers/1.0 (academic paper downloader)"
TIMEOUT = (10, 30)
MAX_BYTES = 100 * 1024 * 1024
DOI_RE = re.compile(r"^10\.\d{4,9}/[^\s<>\"']+$", re.I)
DEFAULT_SCIHUB_URL = "https://sci-hub.mk"


class ManifestError(ValueError):
    """Existing checkpoint is unreadable or has an unsupported structure."""


@dataclass
class Candidate:
    url: str
    source: str
    version: str = ""
    host_type: str = ""
    identity_basis: str = ""
    payload: bytes | None = None
    actual_url: str = ""


def normalize_doi(value: str) -> str | None:
    value = value.strip()
    if value.lower().startswith("doi:"):
        value = value[4:].strip()
    else:
        parsed = urlparse(value)
        if parsed.scheme.lower() in {"http", "https"} and parsed.netloc.lower() in {
            "doi.org", "www.doi.org", "dx.doi.org", "www.dx.doi.org"
        }:
            value = unquote(parsed.path.lstrip("/"))
        elif parsed.scheme or parsed.netloc:
            return None
    decoded = unquote(value)
    return decoded.casefold() if DOI_RE.fullmatch(decoded) else None


def identity_for(value: str) -> tuple[str, str]:
    doi = normalize_doi(value)
    return (doi, "doi") if doi else (value.strip(), "input_url")


def crossref_candidates(doi: str, session: Any = requests) -> list[Candidate]:
    try:
        response = session.get(f"https://api.crossref.org/works/{quote(doi, safe='')}",
                               headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        try:
            response.raise_for_status()
            message = response.json().get("message", {})
        finally:
            response.close()
    except Exception:
        message = {}
    result: list[Candidate] = []
    for link in message.get("link", []) or []:
        url = link.get("URL")
        if url:
            result.append(Candidate(url, "crossref_link", identity_basis="doi"))
    landing = message.get("URL")
    if landing:
        result.append(Candidate(landing, "crossref_landing", identity_basis="doi"))
    if not result:
        result.append(Candidate(f"https://doi.org/{quote(doi, safe='/')}",
                                 "doi_resolver_fallback", identity_basis="doi"))
    return result


def unpaywall_candidates(doi: str, email: str, session: Any = requests) -> list[Candidate]:
    response = session.get(f"https://api.unpaywall.org/v2/{quote(doi, safe='')}",
                           params={"email": email}, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    try:
        response.raise_for_status()
        data = response.json()
    finally:
        response.close()
    locations = ([data.get("best_oa_location")] if data.get("best_oa_location") else [])
    locations += data.get("oa_locations", []) or []
    result: list[Candidate] = []
    seen: set[str] = set()
    for location in locations:
        if not location:
            continue
        url = location.get("url_for_pdf") or location.get("url")
        if url and url not in seen:
            seen.add(url)
            result.append(Candidate(url, "unpaywall", str(location.get("version") or ""),
                                    str(location.get("host_type") or ""), "doi"))
    return result[:6]


class _SciHubLinks(HTMLParser):
    """Extract explicit PDF targets from static Sci-Hub article markup."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs = [(key.lower(), value or "") for key, value in attrs]
        values = dict(attrs)
        tag = tag.lower()
        if tag == "iframe" and values.get("id", "").lower() == "pdf" and values.get("src"):
            self.links.append(values["src"])
        if tag in {"embed", "object"}:
            value = values.get("src") or values.get("data")
            if value and (tag == "embed" and "pdf" in values.get("type", "").lower()
                          or tag == "object" and ("pdf" in values.get("type", "").lower()
                                                   or value.lower().endswith(".pdf"))):
                self.links.append(value)
        if tag in {"a", "button"}:
            href = values.get("href", "")
            if href and ("pdf" in href.lower() or "download" in values.get("class", "").lower()
                         or "pdf" in values.get("type", "").lower()):
                self.links.append(href)
            onclick = values.get("onclick", "")
            match = re.search(r"(?:window\.)?location(?:\.href)?\s*=\s*(['\"])(.*?)\1", onclick, re.I)
            if match:
                self.links.append(html.unescape(match.group(2)))


def scihub_candidate(doi: str, base_url: str, session: Any = requests,
                    candidate_log: list[dict[str, str]] | None = None) -> tuple[Candidate | None, str]:
    """Resolve one static Sci-Hub DOI page; never execute its JavaScript."""
    # Preserve DOI path separators so the site does not echo encoded slashes
    # into a doubly encoded PDF path.
    page_url = base_url.rstrip("/") + "/" + quote(doi, safe="/")
    log_entry = {"url": page_url, "actual_url": page_url, "source": "scihub", "version": "",
                 "host_type": "", "status": "not_attempted"}
    if candidate_log is not None:
        candidate_log.append(log_entry)
    try:
        response = session.get(page_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                               allow_redirects=True, stream=True)
    except requests.RequestException as exc:
        log_entry["status"] = f"needs_attention:request_failed:{type(exc).__name__}"
        return None, f"scihub_request_failed:{type(exc).__name__}"
    try:
        page_url = str(getattr(response, "url", "") or page_url)
        log_entry["actual_url"] = page_url
        if response.status_code == 404:
            log_entry["status"] = "scihub_not_found"
            return None, "scihub_not_found"
        if response.status_code in (403, 429) or response.status_code == 503:
            log_entry["status"] = f"needs_attention:http_{response.status_code}_blocked"
            return None, f"scihub_http_{response.status_code}_blocked"
        if not response.ok:
            log_entry["status"] = f"scihub_http_{response.status_code}"
            return None, f"scihub_http_{response.status_code}"
        length = response.headers.get("Content-Length")
        if _content_length_exceeds_limit(length):
            log_entry["status"] = "file_too_large"
            return None, "scihub_file_too_large"
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_BYTES:
                log_entry["status"] = "file_too_large"
                return None, "scihub_file_too_large"
            chunks.append(chunk)
        body = b"".join(chunks)
        if len(body) > MAX_BYTES:
            log_entry["status"] = "file_too_large"
            return None, "scihub_file_too_large"
        if body.startswith(b"%PDF-"):
            log_entry["status"] = "downloaded"
            return Candidate(page_url, "scihub", identity_basis="doi", payload=body,
                             actual_url=page_url), "downloaded"
        if re.search(rb"captcha|cloudflare|checking your browser", body[:200_000], re.I):
            log_entry["status"] = "needs_attention:interstitial"
            return None, "scihub_interstitial_needs_attention"
        parser = _SciHubLinks()
        parser.feed(body.decode("utf-8", errors="replace"))
        for link in parser.links[:5]:
            # Static onclick strings commonly contain JavaScript-escaped /.
            # Decode that literal spelling without executing the script.
            link = html.unescape(link).replace("\\/", "/").strip().split("#", 1)[0]
            resolved = urljoin(str(getattr(response, "url", "") or page_url), link)
            if _valid_http_url(resolved):
                log_entry["status"] = "scihub_candidate_found"
                return Candidate(resolved, "scihub", identity_basis="doi"), "scihub_candidate_found"
        log_entry["status"] = "scihub_pdf_not_found"
        return None, "scihub_pdf_not_found"
    except Exception as exc:
        log_entry["status"] = f"needs_attention:request_failed:{type(exc).__name__}"
        return None, f"scihub_request_failed:{type(exc).__name__}"
    finally:
        response.close()


def _pdf_candidate_links(page_url: str, body: bytes) -> list[Candidate]:
    text = body.decode("utf-8", errors="replace")
    found: list[Candidate] = []
    # Only article-specific PDF metadata is followed. General attachment links are ignored.
    for match in re.finditer(r"<meta\b[^>]*(?:name|property)\s*=\s*['\"]citation_pdf_url['\"][^>]*>", text, re.I):
        tag = match.group(0)
        content = re.search(r"content\s*=\s*['\"]([^'\"]+)", tag, re.I)
        if content:
            found.append(Candidate(urljoin(page_url, html.unescape(content.group(1))), "citation_pdf_url"))
    for match in re.finditer(r"<link\b[^>]*>", text, re.I):
        tag = match.group(0)
        rel = re.search(r"rel\s*=\s*['\"]([^'\"]+)", tag, re.I)
        typ = re.search(r"type\s*=\s*['\"]([^'\"]+)", tag, re.I)
        href = re.search(r"href\s*=\s*['\"]([^'\"]+)", tag, re.I)
        if href and ((rel and "alternate" in rel.group(1).lower() and typ and "pdf" in typ.group(1).lower())
                     or (typ and typ.group(1).lower() == "application/pdf")):
            found.append(Candidate(urljoin(page_url, html.unescape(href.group(1))), "article_pdf_link"))
    unique: list[Candidate] = []
    seen: set[str] = set()
    for item in found:
        if item.url not in seen:
            seen.add(item.url)
            unique.append(item)
    return unique[:5]


def _arxiv_pdf_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"arxiv.org", "www.arxiv.org"}:
        return None
    path = re.sub(r"^/(?:abs|html)/", "/pdf/", parsed.path)
    if path.startswith("/pdf/"):
        return "https://arxiv.org" + path.removesuffix(".pdf") + ".pdf"
    return None


def _fetch_http(candidate: Candidate, session: Any = requests) -> tuple[bytes | None, str, list[Candidate], str]:
    url = _arxiv_pdf_url(candidate.url) or candidate.url
    final_url = url
    try:
        response = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                               allow_redirects=True, stream=True)
    except requests.RequestException as exc:
        return None, f"request_failed:{type(exc).__name__}", [], url
    try:
        final_url = str(getattr(response, "url", "") or url)
        if response.status_code in (401, 403, 429):
            return None, f"http_{response.status_code}_blocked", [], final_url
        if not response.ok:
            return None, f"http_{response.status_code}", [], final_url
        length = response.headers.get("Content-Length")
        if _content_length_exceeds_limit(length):
            return None, "file_too_large", [], final_url
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_BYTES:
                return None, "file_too_large", [], final_url
            chunks.append(chunk)
        payload = b"".join(chunks)
        if payload.startswith(b"%PDF-"):
            return payload, "downloaded", [], final_url
        ctype = response.headers.get("Content-Type", "").lower()
        if "html" in ctype or payload.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            return None, "html_page", _pdf_candidate_links(final_url, payload), final_url
        return None, "response_not_pdf", [], final_url
    except requests.RequestException as exc:
        return None, f"request_failed:{type(exc).__name__}", [], final_url
    finally:
        response.close()


def _fetch_headless(candidate: Candidate) -> tuple[bytes | None, str, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "dynamic_page_needs_playwright_install: install Playwright in the skill environment", candidate.url
    browser = None
    context = None
    page_url = candidate.url
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            response = page.goto(candidate.url, wait_until="domcontentloaded", timeout=30_000)
            if response and response.status in (401, 403, 429):
                return None, f"http_{response.status}_blocked", page.url
            page.wait_for_timeout(1500)
            # Reuse only article PDF metadata, never arbitrary links or UI controls.
            page_url = page.url
            links = _pdf_candidate_links(page_url, page.content().encode("utf-8"))
            downloaded = None
            for linked in links:
                api_response = context.request.get(linked.url, timeout=30_000)
                actual_url = str(getattr(api_response, "url", "") or linked.url)
                try:
                    if api_response.status in (401, 403, 429):
                        continue
                    if not api_response.ok:
                        continue
                    length = api_response.headers.get("content-length") or api_response.headers.get("Content-Length")
                    if _content_length_exceeds_limit(length):
                        continue
                    payload = api_response.body()
                    if len(payload) <= MAX_BYTES and payload.startswith(b"%PDF-"):
                        downloaded = (payload, "downloaded", actual_url)
                        break
                finally:
                    api_response.dispose()
            if downloaded:
                return downloaded
    except Exception as exc:
        detail = "install Chromium with `playwright install chromium`" if "executable" in str(exc).lower() else "check Playwright installation"
        return None, f"headless_browser_failed:{type(exc).__name__}; {detail}", candidate.url
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
    return None, "dynamic_page_pdf_not_found", page_url


def _content_length_exceeds_limit(value: Any) -> bool:
    try:
        return int(value) > MAX_BYTES
    except (TypeError, ValueError):
        return False


def _save_pdf(output: Path, base: str, identity: str, payload: bytes) -> Path:
    digest = hashlib.sha256(identity.encode()).hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".paper-", suffix=".pdf", dir=output)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        for index in range(1000):
            suffix = "" if index == 0 else f"-{digest}" if index == 1 else f"-{digest}-{index - 1}"
            target = output / f"{base}{suffix}.pdf"
            try:
                # Hard-link is atomic and fails rather than replacing an existing file.
                os.link(temp_name, target)
                return target
            except FileExistsError:
                continue
        raise OSError("could not reserve a unique output filename after 1000 collisions")
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _safe_stem(identity: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", identity).strip("._-")[:100] or "paper"
    return clean


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def download(inputs: list[str], output: Path, email: str | None = None, *,
             http_only: bool = False, dry_run: bool = False,
             session: Any = requests, browser_fetch: Any = _fetch_headless,
             no_scihub: bool = False, scihub_url: str = DEFAULT_SCIHUB_URL) -> dict[str, Any]:
    scihub_url = _validate_scihub_url(scihub_url)
    output = output.expanduser().resolve()
    manifest_path = output / "manifest.json"
    old_manifest = {"items": []}
    if manifest_path.exists():
        try:
            old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (not isinstance(old_manifest, dict) or old_manifest.get("version") != 1
                    or not isinstance(old_manifest.get("items"), list)
                    or any(not isinstance(item, dict) or not isinstance(item.get("identity"), str)
                           for item in old_manifest["items"])):
                raise ManifestError("manifest must have version 1 and an items list with identity values")
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"cannot read existing manifest {manifest_path}: {exc}") from exc
    previous = {item.get("identity"): item for item in old_manifest.get("items", [])}
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for raw in inputs:
        identity, identity_kind = identity_for(raw)
        if identity in seen:
            continue
        seen.add(identity)
        existing = previous.get(identity)
        if existing and existing.get("status") == "downloaded" and Path(existing.get("path", "")).is_file():
            results.append({**existing, "status": "already_present"})
            continue
        if dry_run:
            status = "planned" if identity_kind == "doi" or _valid_http_url(raw) else "invalid_input"
            results.append({"input": raw, "identity": identity, "status": status})
            continue
        doi = normalize_doi(raw)
        notes: list[str] = []
        stages: list[Any] = []
        if doi:
            if email:
                stages.append(lambda: _unpaywall_stage(doi, email, session, notes))
            else:
                notes.append("unpaywall_lookup_skipped:no_email")
            if not no_scihub:
                stages.append(lambda: _scihub_stage(doi, scihub_url, session, notes, candidate_log))
            else:
                notes.append("scihub_lookup_skipped:disabled")
            stages.append(lambda: _crossref_stage(doi, session, notes))
        elif _valid_http_url(raw):
            stages = [lambda: [Candidate(raw, "direct_url", identity_basis=identity_kind)]]
        else:
            invalid = {"input": raw, "identity": identity, "status": "invalid_input",
                       "errors": ["input must be a DOI, doi: DOI, doi.org URL, or absolute HTTP(S) URL"]}
            results.append(invalid)
            previous[identity] = invalid
            _atomic_json(manifest_path, {"version": 1, "items": list(previous.values())})
            continue
        saved: dict[str, Any] | None = None
        errors: list[str] = []
        candidate_log: list[dict[str, str]] = []
        visited: set[str] = set()
        for get_candidates in stages:
            try:
                candidates = get_candidates()
            except Exception as exc:
                label = "unpaywall" if email and doi and get_candidates is stages[0] else "source"
                notes.append(f"{label}_lookup_failed:{type(exc).__name__}")
                candidates = []
            for candidate in candidates[:12]:
                if candidate.url in visited:
                    continue
                visited.add(candidate.url)
                log_index = len(candidate_log)
                if candidate.payload is not None:
                    candidate_log.append({"url": candidate.url, "actual_url": candidate.actual_url,
                                          "source": candidate.source, "version": candidate.version,
                                          "host_type": candidate.host_type, "status": "downloaded"})
                    payload, state, linked, actual_url = candidate.payload, "downloaded", [], candidate.actual_url
                else:
                    candidate_log.append({"url": candidate.url, "actual_url": "", "source": candidate.source,
                                          "version": candidate.version, "host_type": candidate.host_type,
                                          "status": "not_attempted"})
                    payload, state, linked, actual_url = _fetch_http(candidate, session)
                    candidate_log[log_index].update(actual_url=actual_url, status=state)
                if payload is None and state == "html_page":
                    dynamic_fallback: Candidate | None = candidate if not linked else None
                    blocked_link = False
                    for metadata_candidate in linked:
                        if metadata_candidate.url in visited:
                            continue
                        visited.add(metadata_candidate.url)
                        payload, state, _, actual_url = _fetch_http(metadata_candidate, session)
                        linked_log = {"url": metadata_candidate.url, "actual_url": actual_url,
                                      "source": candidate.source, "version": candidate.version,
                                      "host_type": candidate.host_type, "status": state}
                        candidate_log.append(linked_log)
                        if payload:
                            candidate = Candidate(actual_url, candidate.source, candidate.version,
                                                  candidate.host_type, candidate.identity_basis)
                            break
                        if state.endswith("_blocked"):
                            blocked_link = True
                        elif state == "html_page":
                            dynamic_fallback = Candidate(actual_url, candidate.source, candidate.version,
                                                         candidate.host_type, candidate.identity_basis)
                    if (payload is None and dynamic_fallback and not blocked_link and not http_only
                            and candidate.source != "scihub"):
                        candidate = dynamic_fallback
                        payload, state, actual_url = browser_fetch(candidate)
                        candidate_log.append({"url": candidate.url, "actual_url": actual_url,
                                              "source": candidate.source, "version": candidate.version,
                                              "host_type": candidate.host_type, "status": f"headless:{state}"})
                if payload and payload.startswith(b"%PDF-"):
                    base = _safe_stem(doi or identity)
                    target = _save_pdf(output, base, identity, payload)
                    saved = {"input": raw, "identity": identity,
                             "identity_basis": candidate.identity_basis or identity_kind,
                             "status": "downloaded", "path": str(target), "source": candidate.source,
                             "version": candidate.version, "host_type": candidate.host_type,
                             "url": candidate.url, "actual_url": actual_url, "candidates": candidate_log,
                             "bytes": len(payload), "notes": notes,
                             "note": "PDF header checked; full paper identity/content not verified"}
                    break
                errors.append(state)
            if saved:
                break
        result = saved or {"input": raw, "identity": identity, "status": "needs_attention",
                           "errors": errors, "notes": notes, "candidates": candidate_log}
        results.append(result)
        updated_items = dict(previous)
        updated_items[identity] = result
        previous = updated_items
        _atomic_json(manifest_path, {"version": 1, "items": list(previous.values())})
    return {"output": str(output), "manifest": str(manifest_path), "results": results,
            "failed": sum(item["status"] in {"needs_attention", "invalid_input"} for item in results)}


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc) and not parsed.username and not parsed.password


def _validate_scihub_url(value: str) -> str:
    parsed = urlparse(value)
    if (parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError("Sci-Hub base URL must be HTTP(S) without credentials, query, or fragment")
    return value.rstrip("/")


def _unpaywall_stage(doi: str, email: str, session: Any, notes: list[str]) -> list[Candidate]:
    try:
        candidates = unpaywall_candidates(doi, email, session)
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 404:
            notes.append("unpaywall_api_not_found")
        else:
            notes.append(f"unpaywall_lookup_failed:{type(exc).__name__}")
        return []
    if not candidates:
        notes.append("unpaywall_no_oa_location")
    return candidates


def _scihub_stage(doi: str, base: str, session: Any, notes: list[str],
                  candidate_log: list[dict[str, str]]) -> list[Candidate]:
    candidate, state = scihub_candidate(doi, base, session, candidate_log)
    if not candidate:
        notes.append(state)
        return []
    return [candidate]


def _crossref_stage(doi: str, session: Any, notes: list[str]) -> list[Candidate]:
    try:
        return crossref_candidates(doi, session)
    except Exception as exc:
        notes.append(f"crossref_lookup_failed:{type(exc).__name__}")
        return []


def _read_inputs(args: argparse.Namespace) -> list[str]:
    values = list(args.items)
    if args.input:
        values.extend(line.strip() for line in Path(args.input).read_text(encoding="utf-8").splitlines()
                      if line.strip() and not line.lstrip().startswith("#"))
    return values


def _extract_downloaded_markdown(result: dict[str, Any], output: Path, *,
                                 backend: str, timeout: int, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"output": str(output.expanduser().resolve()), "backend": backend,
                "status": "planned_after_download", "results": [], "failed": 0}
    paths = [Path(item["path"]) for item in result["results"]
             if item.get("status") in {"downloaded", "already_present"} and item.get("path")]
    module_name = "download_papers_extract_markdown"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            module_name, Path(__file__).with_name("extract_markdown.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    extractor = sys.modules[module_name]
    try:
        return extractor.extract_many(paths, output, backend=backend, timeout=timeout)
    except extractor.ManifestError as exc:
        # A Markdown checkpoint error must not discard successful PDF results.
        return {"output": str(output.expanduser().resolve()), "results": [],
                "failed": max(1, len(paths)), "error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("items", nargs="*", help="DOI, doi.org URL, article page, or PDF URL")
    parser.add_argument("--input", help="UTF-8 text file, one DOI or URL per line")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--email", default=os.environ.get("UNPAYWALL_EMAIL") or os.environ.get("PAPER_DOWNLOAD_EMAIL"), help="Contact email for Unpaywall")
    parser.add_argument("--no-scihub", action="store_true", help="Skip the Sci-Hub DOI lookup")
    parser.add_argument("--scihub-url", default=os.environ.get("SCIHUB_BASE_URL", DEFAULT_SCIHUB_URL), help="Single Sci-Hub base URL")
    parser.add_argument("--http-only", action="store_true", help="Do not try headless browser on dynamic pages")
    parser.add_argument("--to-markdown", action="store_true", help="Extract downloaded PDFs to full-text Markdown; no notes")
    parser.add_argument("--md-output", type=Path, help="Markdown artifacts directory (default: OUTPUT/markdown)")
    parser.add_argument("--markdown-backend", choices=("mineru", "local"), default="mineru")
    parser.add_argument("--markdown-timeout", type=int, default=900, help="MinerU polling timeout per PDF in seconds")
    parser.add_argument("--dry-run", action="store_true", help="List inputs without creating files or network access")
    args = parser.parse_args(argv)
    if args.markdown_timeout < 1:
        parser.error("--markdown-timeout must be positive")
    try:
        args.scihub_url = _validate_scihub_url(args.scihub_url)
    except ValueError as exc:
        parser.error(str(exc))
    inputs = _read_inputs(args)
    if not inputs:
        parser.error("provide one or more items or --input")
    try:
        result = download(inputs, args.output, args.email, http_only=args.http_only, dry_run=args.dry_run,
                          no_scihub=args.no_scihub, scihub_url=args.scihub_url)
    except ManifestError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.to_markdown:
        result["markdown"] = _extract_downloaded_markdown(
            result, args.md_output or args.output / "markdown", backend=args.markdown_backend,
            timeout=args.markdown_timeout, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["failed"] or result.get("markdown", {}).get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
