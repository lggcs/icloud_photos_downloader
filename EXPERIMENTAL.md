# Experimental Mode

Goal is to try new things and get feedback from users without breaking existing behavior.

Anything in this section can change without backward compatibility or even completely removed.

DANGER ZONE: Code may not work as expected.

## Advanced Data Protection (`--adp-web-access`)

Opt-in support for accounts with Advanced Data Protection (ADP). After signing in, icloudpd replays the consent sequence icloud.com uses to unlock protected photos. See [ADP](docs/authentication.md#adp) for setup.

- Requires `Access iCloud Data on the Web` to be enabled on the account
- The first run sends an approval request to your Apple devices; icloudpd waits up to 2 minutes for it
- Tested by its author on a real ADP account; the test suite only covers simulated responses. Please report how it works for you in [#687](https://github.com/icloud-photos-downloader/icloud_photos_downloader/issues/687)
- It is not yet known how long Apple keeps the approval valid; when it expires, the next run asks for approval again

## Security keys (FIDO)

Sign in with a security key, see [FIDO](docs/authentication.md#fido). Only tested on Linux, see [#583](https://github.com/icloud-photos-downloader/icloud_photos_downloader/issues/583).

## Rsync-style backup (`--sync-manifest`)

Opt-in support for resumable, bandwidth-friendly backups. icloudpd remembers every downloaded file in a SQLite manifest (`.icloudpd-sync.db` in the download directory) and skips anything that is already in sync with iCloud, so repeated runs download only what is missing or changed.

- Requires a dedicated download directory
- Files that already exist on disk before the first run are adopted into the manifest without downloading
- `--convert-to-jpeg` converts HEIC/HEIF photos to JPEG after downloading and removes the original, so the backup contains no Mac-specific file types; EXIF data, file modification time and XMP sidecars are preserved
- `--verify` audits the local mirror against the manifest and the iCloud listing without downloading anything, reporting missing, changed or outdated files; `--verify-checksums` additionally compares SHA-256 checksums of the local files
- The manifest is bound to the account, library and configuration; icloudpd refuses to reuse a manifest created with different settings
- The interactive helper `icloudpd-tui` walks through this flow: it asks for the account and backup directory, offers backup and verify actions, and prints the exact icloudpd command it runs
