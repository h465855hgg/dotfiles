import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dot_local/lib"))

from dotbackup import BackupError, DotBackup


class TestDotBackup(DotBackup):
    def noctalia_running(self):
        return False

    def stop_noctalia(self):
        return False

    def start_noctalia(self):
        return None


class FailingRestoreDotBackup(TestDotBackup):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.restore_calls = 0

    def _restore_files(self, snapshot, manifest):
        self.restore_calls += 1
        if self.restore_calls == 1:
            settings = self.home / ".local/state/noctalia/settings.toml"
            settings.write_text('[shell]\nlang = "broken"\n')
            raise BackupError("injected restore failure")
        return super()._restore_files(snapshot, manifest)


class DotBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        self.runtime = self.root / "runtime"
        self.home.mkdir()
        self.repo.mkdir()
        noctalia = self.home / ".local/state/noctalia"
        noctalia.mkdir(parents=True)
        (noctalia / "instance.id").write_text("test\n")
        (noctalia / "settings.toml").write_text("[shell]\nlang = \"zh-Hans\"\n")
        (noctalia / "state.toml").write_text("[state]\nready = true\n")
        (noctalia / "recently_used.json").write_text("{}\n")
        self.app = TestDotBackup(self.home, self.repo, self.state, self.runtime)

    def tearDown(self):
        self.temp.cleanup()

    def test_resolves_active_state_directory(self):
        root, evidence = self.app.resolve_noctalia_root()
        self.assertEqual(root, (self.home / ".local/state/noctalia").resolve())
        self.assertIn("markers", evidence)

    def test_snapshot_manifest_and_checksum(self):
        entries, metadata = self.app.entries()
        snapshot = self.app._create_snapshot(self.repo / "snapshots", "test", entries, metadata)
        manifest = self.app.verify_snapshot(snapshot)
        settings = next(item for item in manifest["entries"] if item["key"] == "noctalia/settings.toml")
        self.assertTrue(settings["present"])
        self.assertEqual(len(settings["sha256"]), 64)

    def test_full_entries_include_nested_noctalia_state(self):
        nested = self.home / ".local/state/noctalia/clipboard/index.json"
        nested.parent.mkdir(parents=True)
        nested.write_text("{}\n")
        entries, metadata = self.app.full_entries()
        destinations = {entry.destination for entry in entries}
        self.assertIn(nested, destinations)
        self.assertEqual(metadata["profile"], "full-local")
        self.assertEqual(metadata["contains_sensitive_data"], "true")

    def test_archive_snapshot_round_trip(self):
        entries, metadata = self.app.entries()
        snapshot = self.app._create_snapshot(self.app.local_snapshot_dir, "test", entries, metadata)
        archive, digest = self.app.archive_snapshot(snapshot)
        self.assertTrue(archive.is_file())
        self.assertEqual(self.app.sha256(archive), digest)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

    def test_corrupt_snapshot_is_rejected(self):
        entries, metadata = self.app.entries()
        snapshot = self.app._create_snapshot(self.repo / "snapshots", "test", entries, metadata)
        (snapshot / "noctalia/settings.toml").write_text("broken")
        with self.assertRaises(BackupError):
            self.app.verify_snapshot(snapshot)

    def test_restore_creates_rollback_and_restores_file(self):
        entries, metadata = self.app.entries()
        snapshot = self.app._create_snapshot(self.repo / "snapshots", "test", entries, metadata)
        live = self.home / ".local/state/noctalia/settings.toml"
        live.write_text("[shell]\nlang = \"en\"\n")
        rollback = self.app.restore_snapshot(snapshot)
        self.assertIn('lang = "zh-Hans"', live.read_text())
        rollback_manifest = self.app.verify_snapshot(rollback)
        rollback_settings = rollback / "noctalia/settings.toml"
        self.assertIn('lang = "en"', rollback_settings.read_text())
        self.assertTrue(rollback_manifest["complete"])

    def test_manifest_cannot_target_arbitrary_file(self):
        entries, metadata = self.app.entries()
        snapshot = self.app._create_snapshot(self.repo / "snapshots", "test", entries, metadata)
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["entries"][0]["destination"] = "/tmp/not-allowed"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(BackupError):
            self.app.verify_snapshot(snapshot)

    def test_restore_failure_rolls_back_live_file(self):
        entries, metadata = self.app.entries()
        snapshot = self.app._create_snapshot(self.repo / "snapshots", "test", entries, metadata)
        live = self.home / ".local/state/noctalia/settings.toml"
        live.write_text('[shell]\nlang = "en"\n')
        failing = FailingRestoreDotBackup(self.home, self.repo, self.state, self.runtime)
        with self.assertRaisesRegex(BackupError, "was rolled back"):
            failing.restore_snapshot(snapshot)
        self.assertIn('lang = "en"', live.read_text())

    def test_rollback_removes_file_that_was_originally_absent(self):
        optional = self.home / ".local/state/noctalia/usage_counts.json"
        self.assertFalse(optional.exists())
        entries, metadata = self.app.entries()
        rollback = self.app._create_snapshot(self.app.rollback_dir, "rollback-test", entries, metadata)
        optional.write_text("{}\n")
        self.app._restore_files(rollback, self.app.verify_snapshot(rollback))
        self.assertFalse(optional.exists())


if __name__ == "__main__":
    unittest.main()
