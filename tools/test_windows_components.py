"""Offline Windows component installation outcomes and authenticated repair receipts."""
import hashlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from loop import evidence, workbench as W
from loop.session import Session
from tools.test_conversations import ConversationFixture

installer = Path(__file__).resolve().parents[1] / "desktop/installer/dokan-component.py"
spec = importlib.util.spec_from_file_location("dokan_component", installer)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class DriverInstallerTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.msi = self.root / "Dokan_x64.msi"
        self.msi.write_bytes(b"fixture")
        pin = {"dokan": {"sha256": hashlib.sha256(b"fixture").hexdigest()}}
        (self.root / "windows-components.json").write_text(json.dumps(pin), encoding="utf-8")
        self.enterContext(patch.object(driver, "__file__", str(self.root / installer.name)))
        self.enterContext(patch.object(driver, "locked_read", side_effect=lambda path: path.open("rb")))
        self.enterContext(patch.object(driver, "system_installer", return_value="msiexec.exe"))
        self.enterContext(patch.object(driver.subprocess, "CREATE_NO_WINDOW", 0, create=True))
        self.execute = self.enterContext(patch.object(driver.subprocess, "run"))
        self.probe = self.enterContext(patch.object(driver, "status", return_value=driver.Status.MISSING))

    def test_ready_and_newer_driver_are_left_alone(self):
        for state in (driver.Status.READY, driver.Status.NEWER):
            self.probe.return_value = state
            self.assertEqual(driver.install(), state)
        self.execute.assert_not_called()

    def test_invalid_package_does_not_start_windows_installer(self):
        self.msi.write_bytes(b"changed")
        self.assertEqual(driver.install(), driver.Status.INVALID)
        self.execute.assert_not_called()

    def test_installer_outcome_and_post_install_driver_probe(self):
        for code, expected in [(1602, driver.Status.CANCELLED), (1223, driver.Status.CANCELLED),
                               (1618, driver.Status.BUSY), (3010, driver.Status.RESTART),
                               (1641, driver.Status.RESTART), (1603, driver.Status.FAILED)]:
            with self.subTest(code=code):
                self.execute.return_value = Mock(returncode=code)
                self.assertEqual(driver.install(), expected)
                self.assertEqual(self.execute.call_args.args[0], ["msiexec.exe", "/i", str(self.msi), "/norestart"])
        self.execute.return_value = Mock(returncode=0)
        for after, expected in [(driver.Status.READY, driver.Status.READY),
                                (driver.Status.MISSING, driver.Status.FAILED)]:
            self.probe.side_effect = [driver.Status.MISSING, after]
            self.assertEqual(driver.install(), expected)


class DriverProbeTests(unittest.TestCase):
    def test_driver_readiness_uses_the_loaded_kernel_protocol(self):
        for version, kernel, state in [(231, 0x190, driver.Status.READY), (231, 0, driver.Status.MISSING),
                                       (231, 399, driver.Status.MISSING), (232, 0, driver.Status.NEWER),
                                       (232, 401, driver.Status.READY)]:
            with self.subTest(version=version, kernel=kernel):
                lib = Mock()
                lib.DokanVersion.return_value = version
                lib.DokanDriverVersion.return_value = kernel
                with patch.object(driver.ctypes, "WinDLL", return_value=lib, create=True) as load:
                    self.assertEqual(driver.status(), state)
                load.assert_called_once_with("dokan2.dll", winmode=0x800)
                lib.DokanShutdown.assert_called_once()


class DriverReceiptTests(ConversationFixture, unittest.TestCase):
    def page(self, tool, result, *, denied=False):
        _, thread = self.call("POST")
        directory = self.root / thread["id"]
        aid = "a" * 32
        meta = evidence.capture(Session(directory), aid, result)
        row = dict(seq=1, run_id="run", act="action", action_id=aid, tool=tool,
                   role="executor", state="failed", args={}, outputEvidence=meta)
        (directory / "events.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        # Isolate the typed receipt from project-grant fixtures. The real API calls
        # this gate before loading retained output or adding any repair action.
        error = W.Problem("resource_denied", "revoked", 403) if denied else None
        with patch.object(evidence, "check_output_access", side_effect=error):
            return evidence.output(thread["id"], aid, 0, self.sv, readable=True)

    def test_project_driver_failure_has_repair_action(self):
        for tool in ("run_project_shell", "run_project_check", "read_check_output"):
            with self.subTest(tool=tool):
                page = self.page(tool, {"ok": False, "code": "project_driver_missing"})
                self.assertEqual(page.get("requiredComponent"), "dokan2")

    def test_ordinary_terminal_text_and_other_tools_cannot_offer_install(self):
        cases = [("run_project_shell", {"ok": True, "stdout": "project_driver_missing"}),
                 ("run_project_check", {"ok": False, "code": "ordinary", "error": "project_driver_missing"}),
                 ("search_project_code", {"ok": False, "code": "project_driver_missing"})]
        for tool, result in cases:
            with self.subTest(tool=tool, result=result):
                self.assertNotIn("requiredComponent", self.page(tool, result))

    def test_restricted_output_cannot_offer_install(self):
        with self.assertRaises(W.Problem) as refused:
            self.page("run_project_shell", {"ok": False, "code": "project_driver_missing"}, denied=True)
        self.assertEqual(refused.exception.status, 403)
