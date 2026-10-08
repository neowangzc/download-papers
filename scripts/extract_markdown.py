#!/usr/bin/env python3
"""Extract local PDFs to resumable Markdown artifacts.

This module is deliberately independent of Zotero and the Obsidian note pipeline.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from io import BytesIO
from typing import Any


DEFAULT_BASE_URL = "https://mineru.net/api/v4"
MAX_ZIP_BYTES = 500 * 1024 * 1024
MAX_ZIP_MEMBERS = 10_000


class ManifestError(ValueError):
    """An input or checkpoint manifest is malformed and must not be overwritten."""


@dataclass(frozen=True)
class ExtractionResult:
    markdown: str
    images: dict[str, bytes]


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _load_manifest_inputs(path: Path) -> list[Path]:
    path = path.expanduser().resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError("input manifest is unreadable or invalid JSON") from None
    if isinstance(data, dict):
        items = data.get("items")
        if not isinstance(items, list):
            # InstSci complete-list exports have also appeared as named lists.
            items = data.get("complete")
    elif isinstance(data, list):
        items = data
    else:
        items = None
    if not isinstance(items, list):
        raise ManifestError("input manifest must contain an items or complete list")
    paths: list[Path] = []
    for item in items:
        if not isinstance(item, dict):
            raise ManifestError("input manifest entries must be objects")
        status = str(item.get("status", "")).lower()
        value = item.get("path") or item.get("pdf_path")
        accepted = status in {"downloaded", "success"}
        if accepted:
            if not isinstance(value, str) or not value.strip():
                raise ManifestError("successful input manifest entry is missing its PDF path")
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            paths.append(candidate.resolve())
    return paths


def _read_extraction_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "backend": "mineru", "results": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ManifestError("existing extraction manifest is unreadable or invalid JSON") from None
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("results"), list)
            or any(not isinstance(item, dict) or not isinstance(item.get("identity"), str)
                   for item in data["results"])):
        raise ManifestError("existing extraction manifest has an invalid format")
    return data


def _pdf_identity(path: Path, backend: str, model: str) -> tuple[str, dict[str, Any]]:
    resolved = path.expanduser().resolve()
    info = resolved.stat()
    source = {
        "input_pdf": str(resolved),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "backend": backend,
        "model": model if backend == "mineru" else "",
    }
    digest = hashlib.sha256(json.dumps(source, sort_keys=True).encode("utf-8")).hexdigest()
    stable_id = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", resolved.stem).strip("._-")[:64] or "paper"
    return f"{stem}-{stable_id}-{digest[:8]}", {**source, "identity": digest}


def _validate_pdf(path: Path) -> None:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            header = stream.read(2048)
            stream.seek(max(0, size - 8192))
            tail = stream.read()
    except OSError as exc:
        raise ValueError("pdf_unreadable") from exc
    if size == 0 or b"%PDF-" not in header[:1024]:
        raise ValueError("invalid_pdf")
    declared = re.search(rb"/Linearized\s+[\d.]+.*?/L\s+(\d+)\b", header, re.S)
    if declared and int(declared[1]) > size:
        raise ValueError("truncated_pdf")
    if b"%%EOF" not in tail:
        raise ValueError("truncated_pdf")


def _local_extract(path: Path) -> ExtractionResult:
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("pymupdf_unavailable") from exc
    try:
        document = pymupdf.open(path)
        pages = []
        for index, page in enumerate(document, start=1):
            text = page.get_text("text").strip()
            if text:
                pages.extend((f"## Page {index}", "", text, ""))
        document.close()
    except Exception as exc:
        raise RuntimeError("pdf_parse_failed") from exc
    markdown = "\n".join(pages).strip()
    if not markdown:
        raise ValueError("no_extractable_text")
    return ExtractionResult(markdown + "\n", {})


class MinerUExtractor:
    def __init__(self, api_token: str, base_url: str = DEFAULT_BASE_URL,
                 model: str = "vlm", *, timeout: int = 900,
                 sleep=time.sleep, poll_interval: int = 3, progress=None):
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.sleep = sleep
        self.poll_interval = poll_interval
        self.progress = progress

    def _request_json(self, method: str, endpoint: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        for attempt in range(4):
            request = urllib.request.Request(
                f"{self.base_url}/{endpoint.lstrip('/')}", data=body,
                headers={"Authorization": f"Bearer {self.api_token}",
                         "Content-Type": "application/json"}, method=method)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    value = json.load(response)
                if not isinstance(value, dict) or value.get("code") != 0:
                    raise RuntimeError("mineru_api_error")
                data = value.get("data")
                if not isinstance(data, dict):
                    raise RuntimeError("mineru_response_invalid")
                return data
            except urllib.error.HTTPError as exc:
                if exc.code in {429, 500, 502, 503, 504} and attempt < 3:
                    delay = 5 * (2 ** attempt)
                    retry_after = (exc.headers or {}).get("Retry-After", "")
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            try:
                                retry_time = parsedate_to_datetime(retry_after)
                                if retry_time.tzinfo is None:
                                    retry_time = retry_time.replace(tzinfo=timezone.utc)
                                delay = max(delay, retry_time.timestamp() - time.time())
                            except (TypeError, ValueError, OverflowError):
                                pass
                    if exc.code == 429 and delay > 300:
                        raise RuntimeError("mineru_rate_limited") from None
                    self.sleep(max(0, delay))
                    continue
                raise RuntimeError("mineru_rate_limited" if exc.code == 429 else "mineru_http_error") from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise RuntimeError("mineru_request_failed") from None
        raise RuntimeError("mineru_retry_exhausted")

    @staticmethod
    def _upload(url: str, pdf_path: Path) -> None:
        # The signed upload URL is used in memory only and is never put in exceptions/logs.
        from urllib.parse import urlparse
        import http.client

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("mineru_upload_url_invalid")
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(parsed.netloc, timeout=300)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        try:
            connection.putrequest("PUT", target)
            connection.putheader("Content-Length", str(pdf_path.stat().st_size))
            connection.endheaders()
            with pdf_path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    connection.send(chunk)
            response = connection.getresponse()
            response.read(1000)
            if not 200 <= response.status < 300:
                raise RuntimeError("mineru_upload_failed")
        except OSError as exc:
            raise RuntimeError("mineru_upload_failed") from None
        finally:
            connection.close()

    @staticmethod
    def _download_zip(url: str) -> bytes:
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                blob = response.read(MAX_ZIP_BYTES + 1)
            if len(blob) > MAX_ZIP_BYTES:
                raise RuntimeError("mineru_zip_too_large")
            return blob
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError("mineru_result_download_failed") from None

    @staticmethod
    def _unpack(archive: bytes) -> ExtractionResult:
        try:
            with zipfile.ZipFile(BytesIO(archive)) as result_zip:
                members = result_zip.infolist()
                if len(members) > MAX_ZIP_MEMBERS or sum(m.file_size for m in members) > MAX_ZIP_BYTES:
                    raise RuntimeError("mineru_zip_too_large")
                files: dict[str, bytes] = {}
                markdown_names = []
                for member in members:
                    name = member.filename.replace("\\", "/")
                    relative = PurePosixPath(name)
                    mode = (member.external_attr >> 16) & 0xFFFF
                    if (relative.is_absolute() or ".." in relative.parts
                            or not relative.parts or stat.S_ISLNK(mode)):
                        raise RuntimeError("unsafe_zip_path")
                    if member.is_dir():
                        continue
                    if name.endswith("/full.md") or name == "full.md":
                        markdown_names.append(name)
                    files[name] = result_zip.read(member)
                if len(markdown_names) != 1:
                    raise RuntimeError("mineru_markdown_missing_or_ambiguous")
                markdown_name = markdown_names[0]
                markdown = files[markdown_name].decode("utf-8")
                prefix = str(PurePosixPath(markdown_name).parent)
                image_prefix = "images/" if prefix == "." else f"{prefix}/images/"
                images = {
                    name[len(image_prefix):]: content
                    for name, content in files.items()
                    if name.startswith(image_prefix) and name[len(image_prefix):]
                }
                if prefix != ".":
                    # MinerU normally emits full.md and images/ together. Normalize
                    # that common nested layout without altering relative image names.
                    markdown = re.sub(r"(?<![\w/])(?:\./)?images/", "images/", markdown)
                return ExtractionResult(markdown, images)
        except zipfile.BadZipFile as exc:
            raise RuntimeError("mineru_invalid_zip") from None
        except UnicodeDecodeError as exc:
            raise RuntimeError("mineru_markdown_not_utf8") from None

    def extract(self, pdf_path: Path, data_id: str,
                batch_id: str | None = None) -> tuple[ExtractionResult, str | None]:
        if batch_id is None:
            submission = self._request_json("POST", "file-urls/batch", {
                "files": [{"name": pdf_path.name, "data_id": data_id}],
                "model_version": self.model,
            })
            batch_id = submission.get("batch_id")
            urls = submission.get("file_urls")
            if not isinstance(batch_id, str) or not batch_id or not isinstance(urls, list) or len(urls) != 1:
                raise RuntimeError("mineru_submission_invalid")
            if self.progress:
                self.progress("uploading PDF to MinerU")
            self._upload(urls[0], pdf_path)
        started = time.monotonic()
        last_state = None
        last_reported = started - 30
        while time.monotonic() - started < self.timeout:
            data = self._request_json("GET", f"extract-results/batch/{batch_id}")
            entries = data.get("extract_result")
            if not isinstance(entries, list):
                raise RuntimeError("mineru_result_invalid")
            result = next((entry for entry in entries if entry.get("data_id") == data_id), None)
            if result is None:
                result = next((entry for entry in entries if entry.get("file_name") == pdf_path.name), None)
            state = result.get("state", "waiting") if result else "waiting"
            now = time.monotonic()
            if self.progress and (state != last_state or now - last_reported >= 30):
                self.progress(f"MinerU parsing status: {state}")
                last_state = state
                last_reported = now
            if result and result.get("state") == "failed":
                raise RuntimeError("mineru_parse_failed")
            if result and result.get("state") == "done":
                return self._unpack(self._download_zip(result.get("full_zip_url", ""))), None
            self.sleep(self.poll_interval)
        return ExtractionResult("", {}), batch_id


def _write_result(folder: Path, extracted: ExtractionResult) -> tuple[Path, list[str]]:
    folder.parent.mkdir(parents=True, exist_ok=True)
    base = folder
    suffix = 1
    while True:
        folder = base if suffix == 1 else base.with_name(f"{base.name}-{suffix}")
        try:
            folder.mkdir(exist_ok=False)
            break
        except FileExistsError:
            suffix += 1
    markdown_path = folder / "full.md"
    image_paths = []
    try:
        for name, content in extracted.images.items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise RuntimeError("unsafe_image_path")
            image_path = Path("images", *relative.parts)
            _atomic_bytes(folder / image_path, content)
            image_paths.append(image_path.as_posix())
        _atomic_bytes(markdown_path, extracted.markdown.encode("utf-8"))
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return markdown_path, image_paths


def extract_many(pdf_paths: list[Path], output: Path, *, backend: str = "mineru",
                 api_token: str | None = None, base_url: str = DEFAULT_BASE_URL,
                 model: str = "vlm", timeout: int = 900, dry_run: bool = False,
                 progress=None) -> dict[str, Any]:
    if backend not in {"mineru", "local"}:
        raise ValueError("backend must be mineru or local")
    output = output.expanduser().resolve()
    manifest_path = output / "markdown-manifest.json"
    manifest = _read_extraction_manifest(manifest_path) if not dry_run else {
        "version": 1, "backend": backend, "results": []}
    records = {item.get("identity"): item for item in manifest["results"]
               if isinstance(item, dict) and item.get("identity")}
    token = api_token
    if backend == "mineru" and not dry_run and not token:
        token = os.environ.get("MINERU_API_TOKEN", "").strip() or None
    extractor = MinerUExtractor(token, base_url, model, timeout=timeout) if backend == "mineru" and token else None
    results = []

    def report(message: str) -> None:
        if progress:
            progress(message)
        else:
            print(message, file=sys.stderr)

    for raw_path in pdf_paths:
        path = Path(raw_path).expanduser()
        absolute = str(path.resolve())
        if not path.is_file():
            missing_identity = hashlib.sha256(
                f"{absolute}\0{backend}\0{model}".encode("utf-8")
            ).hexdigest()
            result = {"input_pdf": absolute, "status": "needs_attention", "backend": backend,
                      "reason": "pdf_missing", "identity": missing_identity}
            results.append(result)
            report(f"missing PDF: {absolute}")
            if not dry_run:
                records[missing_identity] = result
                manifest = {"version": 1, "backend": backend,
                            "results": list(records.values())}
                _atomic_json(manifest_path, manifest)
            continue
        try:
            folder_name, source = _pdf_identity(path, backend, model)
        except OSError:
            result = {"input_pdf": absolute, "status": "needs_attention", "backend": backend,
                      "reason": "pdf_unreadable"}
            results.append(result)
            continue
        prior = records.get(source["identity"])
        pending_batch_id = prior.get("batch_id") if prior else None
        prior_markdown = Path(prior.get("markdown_path", "")) if prior else None
        recorded_images = prior.get("image_paths", []) if prior else []
        images_complete = (isinstance(recorded_images, list)
                           and all(isinstance(item, str)
                                   and not PurePosixPath(item).is_absolute()
                                   and ".." not in PurePosixPath(item).parts
                                   and (prior_markdown.parent / Path(item)).is_file()
                                   for item in recorded_images))
        if (prior and prior.get("status") == "extracted" and prior_markdown
                and prior_markdown.is_file() and images_complete):
            results.append({"input_pdf": absolute, "status": "already_present",
                            "markdown_path": str(prior_markdown), "backend": backend})
            continue
        if dry_run:
            results.append({"input_pdf": absolute, "status": "planned", "backend": backend})
            continue
        try:
            _validate_pdf(path)
            if backend == "mineru" and extractor is None:
                raise RuntimeError("missing_mineru_token")
            if backend == "local":
                report(f"extracting locally: {absolute}")
                extracted = _local_extract(path)
                pending_batch_id = None
            else:
                report(f"submitting to MinerU: {absolute}")
                extractor.progress = lambda message: report(f"{message}: {absolute}")
                pending_id = pending_batch_id if prior and prior.get("status") == "needs_attention" else None
                data_id = source["identity"][:32]
                extracted, pending_batch_id = extractor.extract(path, data_id, pending_id)
                if pending_batch_id:
                    raise TimeoutError("mineru_timeout")
            if not extracted.markdown.strip():
                raise ValueError("no_extractable_text")
            folder = output / folder_name
            markdown_path, image_paths = _write_result(folder, extracted)
            result = {"input_pdf": absolute, "status": "extracted",
                      "markdown_path": str(markdown_path), "image_paths": image_paths,
                      "backend": backend,
                      **source}
            report(f"extracted: {markdown_path}")
        except Exception as exc:
            reason = str(exc) if str(exc) in {
                "invalid_pdf", "truncated_pdf", "pdf_unreadable", "missing_mineru_token",
                "pymupdf_unavailable", "pdf_parse_failed", "no_extractable_text",
                "mineru_rate_limited", "mineru_http_error", "mineru_request_failed",
                "mineru_retry_exhausted", "mineru_upload_url_invalid", "mineru_upload_failed",
                "mineru_zip_too_large", "unsafe_zip_path", "mineru_markdown_missing_or_ambiguous",
                "mineru_invalid_zip", "mineru_markdown_not_utf8", "mineru_submission_invalid",
                "mineru_result_invalid", "mineru_parse_failed", "mineru_result_download_failed",
                "output_exists", "unsafe_image_path",
            } else "extraction_failed"
            result = {"input_pdf": absolute, "status": "needs_attention", "backend": backend,
                      "reason": reason, **source}
            if pending_batch_id:
                result["batch_id"] = pending_batch_id
                if isinstance(exc, TimeoutError):
                    result["reason"] = "mineru_timeout"
            report(f"needs attention ({result['reason']}): {absolute}")
        results.append(result)
        records[source["identity"]] = result
        manifest = {"version": 1, "backend": backend,
                    "results": list(records.values())}
        _atomic_json(manifest_path, manifest)
    if not dry_run:
        output.mkdir(parents=True, exist_ok=True)
        # Checkpoint even when every input failed before identity creation.
        manifest = {"version": 1, "backend": backend, "results": list(records.values())}
        _atomic_json(manifest_path, manifest)
    failed = sum(item.get("status") == "needs_attention" for item in results)
    return {"output": str(output), "manifest": str(manifest_path),
            "results": results, "failed": failed}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract local PDFs to Markdown.")
    parser.add_argument("pdfs", nargs="*", type=Path, help="local PDF paths")
    parser.add_argument("--input-manifest", type=Path,
                        help="download manifest (items/path) or InstSci list (pdf_path)")
    parser.add_argument("--output", required=True, type=Path, help="artifact output directory")
    parser.add_argument("--backend", choices=("mineru", "local"), default="mineru")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true", help="plan only; no token, network, or writes")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    paths = list(args.pdfs)
    if args.input_manifest:
        try:
            paths.extend(_load_manifest_inputs(args.input_manifest))
        except ManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if not paths:
        parser.error("provide PDF paths or --input-manifest")
    try:
        result = extract_many(paths, args.output, backend=args.backend,
                              timeout=args.timeout, dry_run=args.dry_run)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
