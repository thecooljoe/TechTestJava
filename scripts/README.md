# SARS SFTP exchange script

`sars_sftp_sync.py` connects to the SARS test environment over SFTP and:

1. Prompts for a username and password.
2. Downloads the `UIF_1_*.psv` files from `SARSOUTBOUND` (warns if there are not exactly two)
   and prints a content summary (size, line count, column-count distribution, preview, last line) for you
   to confirm. Files are streamed, so multi-gigabyte files are fine. A single short last line is
   reported as a trailer record; two adjacent short lines that together make one record are reported
   as a record split by a line break inside a field; other rows with the wrong column count are listed
   with line numbers. ID Number values that are not 13 digits are counted and listed.
3. Deletes files in `SARSOUTBOUND` older than 31 days (asks before deleting; `--dry-run` only reports).
4. Uploads any `--upload FILE ...` to the root directory, which is where files destined for SARS go.

## Setup

```bash
pip install -r scripts/requirements.txt
```

paramiko is pinned below version 4 on purpose. The SARS SFTP server offers only legacy
host-key algorithms (`ssh-rsa` with SHA-1 / `ssh-dss`) that paramiko 4 and later removed;
with a newer paramiko the connection fails with `Incompatible ssh peer (no acceptable host key)`.
If you already have a newer version installed, run `pip install 'paramiko>=3.4,<4'`.

## Usage

```bash
# Test environment (sftptest.sars.gov.za is the default host; prompts for username and password)
python3 scripts/sars_sftp_sync.py

# Production
python3 scripts/sars_sftp_sync.py --host sftp.sars.gov.za

# Preview the clean-up and upload without changing the server
python3 scripts/sars_sftp_sync.py --dry-run --upload UIF_2_20261014.psv

# Re-check files you already downloaded, without connecting
python3 scripts/sars_sftp_sync.py --inspect downloads/SARSOUTBOUND/*.psv

# Same, and also write <name>.repaired.psv with records that were split by an embedded line break joined
python3 scripts/sars_sftp_sync.py --inspect downloads/SARSOUTBOUND/*.psv --repair

# Re-run without downloading files that are already present locally
python3 scripts/sars_sftp_sync.py --skip-existing

# Unattended run (no confirmation prompts)
python3 scripts/sars_sftp_sync.py --username <user> --yes
```

Host, port and username may also be given via `SARS_SFTP_HOST`, `SARS_SFTP_PORT` and
`SARS_SFTP_USER`. The password is always prompted for and never read from arguments.
Downloads go to `downloads/SARSOUTBOUND/` by default (`--download-dir` to change).
Use `--strict-host-key` once the server's key is in `~/.ssh/known_hosts`.

## Notes on the SARS server

The SARS endpoint is an IBM DataPower appliance with limited SFTP support. The script:

- tries relative, absolute and `./`-prefixed paths when opening a file, because DataPower
  rejects some path forms (the OpenSSH `sftp` client's `get` after `cd` fails for this reason);
- falls back to keyboard-interactive login if plain password login is rejected;
- never deletes a file whose modification time the server does not report;
- uploads straight to the final file name (no temporary name and rename), because DataPower
  may process a file as soon as it is closed and may not support rename.

If a login fails, the script prints the authentication methods the server accepts. Run with
`--verbose` to see the full SSH negotiation.
