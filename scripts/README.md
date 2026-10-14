# SARS SFTP exchange script

`sars_sftp_sync.py` connects to the SARS test environment over SFTP and:

1. Prompts for a username and password.
2. Downloads the `UIF_1_*.psv` files from `SARSOUTBOUND` (warns if there are not exactly two)
   and prints a content summary (size, encoding, line/column counts, preview) for you to confirm.
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
# Interactive run (prompts for host, username and password)
python3 scripts/sars_sftp_sync.py

# Typical run against the test environment
python3 scripts/sars_sftp_sync.py --host <sars-test-host> --port 22

# Preview the clean-up and upload without changing the server
python3 scripts/sars_sftp_sync.py --host <sars-test-host> --dry-run --upload UIF_2_20261014.psv

# Unattended run (no confirmation prompts)
python3 scripts/sars_sftp_sync.py --host <sars-test-host> --username <user> --yes
```

Host, port and username may also be given via `SARS_SFTP_HOST`, `SARS_SFTP_PORT` and
`SARS_SFTP_USER`. The password is always prompted for and never read from arguments.
Downloads go to `downloads/SARSOUTBOUND/` by default (`--download-dir` to change).
Use `--strict-host-key` once the server's key is in `~/.ssh/known_hosts`.
