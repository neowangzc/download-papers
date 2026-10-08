#!/usr/bin/env python3
"""Download institution-access PDFs with a headless-first, visible-login handoff."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

DEFAULT_ENGINE_DIR = Path(__file__).resolve().parents[1] / "vendor" / "instsci"
PROFILE_DIR = Path.home() / ".local/share/download-papers/institution-profile"
BROWSER_CACHE_DIR = Path.home() / ".cache/download-papers/cloakbrowser"
AUTH_REASONS = {"sso_required", "sso_redirect_stalled", "challenge_or_viewer_timeout"}
DOI_RE = re.compile(r"^10\.\d{4,9}/[^\s<>\"']+$", re.IGNORECASE)


def _load_engine(engine_dir: Path) -> dict[str, Any]:
    if not (engine_dir.expanduser() / "instsci" / "publisher_batch.py").is_file():
        raise FileNotFoundError("InstSci engine source is missing; restore vendor/instsci or use --engine-dir")
    engine_path = str(engine_dir.expanduser().resolve())
    if engine_path not in sys.path:
        sys.path.insert(0, engine_path)
    from instsci.config import Config
    from instsci.publisher_batch import PaperRecord, PublisherBatchDownloader
    from instsci.publisher_profiles import get_publisher_profile

    return {"Config": Config, "PaperRecord": PaperRecord,
            "PublisherBatchDownloader": PublisherBatchDownloader,
            "get_publisher_profile": get_publisher_profile}


def _records_from_file(path: Path, api: dict[str, Any]) -> list[Any]:
    if not path.is_file():
        raise ValueError("DOI input file does not exist")
    dois: list[str] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = line.strip()
        if not value or line.lstrip().startswith("#"):
            continue
        if value.lower().startswith("doi:"):
            value = value[4:].strip()
        else:
            parsed = urlparse(value)
            if parsed.scheme.lower() in {"http", "https"} and parsed.netloc.lower() in {
                "doi.org", "www.doi.org", "dx.doi.org", "www.dx.doi.org"
            }:
                value = unquote(parsed.path.lstrip("/"))
        value = unquote(value)
        if not DOI_RE.fullmatch(value):
            raise ValueError(f"invalid DOI on input line {line_number}")
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            dois.append(value)
    if not dois:
        raise ValueError("DOI input file is empty")
    return [api["PaperRecord"](doi=doi) for doi in dict.fromkeys(dois)]


def _existing_entries(manifest_path: Path) -> dict[str, dict[str, Any]]:
    if not manifest_path.exists():
        return {}
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("existing manifest is unreadable; preserving it and stopping") from exc
    if not isinstance(entries, list):
        raise ValueError("existing manifest has an unsupported structure; preserving it and stopping")
    found: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"existing manifest has an invalid item at position {index + 1}")
        doi = str(entry.get("doi", ""))
        if not DOI_RE.fullmatch(doi):
            raise ValueError(f"existing manifest has an invalid DOI at item {index + 1}")
        found[doi.casefold()] = entry
    return found


def _existing_successes(entries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for doi, entry in entries.items():
        if entry.get("status") != "success":
            continue
        pdf_value = entry.get("pdf_path")
        if not isinstance(pdf_value, str) or not pdf_value:
            continue
        pdf = Path(pdf_value)
        if pdf.is_file() and pdf.stat().st_size:
            found[doi] = entry
    return found


def enable_dedicated_profile_password_manager(profile_dir: Path | None = None) -> Path:
    """Enable local password save/fill prefs for this skill's profile only."""
    profile_path = Path(profile_dir or PROFILE_DIR).expanduser().resolve()
    if profile_path != PROFILE_DIR.expanduser().resolve():
        raise ValueError("password preferences may only be changed for the dedicated profile")
    singleton_lock = profile_path / "SingletonLock"
    if singleton_lock.exists() or singleton_lock.is_symlink():
        raise RuntimeError("dedicated browser profile is in use")
    preferences_path = profile_path / "Default" / "Preferences"
    preferences_path.parent.mkdir(parents=True, exist_ok=True)
    if preferences_path.exists():
        try:
            preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("dedicated browser Preferences is invalid; preserving it") from exc
        if not isinstance(preferences, dict):
            raise ValueError("dedicated browser Preferences has an invalid structure; preserving it")
    else:
        preferences = {}
    profile_prefs = preferences.setdefault("profile", {})
    if not isinstance(profile_prefs, dict):
        raise ValueError("dedicated browser profile preferences have an invalid structure; preserving it")
    preferences["credentials_enable_service"] = True
    profile_prefs["password_manager_enabled"] = True
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=preferences_path.parent,
                                         prefix=".Preferences.", suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(json.dumps(preferences, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, preferences_path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
    return preferences_path


def _make_downloader(base: type, headless: bool, visible_needed: set[str]):
    class InstitutionalDownloader(base):
        def __init__(self, *args, password_save_wait: int = 0, **kwargs):
            self._password_save_wait = max(0, int(password_save_wait or 0))
            self._interactive_login_completed = False
            self._password_save_wait_done = False
            kwargs["post_login_hold_sec"] = self._password_save_wait if not headless else 0
            super().__init__(*args, **kwargs)

        def _launch_context(self, profile_dir=None, proxy=None):
            from instsci.cloakbrowser_compat import prepare_cloakbrowser_runtime
            os.environ.setdefault("INSTSCI_CLOAKBROWSER_CACHE_DIR", str(BROWSER_CACHE_DIR))
            prepare_cloakbrowser_runtime()
            import cloakbrowser.browser as browser_module
            from cloakbrowser import launch_persistent_context

            profile_path = Path(profile_dir) if profile_dir else Path(self.config.chrome_profile_dir)
            enable_dedicated_profile_password_manager(profile_path)
            profile_path.mkdir(parents=True, exist_ok=True)
            downloads_path = profile_path.parent / f"{profile_path.name}-downloads"
            downloads_path.mkdir(parents=True, exist_ok=True)
            kwargs: dict[str, Any] = {}
            if proxy:
                kwargs["proxy"] = proxy
            original_filters = browser_module.IGNORE_DEFAULT_ARGS
            browser_module.IGNORE_DEFAULT_ARGS = list(dict.fromkeys([
                *original_filters, "--password-store=basic", "--use-mock-keychain",
            ]))
            try:
                return launch_persistent_context(
                    user_data_dir=str(profile_path), headless=headless, humanize=True,
                    accept_downloads=True, downloads_path=str(downloads_path),
                    args=["--disable-features=CrossOriginOpenerPolicy", "--restore-last-session"],
                    **kwargs,
                )
            finally:
                browser_module.IGNORE_DEFAULT_ARGS = original_filters

        def _complete_login_from_current_page(self, page, result):
            if headless:
                visible_needed.add(result.doi.casefold())
                self._event(result, "visible_login_required", "Interactive institution login is required.")
                return False
            completed = super()._complete_login_from_current_page(page, result)
            if completed:
                self._interactive_login_completed = True
            return completed

        def _hold_after_login(self, page, result):
            if (not headless and self._interactive_login_completed
                    and not self._password_save_wait_done and self._password_save_wait):
                self._event(result, "password_save_prompt_wait", f"{self._password_save_wait}s")
                print(f"Sign-in completed. Waiting {self._password_save_wait}s for you to confirm the browser's password-save prompt.",
                      file=sys.stderr)
                super()._hold_after_login(page, result)
                self._password_save_wait_done = True

        def _wait_for_challenge(self, page, result, *, deadline=None):
            if headless and self._is_challenge_page(page):
                visible_needed.add(result.doi.casefold())
                self._event(result, "visible_challenge_required", "Interactive verification is required.")
                return False
            return super()._wait_for_challenge(page, result, deadline=deadline)

    return InstitutionalDownloader


def _stage_results(run_dir: Path, expected_dois: set[str]) -> list[dict[str, Any]]:
    path = run_dir / "primary" / "summary.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{run_dir.name} stage did not produce a valid result summary") from exc
    results = data.get("results", []) if isinstance(data, dict) else []
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        raise RuntimeError(f"{run_dir.name} stage result summary has an invalid structure")
    observed: set[str] = set()
    for item in results:
        doi = str(item.get("doi", "")).casefold()
        if not doi or doi in observed or item.get("status") not in {"success", "failed", "partial"}:
            raise RuntimeError(f"{run_dir.name} stage returned invalid or duplicate DOI results")
        observed.add(doi)
    if observed != expected_dois:
        raise RuntimeError(f"{run_dir.name} stage result DOI set does not match its input")
    return results


def run_download(*, input_path: Path, publisher: str, institution: str,
                 output: Path, engine_dir: Path = DEFAULT_ENGINE_DIR,
                 login_timeout: int = 180, pdf_timeout: int = 45,
                 password_save_wait: int = 30,
                 visible: bool = False, dry_run: bool = False,
                 engine_api: dict[str, Any] | None = None) -> dict[str, Any]:
    if not institution.strip():
        raise ValueError("--institution is required; provide your subscription institution")
    api = engine_api or _load_engine(engine_dir)
    records = _records_from_file(input_path, api)
    output = output.expanduser().resolve()
    profile_dir = PROFILE_DIR.expanduser().resolve()
    requested_dois = {record.doi.casefold() for record in records}
    entries = _existing_entries(output / "complete" / "manifest.json")
    pending = {doi: item for doi, item in _existing_successes(entries).items()
               if doi in requested_dois}
    records = [r for r in records if r.doi.casefold() not in pending]
    if dry_run:
        return {"dry_run": True, "count": len(records), "profile_dir": str(profile_dir),
                "visible": visible}
    if not records:
        report = _write_manifest(output, entries, [], requested_dois)
        print(f"Institutional download finished: {report['success']} success, {report['missing']} pending.",
              file=sys.stderr)
        return report

    profile = api["get_publisher_profile"](publisher)
    output.mkdir(parents=True, exist_ok=True)
    visible_needed: set[str] = set()
    completed_phases: list[str] = []
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]

    def execute(items: list[Any], phase: str, headed: bool) -> list[dict[str, Any]]:
        stage_dir = output / "stages" / run_id / phase
        cfg = api["Config"](chrome_profile_dir=str(profile_dir),
                            carsi_idp_name=institution.strip(),
                            institution_name_en=institution.strip())
        cls = _make_downloader(api["PublisherBatchDownloader"], not headed, visible_needed)
        downloader = cls(cfg, profile=profile, institution_query=institution.strip(),
                         login_timeout_sec=login_timeout, pdf_timeout_sec=pdf_timeout,
                         password_save_wait=password_save_wait)
        print(f"Starting {phase} institutional download ({len(items)} DOI(s)).", file=sys.stderr)
        if headed and password_save_wait > 0:
            print("After your first successful sign-in, leave the visible browser open to confirm its password-save prompt.",
                  file=sys.stderr)
        downloader.run_records(items, stage_dir, retry_failed=False, concurrency=1)
        return _stage_results(stage_dir, {record.doi.casefold() for record in items})

    if visible:
        visible_results = execute(records, "visible", True)
        completed_phases.append("visible")
        report = _write_manifest(output, entries,
                                 [{"phase": "visible", "results": visible_results}], requested_dois)
    else:
        headless_results = execute(records, "headless", False)
        completed_phases.append("headless")
        # Commit usable headless results before any interactive window is opened.
        report = _write_manifest(output, entries,
                                 [{"phase": "headless", "results": headless_results}], requested_dois)
        checkpoint_path = output / "complete" / "manifest.json"
        checkpoint_entries = _existing_entries(checkpoint_path)
        by_doi = {item.get("doi", "").casefold(): item for item in headless_results}
        for item in headless_results:
            if item.get("reason") in AUTH_REASONS:
                visible_needed.add(str(item.get("doi", "")).casefold())
        retry = [record for record in records if record.doi.casefold() in visible_needed
                 and by_doi.get(record.doi.casefold(), {}).get("status") != "success"]
        if retry:
            print(f"Interactive institution login or verification is required for {len(retry)} DOI(s).",
                  file=sys.stderr)
            visible_results = execute(retry, "visible", True)
            completed_phases.append("visible")
            report = _write_manifest(output, checkpoint_entries,
                                     [{"phase": "visible", "results": visible_results}], requested_dois)
    report["stages"] = completed_phases

    print(f"Institutional download finished: {report['success']} success, {report['missing']} pending.",
          file=sys.stderr)
    report["run_id"] = run_id
    return report


