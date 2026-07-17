# DotBackup Incident and Redesign

## Incident summary

The old backup manager copied Noctalia files from
`~/.config/ai.opencode.desktop/noctalia`, while the running Noctalia instance
used `~/.local/state/noctalia`. The repository therefore contained a stale
218-line configuration instead of the active 475-line configuration.

Restore was also unsafe:

- it overwrote live files sequentially;
- it did not create a pre-restore rollback point;
- it had no manifest, checksums, completeness marker, or post-copy validation;
- it could restart Noctalia after a partial failure;
- backup used unrestricted `chezmoi re-add` and `git add -A`;
- old snapshots were pruned before commit and push were durable;
- the GUI worker was a daemon thread and could die with the window.

These failures made the backup appear successful while omitting the actual
Noctalia state, and made recovery capable of leaving mixed configuration files.

## New safety model

- Detect the active Noctalia state directory from runtime markers. Ambiguous
  layouts abort unless `DOTBACKUP_NOCTALIA_ROOT` is explicitly set.
- Stop Noctalia while copying mutable state.
- Build snapshots in a hidden `.partial` directory.
- Parse TOML and JSON files and record SHA-256, size, mode, source, and restore
  destination in `manifest.json`.
- Atomically rename a verified snapshot into place.
- Stage and commit only the newly-created snapshot path. Never run `git add -A`.
- Before every restore, create and verify a local rollback snapshot.
- Restore each file through a temporary file plus atomic `os.replace`.
- Validate destination paths against a fixed allowlist.
- Verify restored hashes and perform a Noctalia startup health check.
- On any failure, automatically restore the rollback snapshot.
- Serialize all operations with an advisory lock.
- Keep two explicit profiles: a public configuration snapshot that may be
  pushed to GitHub, and a complete local snapshot that recursively includes
  both possible Noctalia state roots, Noctalia config/data/local plugins, and
  the full Niri, Ghostty, Fcitx5, OpenCode, VS Code, autostart, environment,
  Firefox config, and WirePlumber state directories.
- Complete local snapshots may contain tokens, clipboard data, notification
  history, account state, and other private material. They are stored with the
  local DotBackup state and are never staged by Git.

## Commands

```sh
dotbackup status
dotbackup backup
dotbackup backup --local-only
dotbackup remote-backup
dotbackup remote-list
dotbackup remote-restore backup-YYYY-MM-DD_HHMMSS_microseconds
dotbackup list
dotbackup verify /path/to/snapshot
dotbackup restore /path/to/snapshot
```

Local rollback snapshots are stored under
`~/.local/state/dotbackup/rollbacks/` and are intentionally not committed.
Snapshots created with `--local-only` are stored under
`~/.local/state/dotbackup/snapshots/`, outside the Git repository.
`remote-backup` uploads the complete sensitive snapshot as a GitHub Release
asset in the private `h465855hgg/dotfiles-backups` repository. The local archive
and snapshot are deleted only after the remote asset checksum is verified.
