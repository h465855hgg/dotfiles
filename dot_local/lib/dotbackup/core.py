#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


Progress = Callable[[str], None]
MANIFEST_VERSION = 1


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    key: str
    source: Path
    destination: Path
    required: bool = False
    validator: str = "none"


def _default_progress(_: str) -> None:
    pass


class DotBackup:
    def __init__(
        self,
        home: Path | None = None,
        repo: Path | None = None,
        state_dir: Path | None = None,
        runtime_dir: Path | None = None,
    ) -> None:
        self.home = (home or Path.home()).resolve()
        self.repo = (repo or self.home / ".local/share/chezmoi").resolve()
        self.snapshot_dir = self.repo / "snapshots"
        self.state_dir = (state_dir or self.home / ".local/state/dotbackup").resolve()
        self.local_snapshot_dir = self.state_dir / "snapshots"
        self.rollback_dir = self.state_dir / "rollbacks"
        runtime = runtime_dir or Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/dotbackup-{os.getuid()}"))
        self.runtime_dir = runtime.resolve()
        self.lock_path = self.runtime_dir / "dotbackup.lock"
        self.remote_url = "https://github.com/h465855hgg/dotfiles.git"
        self.backup_repo = os.environ.get("DOTBACKUP_REMOTE_REPO", "h465855hgg/dotfiles-backups")

    def run(
        self,
        args: Iterable[str | Path],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(arg) for arg in args]
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise BackupError(f"{' '.join(command)}: {detail}")
        return result

    @contextlib.contextmanager
    def lock(self):
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("w", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackupError("another DotBackup operation is already running") from exc
            handle.write(f"pid={os.getpid()}\n")
            handle.flush()
            yield

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def snapshot_id(prefix: str = "") -> str:
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        return f"{prefix}{stamp}"

    def ensure_ready(self, *, require_git: bool) -> None:
        if shutil.disk_usage(self.home).free < 512 * 1024 * 1024:
            raise BackupError("less than 512 MiB free space remains")
        if require_git:
            if not (self.repo / ".git").is_dir():
                raise BackupError(f"dotfiles repository not found: {self.repo}")
            if shutil.which("git") is None:
                raise BackupError("git is not available")

    def resolve_noctalia_root(self) -> tuple[Path, str]:
        override = os.environ.get("DOTBACKUP_NOCTALIA_ROOT")
        if override:
            root = Path(override).expanduser().resolve()
            if not root.is_dir():
                raise BackupError(f"DOTBACKUP_NOCTALIA_ROOT does not exist: {root}")
            return root, "environment override"

        if self.home == Path.home().resolve():
            state_home = Path(os.environ.get("XDG_STATE_HOME", self.home / ".local/state"))
        else:
            state_home = self.home / ".local/state"
        state_root = state_home / "noctalia"
        desktop_root = self.home / ".config/ai.opencode.desktop/noctalia"
        state_markers = ("instance.id", "state.toml", "plugins", "settings.toml")
        desktop_markers = ("instance.id", "state.toml", "plugins", "settings.toml")
        state_score = sum((state_root / name).exists() for name in state_markers)
        desktop_score = sum((desktop_root / name).exists() for name in desktop_markers)

        if state_score >= 2:
            return state_root.resolve(), f"state directory markers={state_score}"
        if desktop_score >= 2 and state_score == 0:
            return desktop_root.resolve(), f"desktop directory markers={desktop_score}"
        if state_score == 1 and desktop_score == 0:
            return state_root.resolve(), "only state directory exists"
        if desktop_score == 1 and state_score == 0:
            return desktop_root.resolve(), "only desktop directory exists"
        raise BackupError(
            "cannot determine the active Noctalia state directory; set DOTBACKUP_NOCTALIA_ROOT explicitly"
        )

    def entries(self) -> tuple[list[Entry], dict[str, str]]:
        noctalia, evidence = self.resolve_noctalia_root()
        entries = [
            Entry("niri/config.kdl", self.home / ".config/niri/config.kdl", self.home / ".config/niri/config.kdl"),
            Entry("niri/default-base.kdl", self.home / ".config/niri/default-base.kdl", self.home / ".config/niri/default-base.kdl"),
            Entry("ghostty/config", self.home / ".config/ghostty/config", self.home / ".config/ghostty/config"),
            Entry("ghostty/config.ghostty", self.home / ".config/ghostty/config.ghostty", self.home / ".config/ghostty/config.ghostty"),
            Entry("noctalia/settings.toml", noctalia / "settings.toml", noctalia / "settings.toml", True, "toml"),
            Entry("noctalia/state.toml", noctalia / "state.toml", noctalia / "state.toml", False, "toml"),
            Entry("noctalia/recently_used.json", noctalia / "recently_used.json", noctalia / "recently_used.json", False, "json"),
            Entry("noctalia/screen_time.json", noctalia / "screen_time.json", noctalia / "screen_time.json", False, "json"),
            Entry("noctalia/usage_counts.json", noctalia / "usage_counts.json", noctalia / "usage_counts.json", False, "json"),
            Entry("shell/bashrc", self.home / ".bashrc", self.home / ".bashrc"),
            Entry("shell/bash_profile", self.home / ".bash_profile", self.home / ".bash_profile"),
            Entry("shell/bash_logout", self.home / ".bash_logout", self.home / ".bash_logout"),
            Entry("shell/gitconfig", self.home / ".gitconfig", self.home / ".gitconfig"),
            Entry("vscode/settings.json", self.home / ".config/Code/User/settings.json", self.home / ".config/Code/User/settings.json", False, "json"),
            Entry("vscode/chatLanguageModels.json", self.home / ".config/Code/User/chatLanguageModels.json", self.home / ".config/Code/User/chatLanguageModels.json", False, "json"),
            Entry("vscode/argv.json", self.home / ".vscode/argv.json", self.home / ".vscode/argv.json"),
            Entry("opencode/opencode.jsonc", self.home / ".config/opencode/opencode.jsonc", self.home / ".config/opencode/opencode.jsonc"),
            Entry("desktop/kdeglobals", self.home / ".config/kdeglobals", self.home / ".config/kdeglobals"),
            Entry("desktop/user-dirs.dirs", self.home / ".config/user-dirs.dirs", self.home / ".config/user-dirs.dirs"),
            Entry("desktop/user-dirs.locale", self.home / ".config/user-dirs.locale", self.home / ".config/user-dirs.locale"),
            Entry("desktop/QtProject.conf", self.home / ".config/QtProject.conf", self.home / ".config/QtProject.conf"),
            Entry("desktop/arkrc", self.home / ".config/arkrc", self.home / ".config/arkrc"),
            Entry("desktop/mimeapps.list", self.home / ".config/mimeapps.list", self.home / ".config/mimeapps.list"),
            Entry("fcitx5/profile", self.home / ".config/fcitx5/profile", self.home / ".config/fcitx5/profile"),
            Entry("autostart/Clash Verge.desktop", self.home / ".config/autostart/Clash Verge.desktop", self.home / ".config/autostart/Clash Verge.desktop"),
            Entry("autostart/fcitx5.desktop", self.home / ".config/autostart/fcitx5.desktop", self.home / ".config/autostart/fcitx5.desktop"),
            Entry("autostart/MotrixNext.desktop", self.home / ".config/autostart/MotrixNext.desktop", self.home / ".config/autostart/MotrixNext.desktop"),
            Entry("system/environment.d/fcitx5.conf", self.home / ".config/environment.d/fcitx5.conf", self.home / ".config/environment.d/fcitx5.conf"),
            Entry("system/environment.d/locale.conf", self.home / ".config/environment.d/locale.conf", self.home / ".config/environment.d/locale.conf"),
            Entry("system/wireplumber/default-routes", self.home / ".local/state/wireplumber/default-routes", self.home / ".local/state/wireplumber/default-routes"),
            Entry("system/wireplumber/stream-properties", self.home / ".local/state/wireplumber/stream-properties", self.home / ".local/state/wireplumber/stream-properties"),
            Entry("firefox/profiles.ini", self.home / ".config/mozilla/firefox/profiles.ini", self.home / ".config/mozilla/firefox/profiles.ini"),
            Entry("firefox/installs.ini", self.home / ".config/mozilla/firefox/installs.ini", self.home / ".config/mozilla/firefox/installs.ini"),
            Entry("scripts/noctalia-watchdog", self.home / ".local/bin/noctalia-watchdog", self.home / ".local/bin/noctalia-watchdog"),
            Entry("scripts/dotbackup", self.home / ".local/bin/dotbackup", self.home / ".local/bin/dotbackup"),
            Entry("scripts/dotbackup-gui", self.home / ".local/bin/dotbackup-gui", self.home / ".local/bin/dotbackup-gui"),
            Entry("scripts/dotbackup-core.py", self.home / ".local/lib/dotbackup/core.py", self.home / ".local/lib/dotbackup/core.py"),
            Entry("scripts/dotbackup-init.py", self.home / ".local/lib/dotbackup/__init__.py", self.home / ".local/lib/dotbackup/__init__.py"),
        ]
        return entries, {"noctalia_root": str(noctalia), "noctalia_evidence": evidence}

    def _tree_entries(self, key: str, root: Path) -> list[Entry]:
        if not root.is_dir():
            return []
        entries = []
        for source in sorted(root.rglob("*")):
            if source.is_symlink() or not source.is_file():
                continue
            if source.name in ("opencode.db-wal", "opencode.db-shm"):
                continue
            relative = source.relative_to(root)
            validator = "toml" if source.suffix == ".toml" else "none"
            entries.append(Entry(f"{key}/{relative}", source, source, False, validator))
        return entries

    def full_backup_roots(self) -> list[tuple[str, Path]]:
        active_noctalia, evidence = self.resolve_noctalia_root()
        del evidence
        return [
            ("noctalia/active-state", active_noctalia),
            ("noctalia/default-state", self.home / ".local/state/noctalia"),
            ("noctalia/desktop-state", self.home / ".config/ai.opencode.desktop/noctalia"),
            ("noctalia/config", self.home / ".config/noctalia"),
            ("noctalia/data", self.home / ".local/share/noctalia"),
            ("niri", self.home / ".config/niri"),
            ("ghostty", self.home / ".config/ghostty"),
            ("fcitx5", self.home / ".config/fcitx5"),
            ("opencode", self.home / ".config/opencode"),
            ("opencode/state", self.home / ".local/share/opencode"),
            ("vscode/user", self.home / ".config/Code/User"),
            ("autostart", self.home / ".config/autostart"),
            ("environment.d", self.home / ".config/environment.d"),
            ("firefox/config", self.home / ".config/mozilla/firefox"),
            ("clash-verge", self.home / ".config/io.github.clash-verge-rev.clash-verge-rev"),
            ("gtk-3.0", self.home / ".config/gtk-3.0"),
            ("evolution", self.home / ".config/evolution"),
            ("gh", self.home / ".config/gh"),
            ("ssh", self.home / ".ssh"),
            ("keyrings", self.home / ".local/share/keyrings"),
            ("systemd/user", self.home / ".config/systemd/user"),
            ("wireplumber", self.home / ".local/state/wireplumber"),
            ("dotbackup", self.home / ".local/lib/dotbackup"),
            ("applications", self.home / ".local/share/applications"),
        ]

    def full_entries(self) -> tuple[list[Entry], dict[str, str]]:
        active_noctalia, evidence = self.resolve_noctalia_root()
        roots = self.full_backup_roots()
        entries = []
        seen_roots = set()
        tree_roots = []
        for key, root in roots:
            resolved = root.resolve()
            if resolved in seen_roots:
                continue
            seen_roots.add(resolved)
            tree_roots.append(str(resolved))
            entries.extend(self._tree_entries(key, resolved))
        for key, path in (
            ("shell/bashrc", self.home / ".bashrc"),
            ("shell/bash_profile", self.home / ".bash_profile"),
            ("shell/bash_logout", self.home / ".bash_logout"),
            ("shell/gitconfig", self.home / ".gitconfig"),
            ("vscode/argv.json", self.home / ".vscode/argv.json"),
            ("desktop/kdeglobals", self.home / ".config/kdeglobals"),
            ("desktop/user-dirs.dirs", self.home / ".config/user-dirs.dirs"),
            ("desktop/user-dirs.locale", self.home / ".config/user-dirs.locale"),
            ("desktop/QtProject.conf", self.home / ".config/QtProject.conf"),
            ("desktop/arkrc", self.home / ".config/arkrc"),
            ("desktop/mimeapps.list", self.home / ".config/mimeapps.list"),
            ("scripts/noctalia-watchdog", self.home / ".local/bin/noctalia-watchdog"),
            ("scripts/dotbackup", self.home / ".local/bin/dotbackup"),
            ("scripts/dotbackup-gui", self.home / ".local/bin/dotbackup-gui"),
        ):
            if path.is_file() and not path.is_symlink():
                entries.append(Entry(key, path, path))
        deduplicated = {}
        for entry in entries:
            deduplicated[entry.destination.resolve()] = entry
        return list(deduplicated.values()), {
            "profile": "full-local",
            "contains_sensitive_data": "true",
            "noctalia_root": str(active_noctalia),
            "noctalia_evidence": evidence,
            "tree_roots": json.dumps(tree_roots),
        }

    @staticmethod
    def validate_file(path: Path, validator: str) -> None:
        try:
            if validator == "toml":
                with path.open("rb") as handle:
                    tomllib.load(handle)
            elif validator == "json":
                with path.open(encoding="utf-8") as handle:
                    json.load(handle)
        except (OSError, ValueError) as exc:
            raise BackupError(f"invalid {validator} file {path}: {exc}") from exc

    def _matching_pids(self, args: list[str]) -> list[int]:
        result = self.run(["pgrep", *args], check=False)
        if result.returncode != 0:
            return []
        return [int(pid) for pid in result.stdout.split() if pid.isdigit()]

    def noctalia_running(self) -> bool:
        return bool(self._matching_pids(["-x", "noctalia"]))

    def stop_noctalia(self) -> bool:
        watchdog = self.home / ".local/bin/noctalia-watchdog"
        patterns = [f"^bash {watchdog}$", f"^{watchdog}$"]
        watchdog_pids: set[int] = set()
        for pattern in patterns:
            watchdog_pids.update(self._matching_pids(["-f", pattern]))
        noctalia_pids = set(self._matching_pids(["-x", "noctalia"]))
        was_running = bool(watchdog_pids or noctalia_pids)
        for pid in watchdog_pids | noctalia_pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            remaining = set(self._matching_pids(["-x", "noctalia"]))
            for pattern in patterns:
                remaining.update(self._matching_pids(["-f", pattern]))
            if not remaining:
                return was_running
            time.sleep(0.1)
        raise BackupError("Noctalia or its watchdog did not stop within 5 seconds")

    def start_noctalia(self) -> None:
        watchdog = self.home / ".local/bin/noctalia-watchdog"
        if not watchdog.is_file():
            raise BackupError(f"Noctalia watchdog not found: {watchdog}")
        subprocess.Popen(
            [str(watchdog)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.noctalia_running():
                time.sleep(1)
                if self.noctalia_running():
                    return
            time.sleep(0.2)
        raise BackupError("Noctalia failed its startup health check")

    def _copy_to_snapshot(self, entry: Entry, root: Path) -> dict:
        source = entry.source
        record = {
            "key": entry.key,
            "source": str(source),
            "destination": str(entry.destination),
            "required": entry.required,
            "validator": entry.validator,
            "present": source.is_file(),
        }
        if not source.is_file():
            if entry.required:
                raise BackupError(f"required file is missing: {source}")
            return record
        if source.is_symlink():
            raise BackupError(f"refusing to snapshot symlink: {source}")
        destination = root / entry.key
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.name == "opencode.db" and source.parent == self.home / ".local/share/opencode":
            src_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            dst_db = sqlite3.connect(destination)
            try:
                src_db.backup(dst_db)
            finally:
                dst_db.close()
                src_db.close()
            shutil.copystat(source, destination)
        else:
            for attempt in range(3):
                before = source.stat()
                shutil.copy2(source, destination)
                after = source.stat()
                if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
                    break
                if attempt == 2:
                    raise BackupError(f"file changed repeatedly while being backed up: {source}")
        self.validate_file(destination, entry.validator)
        record.update(
            sha256=self.sha256(destination),
            size=destination.stat().st_size,
            mode=destination.stat().st_mode & 0o777,
        )
        return record

    def _create_snapshot(
        self,
        destination_root: Path,
        snapshot_name: str,
        entries: list[Entry],
        metadata: dict[str, str],
    ) -> Path:
        destination_root.mkdir(parents=True, exist_ok=True)
        private = destination_root in (self.local_snapshot_dir, self.rollback_dir)
        if private:
            os.chmod(destination_root, 0o700)
        final = destination_root / snapshot_name
        partial = destination_root / f".{snapshot_name}.partial"
        if final.exists() or partial.exists():
            raise BackupError(f"snapshot already exists: {snapshot_name}")
        partial.mkdir(mode=0o700 if private else 0o755)
        try:
            records = [self._copy_to_snapshot(entry, partial) for entry in entries]
            if private:
                for path in partial.rglob("*"):
                    if path.is_dir():
                        os.chmod(path, 0o700)
                    elif path.is_file():
                        os.chmod(path, 0o600)
            manifest = {
                "version": MANIFEST_VERSION,
                "id": snapshot_name,
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "complete": True,
                "metadata": metadata,
                "entries": records,
            }
            manifest_path = partial / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with manifest_path.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(partial, final)
            if private:
                os.chmod(final, 0o700)
            return final
        except Exception:
            shutil.rmtree(partial, ignore_errors=True)
            raise

    def _git_prepare(self) -> None:
        branch = self.run(["git", "branch", "--show-current"], cwd=self.repo).stdout.strip()
        if branch != "main":
            raise BackupError(f"dotfiles repository must be on main, currently: {branch or 'detached HEAD'}")
        if self.run(["git", "diff", "--cached", "--quiet"], cwd=self.repo, check=False).returncode != 0:
            raise BackupError("dotfiles repository has pre-existing staged changes")
        self.run(["git", "remote", "set-url", "origin", self.remote_url], cwd=self.repo)
        self.run(["git", "fetch", "origin", "main"], cwd=self.repo)
        behind = int(self.run(["git", "rev-list", "--count", "HEAD..origin/main"], cwd=self.repo).stdout.strip())
        if behind:
            raise BackupError(f"dotfiles main is {behind} commit(s) behind origin/main; pull before backing up")

    def publish_snapshot(self, snapshot: Path) -> None:
        relative = snapshot.relative_to(self.repo)
        self.run(["git", "add", "--", relative], cwd=self.repo)
        self.run(["git", "commit", "-m", f"Snapshot {snapshot.name}", "--", relative], cwd=self.repo)
        self.run(["git", "push", "origin", "main"], cwd=self.repo)

    def ensure_private_backup_repo(self) -> None:
        if shutil.which("gh") is None:
            raise BackupError("GitHub CLI is not available")
        view = self.run(
            ["gh", "repo", "view", self.backup_repo, "--json", "isPrivate"],
            check=False,
        )
        if view.returncode != 0:
            self.run(
                [
                    "gh",
                    "repo",
                    "create",
                    self.backup_repo,
                    "--private",
                    "--description",
                    "Private full-state backups created by DotBackup",
                    "--disable-issues",
                    "--disable-wiki",
                    "--add-readme",
                ]
            )
            return
        try:
            details = json.loads(view.stdout)
        except json.JSONDecodeError as exc:
            raise BackupError(f"cannot inspect backup repository: {exc}") from exc
        if details.get("isPrivate") is not True:
            raise BackupError(f"refusing to upload sensitive data to non-private repository: {self.backup_repo}")

    def archive_snapshot(self, snapshot: Path) -> tuple[Path, str]:
        archive_dir = self.state_dir / "upload"
        archive_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(archive_dir, 0o700)
        archive = archive_dir / f"{snapshot.name}.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(snapshot, arcname=snapshot.name, recursive=True)
        os.chmod(archive, 0o600)
        return archive, self.sha256(archive)

    def upload_full_backup(self, progress: Progress = _default_progress) -> str:
        snapshot = self.create_backup(progress, publish=False)
        archive = None
        try:
            progress("Creating compressed archive...")
            archive, digest = self.archive_snapshot(snapshot)
            self.ensure_private_backup_repo()
            tag = f"backup-{snapshot.name}"
            progress(f"Uploading full backup to private repository {self.backup_repo}...")
            self.run(
                [
                    "gh",
                    "release",
                    "create",
                    tag,
                    str(archive),
                    "--repo",
                    self.backup_repo,
                    "--title",
                    f"DotBackup {snapshot.name}",
                    "--notes",
                    f"Full private backup. SHA-256: `{digest}`",
                    "--latest=false",
                ],
                timeout=1800,
            )
            release = self.run(
                ["gh", "api", f"repos/{self.backup_repo}/releases/tags/{tag}"],
                timeout=300,
            )
            data = json.loads(release.stdout)
            asset = next((item for item in data.get("assets", []) if item.get("name") == archive.name), None)
            if asset is None:
                raise BackupError("uploaded release asset is missing")
            remote_digest = asset.get("digest")
            if remote_digest and remote_digest != f"sha256:{digest}":
                raise BackupError(f"remote digest mismatch: {remote_digest} != sha256:{digest}")
            if not remote_digest:
                notes = data.get("body", "")
                if digest not in notes:
                    raise BackupError("GitHub did not return an asset digest and release notes lack the checksum")
            shutil.rmtree(snapshot)
            archive.unlink()
            with contextlib.suppress(OSError):
                archive.parent.rmdir()
            return f"uploaded and verified {self.backup_repo}@{tag}; local archive removed"
        except Exception:
            progress(f"Upload failed; local recovery snapshot retained at {snapshot}")
            if archive is not None and archive.exists():
                progress(f"Local archive retained at {archive}")
            raise

    def list_remote_backups(self) -> list[dict]:
        self.ensure_private_backup_repo()
        result = self.run(
            ["gh", "release", "list", "--repo", self.backup_repo, "--limit", "100", "--json", "tagName,name,createdAt"],
        )
        return json.loads(result.stdout)

    def download_remote_backup(self, tag: str, progress: Progress = _default_progress) -> Path:
        self.ensure_private_backup_repo()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        download_dir = Path(tempfile.mkdtemp(prefix="dotbackup-download-", dir=self.state_dir))
        os.chmod(download_dir, 0o700)
        try:
            progress(f"Downloading {tag} from private repository...")
            self.run(
                ["gh", "release", "download", tag, "--repo", self.backup_repo, "--dir", download_dir],
                timeout=1800,
            )
            archives = list(download_dir.glob("*.tar.gz"))
            if len(archives) != 1:
                raise BackupError(f"expected one backup archive, found {len(archives)}")
            archive = archives[0]
            release = json.loads(
                self.run(["gh", "api", f"repos/{self.backup_repo}/releases/tags/{tag}"]).stdout
            )
            asset = next((item for item in release.get("assets", []) if item.get("name") == archive.name), None)
            if asset is None:
                raise BackupError("release asset metadata is missing")
            digest = self.sha256(archive)
            remote_digest = asset.get("digest")
            if remote_digest and remote_digest != f"sha256:{digest}":
                raise BackupError("downloaded archive checksum does not match GitHub asset digest")
            extract_root = download_dir / "extract"
            extract_root.mkdir(mode=0o700)
            with tarfile.open(archive, "r:gz") as handle:
                members = handle.getmembers()
                for member in members:
                    path = Path(member.name)
                    if path.is_absolute() or ".." in path.parts:
                        raise BackupError(f"unsafe archive path: {member.name}")
                    if member.issym() or member.islnk():
                        raise BackupError("backup archive contains links")
                handle.extractall(extract_root, filter="data")
            directories = [path for path in extract_root.iterdir() if path.is_dir()]
            if len(directories) != 1:
                raise BackupError(f"expected one snapshot directory, found {len(directories)}")
            extracted = directories[0]
            self.local_snapshot_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.local_snapshot_dir, 0o700)
            destination = self.local_snapshot_dir / extracted.name
            if destination.exists():
                destination = self.local_snapshot_dir / f"{extracted.name}-imported-{int(time.time())}"
            os.replace(extracted, destination)
            try:
                self.verify_snapshot(destination)
            except Exception:
                shutil.rmtree(destination, ignore_errors=True)
                raise
            return destination
        finally:
            shutil.rmtree(download_dir, ignore_errors=True)

    def create_backup(self, progress: Progress = _default_progress, *, publish: bool = True) -> Path:
        with self.lock():
            self.ensure_ready(require_git=publish)
            if publish:
                progress("Checking dotfiles repository...")
                self._git_prepare()
            if publish:
                entries, metadata = self.entries()
                metadata["profile"] = "public-config"
                metadata["contains_sensitive_data"] = "false"
            else:
                entries, metadata = self.full_entries()
            progress(f"Using Noctalia state: {metadata['noctalia_root']}")
            was_running = self.stop_noctalia()
            try:
                name = self.snapshot_id()
                progress("Creating and verifying snapshot...")
                target = self.snapshot_dir if publish else self.local_snapshot_dir
                snapshot = self._create_snapshot(target, name, entries, metadata)
            finally:
                if was_running:
                    progress("Restarting Noctalia...")
                    self.start_noctalia()
            if publish:
                progress("Publishing only the new snapshot...")
                self.publish_snapshot(snapshot)
            return snapshot

    def list_snapshots(self, *, include_rollbacks: bool = False) -> list[Path]:
        roots = [self.snapshot_dir]
        if include_rollbacks:
            roots.extend((self.local_snapshot_dir, self.rollback_dir))
        result: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.iterdir():
                if path.is_dir() and not path.name.startswith("."):
                    result.append(path)
        return sorted(result, key=lambda path: path.name, reverse=True)

    def load_manifest(self, snapshot: Path) -> dict:
        manifest_path = snapshot / "manifest.json"
        if not manifest_path.is_file():
            return self._legacy_manifest(snapshot)
        with manifest_path.open(encoding="utf-8") as handle:
            manifest = json.load(handle)
        if manifest.get("version") != MANIFEST_VERSION or manifest.get("complete") is not True:
            raise BackupError(f"unsupported or incomplete snapshot: {snapshot.name}")
        return manifest

    @staticmethod
    def _safe_key(key: str) -> bool:
        path = Path(key)
        return bool(key) and not path.is_absolute() and ".." not in path.parts

    def _allowed_destinations(self) -> tuple[set[Path], set[Path]]:
        public_entries, _ = self.entries()
        full_entries, metadata = self.full_entries()
        exact = {entry.destination.resolve() for entry in public_entries + full_entries}
        roots = {Path(root).resolve() for root in json.loads(metadata["tree_roots"])}
        return exact, roots

    def _legacy_manifest(self, snapshot: Path) -> dict:
        entries, metadata = self.entries()
        records = []
        for entry in entries:
            source = snapshot / entry.key
            if not source.is_file():
                continue
            self.validate_file(source, entry.validator)
            records.append(
                {
                    "key": entry.key,
                    "source": str(entry.source),
                    "destination": str(entry.destination),
                    "required": entry.required,
                    "validator": entry.validator,
                    "present": True,
                    "sha256": self.sha256(source),
                    "size": source.stat().st_size,
                    "mode": source.stat().st_mode & 0o777,
                }
            )
        if not records:
            raise BackupError(f"legacy snapshot contains no recognized files: {snapshot}")
        return {
            "version": 0,
            "id": snapshot.name,
            "created_at": None,
            "complete": True,
            "metadata": {**metadata, "legacy": "true"},
            "entries": records,
        }

    def verify_snapshot(self, snapshot: Path) -> dict:
        manifest = self.load_manifest(snapshot)
        allowed, allowed_roots = self._allowed_destinations()
        for record in manifest["entries"]:
            key = record.get("key", "")
            if not self._safe_key(key):
                raise BackupError(f"unsafe snapshot key: {key!r}")
            destination = Path(record.get("destination", "")).resolve()
            if destination not in allowed and not any(root in destination.parents for root in allowed_roots):
                raise BackupError(f"snapshot destination is not allowlisted: {destination}")
            if not record.get("present"):
                if record.get("required"):
                    raise BackupError(f"required entry absent: {record['key']}")
                continue
            source = snapshot / record["key"]
            if not source.is_file():
                raise BackupError(f"snapshot entry missing: {record['key']}")
            self.validate_file(source, record.get("validator", "none"))
            if self.sha256(source) != record["sha256"]:
                raise BackupError(f"snapshot checksum mismatch: {record['key']}")
        return manifest

    @staticmethod
    def _atomic_replace(source: Path, destination: Path, mode: int | None) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.dotbackup-", dir=destination.parent)
        temp = Path(temp_name)
        try:
            with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            if mode is not None:
                os.chmod(temp, mode)
            os.replace(temp, destination)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()

    def _restore_files(self, snapshot: Path, manifest: dict) -> None:
        for record in manifest["entries"]:
            if not record.get("present"):
                destination = Path(record["destination"])
                if destination.is_symlink():
                    raise BackupError(f"refusing to remove symlink destination: {destination}")
                with contextlib.suppress(FileNotFoundError):
                    destination.unlink()
                continue
            source = snapshot / record["key"]
            destination = Path(record["destination"])
            if destination.is_symlink():
                raise BackupError(f"refusing to replace symlink destination: {destination}")
            self._atomic_replace(source, destination, record.get("mode"))
            self.validate_file(destination, record.get("validator", "none"))
            if self.sha256(destination) != record["sha256"]:
                raise BackupError(f"restored checksum mismatch: {destination}")

    def restore_snapshot(self, snapshot: Path, progress: Progress = _default_progress) -> Path:
        with self.lock():
            self.ensure_ready(require_git=False)
            snapshot = snapshot.resolve()
            roots = (self.snapshot_dir.resolve(), self.local_snapshot_dir.resolve(), self.rollback_dir.resolve())
            if not any(snapshot == root or root in snapshot.parents for root in roots):
                raise BackupError(f"snapshot is outside managed storage: {snapshot}")
            progress("Verifying requested snapshot...")
            manifest = self.verify_snapshot(snapshot)
            live_entries = []
            for record in manifest["entries"]:
                destination = Path(record["destination"])
                live_entries.append(
                    Entry(
                        record["key"],
                        destination,
                        destination,
                        False,
                        record.get("validator", "none"),
                    )
                )
            rollback_name = self.snapshot_id("rollback-")
            progress("Creating mandatory pre-restore rollback snapshot...")
            rollback = self._create_snapshot(
                self.rollback_dir,
                rollback_name,
                live_entries,
                {"reason": f"before restoring {snapshot.name}"},
            )
            self.verify_snapshot(rollback)
            was_running = self.stop_noctalia()
            try:
                progress("Restoring files atomically...")
                self._restore_files(snapshot, manifest)
                if was_running:
                    progress("Starting Noctalia and checking health...")
                    self.start_noctalia()
            except Exception as restore_error:
                progress("Restore failed; rolling back automatically...")
                rollback_manifest = self.verify_snapshot(rollback)
                self._restore_files(rollback, rollback_manifest)
                if was_running and not self.noctalia_running():
                    self.start_noctalia()
                raise BackupError(f"restore failed and was rolled back: {restore_error}") from restore_error
            return rollback

    def status(self) -> dict:
        noctalia, evidence = self.resolve_noctalia_root()
        return {
            "repo": str(self.repo),
            "snapshot_dir": str(self.snapshot_dir),
            "local_snapshot_dir": str(self.local_snapshot_dir),
            "rollback_dir": str(self.rollback_dir),
            "noctalia_root": str(noctalia),
            "noctalia_evidence": evidence,
            "noctalia_running": self.noctalia_running(),
            "full_backup_roots": [str(root.resolve()) for _key, root in self.full_backup_roots()],
            "snapshots": len(self.list_snapshots()),
            "local_snapshots": len(list(self.local_snapshot_dir.iterdir())) if self.local_snapshot_dir.is_dir() else 0,
            "rollbacks": len(list(self.rollback_dir.iterdir())) if self.rollback_dir.is_dir() else 0,
        }