def _write_manifest(output: Path, existing: dict[str, dict[str, Any]],
                    stages: list[dict[str, Any]], requested_dois: set[str]) -> dict[str, Any]:
    entries = dict(existing)
    complete = output / "complete"
    pdf_dir = complete / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    for doi in requested_dois:
        entries.setdefault(doi, {"doi": doi, "status": "missing", "reason": "not_attempted",
                                  "pdf_path": "", "size_bytes": 0, "text_length": 0,
                                  "verified_match": False, "pdf_url": ""})
    for stage in stages:
        for result in stage["results"]:
            doi = str(result.get("doi", ""))
            if not doi:
                continue
            pdf_path = str(result.get("pdf_path") or "")
            captured = result.get("status") == "success" and pdf_path and Path(pdf_path).is_file()
            verified = bool(captured and result.get("verified_match"))
            final_pdf = ""
            if captured:
                source = Path(pdf_path)
                suffix = hashlib.sha256(doi.casefold().encode("utf-8")).hexdigest()[:12]
                stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem)[:80] or "paper"
                base = f"{stem}-{suffix}"
                candidate = pdf_dir / f"{base}{source.suffix or '.pdf'}"
                serial = 1
                while True:
                    try:
                        with candidate.open("xb") as target:
                            target.write(source.read_bytes())
                        final_pdf = str(candidate)
                        break
                    except FileExistsError:
                        candidate = pdf_dir / f"{base}-{serial}{source.suffix or '.pdf'}"
                        serial += 1
            entries[doi.casefold()] = {
                "doi": doi,
                "status": "success" if verified else "unverified" if captured else "missing",
                "reason": "" if verified else str(result.get("reason") or result.get("state") or
                                                       ("doi_match_unverified" if captured else "download_failed")),
                "pdf_path": final_pdf if captured else "",
                "size_bytes": int(result.get("size_bytes") or 0) if captured else 0,
                "text_length": int(result.get("text_length") or 0) if captured else 0,
                "verified_match": verified,
                "pdf_url": str(result.get("pdf_url") or "") if captured else "",
            }
    ordered = sorted(entries.values(), key=lambda item: item["doi"].casefold())
    manifest_path = complete / "manifest.json"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=complete,
                                         prefix=".manifest.", suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(json.dumps(ordered, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, manifest_path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
    requested_entries = [entries[doi] for doi in requested_dois if doi in entries]
    count_success = sum(item["status"] == "success" for item in requested_entries)
    return {"count": len(requested_dois), "success": count_success,
            "missing": len(requested_dois) - count_success,
            "manifest": str(complete / "manifest.json"),
            "pdf_dir": str(complete / "pdfs"),
            "stages": [stage["phase"] for stage in stages]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="DOI file, one DOI per line")
    parser.add_argument("--publisher", default="oxfordacademic")
    parser.add_argument("--institution", required=True, help="Your subscription institution")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--engine-dir", type=Path, default=DEFAULT_ENGINE_DIR,
                        help="InstSci source directory (default: bundled vendor/instsci)")
    parser.add_argument("--login-timeout", type=int, default=180)
    parser.add_argument("--pdf-timeout", type=int, default=45)
    parser.add_argument("--password-save-wait", type=int, default=30,
                        help="Seconds to leave the visible browser open after first successful sign-in; 0 disables waiting")
    parser.add_argument("--visible", action="store_true", help="Run visibly from the start")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_download(input_path=args.input, publisher=args.publisher,
                              institution=args.institution, output=args.output,
                              engine_dir=args.engine_dir, login_timeout=args.login_timeout,
                              pdf_timeout=args.pdf_timeout, password_save_wait=args.password_save_wait,
                              visible=args.visible,
                              dry_run=args.dry_run)
    except ValueError:
        print("Input or existing manifest is invalid; saved files were preserved.", file=sys.stderr)
        return 1
    except RuntimeError:
        print("Publisher stage output is incomplete; the saved manifest was preserved.", file=sys.stderr)
        return 1
    except OSError:
        print("Input or output could not be read or written.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Institutional download failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    if report.get("dry_run"):
        print(f"Dry run: {report['count']} DOI(s); profile: {report['profile_dir']}")
        return 0
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("missing", 0) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
