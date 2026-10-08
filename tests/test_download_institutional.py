import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "download_institutional.py"
spec = importlib.util.spec_from_file_location("download_institutional", SCRIPT)
download_institutional = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = download_institutional
spec.loader.exec_module(download_institutional)


class FakeRecord:
    def __init__(self, doi):
        self.doi = doi


class FakeConfig:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeContext:
    def __init__(self, mode, events):
        self.mode = mode
        self.events = events

    def close(self):
        self.events.append(("close", self.mode))


class FakeDownloader:
    scenario = "login"
    events = []
    instances = []
    purge_calls = []

    def __init__(self, config, **kwargs):
        self.config = config
        self.options = kwargs
        self.instances.append(self)

    def _purge_session_restore(self, _path):
        self.purge_calls.append(_path)

    def _event(self, *_args):
        pass

    def _is_challenge_page(self, _page):
        return self.scenario == "challenge"

    def run_records(self, records, run_dir, **kwargs):
        context = self._launch_context()
        mode = context.mode
        if mode == "visible" and self.scenario == "mixed-interrupt":
            context.close()
            raise RuntimeError("simulated user interruption")
        results = []
        for record in records:
            auth = False
            needs_login = (self.scenario == "login" or
                           self.scenario == "mixed-interrupt" and record.doi.endswith("example2"))
            if mode == "headless" and needs_login:
                self._complete_login_from_current_page(types.SimpleNamespace(url=""), types.SimpleNamespace(doi=record.doi))
                auth = True
            elif mode == "headless" and self.scenario == "challenge":
                self._wait_for_challenge(types.SimpleNamespace(url=""), types.SimpleNamespace(doi=record.doi))
                auth = True
            if auth:
                results.append({"doi": record.doi, "status": "failed", "reason": "sso_required"})
            elif self.scenario == "network" and mode == "headless":
                results.append({"doi": record.doi, "status": "failed", "reason": "navigation_error"})
            else:
                pdf = Path(run_dir) / "primary" / "pdfs" / "fake.pdf"
                pdf.parent.mkdir(parents=True, exist_ok=True)
                pdf.write_bytes(b"%PDF-fake")
                results.append({"doi": record.doi, "status": "success", "pdf_path": str(pdf),
                                "size_bytes": pdf.stat().st_size, "verified_match": True,
                                "pdf_url": f"https://example.test/{record.doi}.pdf"})
        primary = Path(run_dir) / "primary"
        primary.mkdir(parents=True, exist_ok=True)
        if self.scenario == "omit":
            results = []
        (primary / "summary.json").write_text(json.dumps({"results": results}), encoding="utf-8")
        context.close()


class DownloadInstitutionalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile_patch = patch.object(download_institutional, "PROFILE_DIR", self.root / "browser-profile")
        self.profile_patch.start()
        self.doi_file = self.root / "dois.txt"
        self.doi_file.write_text("10.1234/example\n", encoding="utf-8")
        FakeDownloader.scenario = "login"
        FakeDownloader.events = []
        FakeDownloader.instances = []
        FakeDownloader.purge_calls = []
        self.launches = []
        self.launch_filters = []
        self.launch_args = []
        self.launcher_module = types.ModuleType("cloakbrowser")
        self.browser_module = types.ModuleType("cloakbrowser.browser")
        self.browser_module.IGNORE_DEFAULT_ARGS = ["--enable-automation", "--enable-unsafe-swiftshader"]
        self.launcher_module.browser = self.browser_module

        def launch_persistent_context(**kwargs):
            mode = "headless" if kwargs.get("headless") else "visible"
            self.launches.append(mode)
            self.launch_filters.append(list(self.browser_module.IGNORE_DEFAULT_ARGS))
            self.launch_args.append(list(kwargs.get("args", [])))
            FakeDownloader.events.append(("launch", mode))
            return FakeContext(mode, FakeDownloader.events)

        self.launcher_module.launch_persistent_context = launch_persistent_context
        compat = types.ModuleType("instsci.cloakbrowser_compat")
        compat.prepare_cloakbrowser_runtime = lambda: None
        self.modules_patch = patch.dict(sys.modules, {
            "cloakbrowser": self.launcher_module,
            "cloakbrowser.browser": self.browser_module,
            "instsci.cloakbrowser_compat": compat,
        })
        self.modules_patch.start()
        self.api = {"Config": FakeConfig, "PaperRecord": FakeRecord,
                    "PublisherBatchDownloader": FakeDownloader,
                    "get_publisher_profile": lambda _name: object()}

    def tearDown(self):
        self.modules_patch.stop()
        self.profile_patch.stop()
        self.temp.cleanup()

    def run_job(self, **overrides):
        params = {"input_path": self.doi_file, "publisher": "oxfordacademic",
                  "institution": "Example University", "output": self.root / "out",
                  "engine_api": self.api}
        params.update(overrides)
        return download_institutional.run_download(**params)

    def test_login_closes_headless_profile_before_visible_handoff(self):
        original_filters = list(self.browser_module.IGNORE_DEFAULT_ARGS)
        report = self.run_job(password_save_wait=37)
        self.assertEqual(self.launches, ["headless", "visible"])
        self.assertLess(FakeDownloader.events.index(("close", "headless")),
                        FakeDownloader.events.index(("launch", "visible")))
        self.assertEqual(report["stages"], ["headless", "visible"])
        self.assertEqual(FakeDownloader.purge_calls, [])
        self.assertEqual(len(self.launch_args), 2)
        self.assertTrue(all("--restore-last-session" in args for args in self.launch_args))
        for filters in self.launch_filters:
            self.assertIn("--password-store=basic", filters)
            self.assertIn("--use-mock-keychain", filters)
        self.assertEqual(self.browser_module.IGNORE_DEFAULT_ARGS, original_filters)
        self.assertEqual(FakeDownloader.instances[0].options["post_login_hold_sec"], 0)
        self.assertEqual(FakeDownloader.instances[1].options["post_login_hold_sec"], 37)

    def test_success_does_not_open_visible_window_and_is_skipped_next_run(self):
        FakeDownloader.scenario = "success"
        first = self.run_job()
        self.assertEqual(self.launches, ["headless"])
        second = self.run_job()
        self.assertEqual(self.launches, ["headless"])
        self.assertEqual(first["success"], second["success"])

    def test_network_failure_does_not_open_visible_window(self):
        FakeDownloader.scenario = "network"
        report = self.run_job()
        self.assertEqual(self.launches, ["headless"])
        self.assertEqual(report["missing"], 1)

    def test_challenge_hands_off_to_visible(self):
        FakeDownloader.scenario = "challenge"
        self.run_job()
        self.assertEqual(self.launches, ["headless", "visible"])

    def test_dry_run_never_launches_browser(self):
        report = self.run_job(dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(self.launches, [])

    def test_corrupt_existing_manifest_is_preserved_and_stops(self):
        manifest = self.root / "out" / "complete" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        original = b"{broken"
        manifest.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "preserving it"):
            self.run_job()
        self.assertEqual(manifest.read_bytes(), original)
        self.assertEqual(self.launches, [])
        second_manifest = self.root / "other-out" / "complete" / "manifest.json"
        second_manifest.parent.mkdir(parents=True)
        second_original = json.dumps([{"doi": "10.1234/example", "status": "missing"}]).encode()
        second_manifest.write_bytes(second_original)
        FakeDownloader.scenario = "omit"
        with self.assertRaisesRegex(RuntimeError, "DOI set"):
            self.run_job(output=self.root / "other-out")
        self.assertEqual(second_manifest.read_bytes(), second_original)

    def test_doi_inputs_normalize_deduplicate_and_reject_invalid_values(self):
        self.doi_file.write_text(" DOI:10.1234/EXAMPLE \nhttps://doi.org/10.1234/example\n", encoding="utf-8")
        report = self.run_job(dry_run=True)
        self.assertEqual(report["count"], 1)
        self.doi_file.write_text("not-a-doi\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid DOI"):
            self.run_job(dry_run=True)
        FakeDownloader.scenario = "success"
        self.doi_file.write_text("10.1234/example\n10.1234/example2\n", encoding="utf-8")
        self.run_job()
        entries = json.loads((self.root / "out" / "complete" / "manifest.json").read_text())
        self.assertEqual(len({entry["pdf_path"] for entry in entries}), 2)
        self.assertTrue(all(Path(entry["pdf_path"]).is_file() for entry in entries))

    def test_subset_preserves_history_and_headless_checkpoint_survives_visible_interruption(self):
        self.doi_file.write_text("10.1234/example\n10.1234/example2\n", encoding="utf-8")
        manifest = self.root / "out" / "complete" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        history = [
            {"doi": "10.1234/legacy", "status": "unverified", "reason": "old",
             "pdf_path": "", "verified_match": False},
            {"doi": "10.1234/older", "status": "missing", "reason": "old",
             "pdf_path": "", "verified_match": False},
        ]
        manifest.write_text(json.dumps(history), encoding="utf-8")
        FakeDownloader.scenario = "mixed-interrupt"
        with self.assertRaisesRegex(RuntimeError, "interruption"):
            self.run_job()
        checkpoint = json.loads(manifest.read_text(encoding="utf-8"))
        by_doi = {entry["doi"]: entry for entry in checkpoint}
        self.assertEqual(by_doi["10.1234/legacy"]["status"], "unverified")
        self.assertEqual(by_doi["10.1234/older"]["status"], "missing")
        headless_pdf = by_doi["10.1234/example"]["pdf_path"]
        self.assertTrue(Path(headless_pdf).is_file())
        self.assertEqual(self.launches, ["headless", "visible"])

        FakeDownloader.scenario = "success"
        report = self.run_job()
        self.assertEqual(self.launches, ["headless", "visible", "headless"])
        self.assertEqual(report["count"], 2)
        self.assertEqual(report["success"], 2)
        final = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(len(final), 4)
        final_by_doi = {entry["doi"]: entry for entry in final}
        self.assertEqual(final_by_doi["10.1234/example"]["pdf_path"], headless_pdf)
        self.assertEqual(final_by_doi["10.1234/legacy"]["status"], "unverified")
        self.assertEqual(final_by_doi["10.1234/older"]["status"], "missing")

    def test_password_manager_preferences_are_scoped_preserving_and_lock_aware(self):
        profile = self.root / "dedicated-profile"
        preferences = profile / "Default" / "Preferences"
        preferences.parent.mkdir(parents=True)
        original = {"profile": {"name": "Keep", "password_manager_enabled": False},
                    "sync": {"requested": True}, "other": 42}
        preferences.write_text(json.dumps(original), encoding="utf-8")
        with patch.object(download_institutional, "PROFILE_DIR", profile):
            self.assertEqual(download_institutional.enable_dedicated_profile_password_manager(),
                             preferences.resolve())
            updated = json.loads(preferences.read_text(encoding="utf-8"))
            self.assertTrue(updated["credentials_enable_service"])
            self.assertTrue(updated["profile"]["password_manager_enabled"])
            self.assertEqual(updated["profile"]["name"], "Keep")
            self.assertEqual(updated["sync"], {"requested": True})
            self.assertEqual(updated["other"], 42)

            corrupt = b"{invalid"
            preferences.write_bytes(corrupt)
            with self.assertRaisesRegex(ValueError, "preserving it"):
                download_institutional.enable_dedicated_profile_password_manager()
            self.assertEqual(preferences.read_bytes(), corrupt)

            preferences.write_text(json.dumps(original), encoding="utf-8")
            (profile / "SingletonLock").touch()
            before_lock = preferences.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "in use"):
                download_institutional.enable_dedicated_profile_password_manager()
            self.assertEqual(preferences.read_bytes(), before_lock)
            with self.assertRaisesRegex(ValueError, "dedicated profile"):
                download_institutional.enable_dedicated_profile_password_manager(self.root / "other")


if __name__ == "__main__":
    unittest.main()
