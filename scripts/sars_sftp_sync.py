#!/usr/bin/env python3
"""
SARS SFTP exchange helper.

What it does, in order:
  1. Prompts for a username and password and connects to the SARS test
     environment over SFTP.
  2. Looks in the ``SARSOUTBOUND`` sub-directory of the login root for ``.psv``
     files whose name starts with ``UIF_1_``, downloads them and prints a
     content summary (size, line count, column count per line, a preview) so
     the operator can confirm they are satisfied with the content.
  3. Maintains ``SARSOUTBOUND`` by deleting remote files that are older than
     the retention period (default: one month / 31 days). Deletion requires an
     explicit confirmation unless ``--yes`` is given; ``--dry-run`` only reports.
  4. Uploads any files given with ``--upload`` to the root directory, which is
     where files destined for SARS must be placed.

Examples:
  python3 sars_sftp_sync.py --host sftp-test.example.co.za
  python3 sars_sftp_sync.py --host sftp-test.example.co.za --port 2222 --dry-run
  python3 sars_sftp_sync.py --host sftp-test.example.co.za --upload UIF_2_20261014.psv
"""

from __future__ import annotations

import argparse
import getpass
import os
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

try:
    import paramiko
except ImportError:  # pragma: no cover - guidance for the operator
    sys.exit("paramiko is required: pip install paramiko")


OUTBOUND_DIR = "SARSOUTBOUND"
INBOUND_PREFIX = "UIF_1_"
INBOUND_SUFFIX = ".psv"
EXPECTED_INBOUND_FILES = 2
DEFAULT_RETENTION_DAYS = 31
PREVIEW_LINES = 5


@dataclass
class RemoteFile:
    name: str
    size: int
    mtime: datetime

    @property
    def age(self) -> timedelta:
        return datetime.now() - self.mtime


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #
def prompt_credentials(username: str | None) -> tuple[str, str]:
    if not username:
        username = input("Username: ").strip()
    if not username:
        sys.exit("A username is required.")
    password = getpass.getpass(f"Password for {username}: ")
    if not password:
        sys.exit("A password is required.")
    return username, password


def connect(host: str, port: int, username: str, password: str,
            strict_host_key: bool, timeout: float) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    known_hosts = Path.home() / ".ssh" / "known_hosts"
    if known_hosts.exists():
        try:
            client.load_host_keys(str(known_hosts))
        except OSError:
            pass
    if strict_host_key:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.WarningPolicy())

    print(f"Connecting to {host}:{port} as {username} ...")
    client.connect(
        hostname=host,
        port=port,
        username=username,
        password=password,
        look_for_keys=False,
        allow_agent=False,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
    )
    sftp = client.open_sftp()
    sftp.get_channel().settimeout(timeout)
    root = sftp.normalize(".")
    print(f"Connected. Root directory: {root}")
    return client, sftp


# --------------------------------------------------------------------------- #
# Remote listing helpers
# --------------------------------------------------------------------------- #
def list_remote_files(sftp: paramiko.SFTPClient, directory: str) -> list[RemoteFile]:
    files: list[RemoteFile] = []
    for attr in sftp.listdir_attr(directory):
        if attr.st_mode is not None and stat.S_ISDIR(attr.st_mode):
            continue
        files.append(RemoteFile(
            name=attr.filename,
            size=attr.st_size or 0,
            mtime=datetime.fromtimestamp(attr.st_mtime or 0),
        ))
    return sorted(files, key=lambda f: f.name)


def remote_join(directory: str, name: str) -> str:
    return f"{directory.rstrip('/')}/{name}"


def ensure_remote_dir(sftp: paramiko.SFTPClient, directory: str) -> None:
    try:
        st = sftp.stat(directory)
    except FileNotFoundError:
        sys.exit(f"Remote directory '{directory}' was not found in the root directory.")
    if st.st_mode is None or not stat.S_ISDIR(st.st_mode):
        sys.exit(f"Remote path '{directory}' exists but is not a directory.")


# --------------------------------------------------------------------------- #
# Step 2: download and verify UIF_1_*.psv
# --------------------------------------------------------------------------- #
def download_inbound(sftp: paramiko.SFTPClient, local_dir: Path) -> list[Path]:
    local_dir.mkdir(parents=True, exist_ok=True)
    all_files = list_remote_files(sftp, OUTBOUND_DIR)

    print(f"\n{OUTBOUND_DIR} contains {len(all_files)} file(s):")
    for f in all_files:
        print(f"  {f.mtime:%Y-%m-%d %H:%M}  {f.size:>10,} B  {f.name}")

    wanted = [f for f in all_files
              if f.name.startswith(INBOUND_PREFIX) and f.name.lower().endswith(INBOUND_SUFFIX)]
    if not wanted:
        print(f"\nNo {INBOUND_PREFIX}*{INBOUND_SUFFIX} files found; nothing to download.")
        return []
    if len(wanted) != EXPECTED_INBOUND_FILES:
        print(f"\nWARNING: expected {EXPECTED_INBOUND_FILES} {INBOUND_PREFIX}*{INBOUND_SUFFIX} "
              f"file(s) but found {len(wanted)}.")

    downloaded: list[Path] = []
    print(f"\nDownloading {len(wanted)} file(s) to {local_dir.resolve()} ...")
    for f in wanted:
        remote_path = remote_join(OUTBOUND_DIR, f.name)
        local_path = local_dir / f.name
        sftp.get(remote_path, str(local_path))
        local_size = local_path.stat().st_size
        status = "OK" if local_size == f.size else f"SIZE MISMATCH (remote {f.size:,} B)"
        print(f"  {f.name}: {local_size:,} B  {status}")
        if local_size != f.size:
            sys.exit(f"Download of {f.name} is incomplete; aborting.")
        downloaded.append(local_path)
    return downloaded


def summarise_psv(path: Path) -> bool:
    """Print a summary of a pipe-separated file. Returns True if it looks sane."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
        encoding = "latin-1 (not valid UTF-8)"

    lines = text.splitlines()
    non_empty = [ln for ln in lines if ln.strip()]
    col_counts = sorted({ln.count("|") + 1 for ln in non_empty})

    print(f"\n=== {path.name} ===")
    print(f"  size:        {len(raw):,} bytes")
    print(f"  encoding:    {encoding}")
    print(f"  lines:       {len(lines)} ({len(non_empty)} non-empty)")
    print(f"  columns:     {col_counts if col_counts else 'n/a'}"
          + ("  <- inconsistent column counts" if len(col_counts) > 1 else ""))
    print(f"  preview (first {PREVIEW_LINES} lines):")
    for ln in lines[:PREVIEW_LINES]:
        shown = ln if len(ln) <= 160 else ln[:157] + "..."
        print(f"    | {shown}")
    if len(lines) > PREVIEW_LINES:
        print(f"    | ... ({len(lines) - PREVIEW_LINES} more line(s))")

    problems = []
    if not raw:
        problems.append("file is empty")
    if non_empty and "|" not in text:
        problems.append("no pipe delimiters found")
    if len(col_counts) > 1:
        problems.append("rows do not all have the same number of columns")
    if problems:
        print("  checks:      " + "; ".join(problems))
        return False
    print("  checks:      passed")
    return True


def confirm_content(paths: list[Path], assume_yes: bool) -> None:
    if not paths:
        return
    all_ok = all(summarise_psv(p) for p in paths)
    if assume_yes:
        print("\nContent review skipped (--yes).")
        return
    prompt = "\nAre you satisfied with the content of the downloaded file(s)? [y/N] "
    if not all_ok:
        prompt = "\nOne or more files failed basic checks. Continue anyway? [y/N] "
    if input(prompt).strip().lower() not in ("y", "yes"):
        sys.exit("Stopped at operator request after content review.")


# --------------------------------------------------------------------------- #
# Step 3: retention on SARSOUTBOUND
# --------------------------------------------------------------------------- #
def apply_retention(sftp: paramiko.SFTPClient, retention_days: int,
                    dry_run: bool, assume_yes: bool) -> None:
    cutoff = datetime.now() - timedelta(days=retention_days)
    files = list_remote_files(sftp, OUTBOUND_DIR)
    stale = [f for f in files if f.mtime < cutoff]

    print(f"\nRetention: removing files in {OUTBOUND_DIR} older than {retention_days} days "
          f"(before {cutoff:%Y-%m-%d %H:%M}).")
    if not stale:
        print("  Nothing to remove.")
        return
    for f in stale:
        print(f"  {f.mtime:%Y-%m-%d %H:%M}  ({f.age.days} days old)  {f.name}")

    if dry_run:
        print(f"  Dry run: {len(stale)} file(s) would be deleted.")
        return
    if not assume_yes:
        answer = input(f"  Delete these {len(stale)} file(s) from the server? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("  Skipped deletion.")
            return
    for f in stale:
        sftp.remove(remote_join(OUTBOUND_DIR, f.name))
        print(f"  Deleted {f.name}")


# --------------------------------------------------------------------------- #
# Step 4: upload files destined for SARS to the root directory
# --------------------------------------------------------------------------- #
def upload_to_root(sftp: paramiko.SFTPClient, paths: list[Path], dry_run: bool) -> None:
    if not paths:
        return
    print(f"\nUploading {len(paths)} file(s) to the root directory ...")
    for p in paths:
        if not p.is_file():
            sys.exit(f"Upload source '{p}' is not a file.")
        remote_name = p.name
        if dry_run:
            print(f"  Dry run: would upload {p} -> {remote_name}")
            continue
        tmp_name = f".{remote_name}.part"
        sftp.put(str(p), tmp_name)
        remote_size = sftp.stat(tmp_name).st_size
        if remote_size != p.stat().st_size:
            sftp.remove(tmp_name)
            sys.exit(f"Upload of {p.name} is incomplete ({remote_size} of {p.stat().st_size} bytes).")
        try:
            sftp.posix_rename(tmp_name, remote_name)
        except OSError:
            # Server without POSIX rename: fall back to remove + rename.
            try:
                sftp.remove(remote_name)
            except FileNotFoundError:
                pass
            sftp.rename(tmp_name, remote_name)
        print(f"  {p.name}: {remote_size:,} B uploaded")


# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download SARS outbound UIF files, prune the outbound folder and upload files for SARS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:")[1] if "Examples:" in __doc__ else None,
    )
    parser.add_argument("--host", default=os.environ.get("SARS_SFTP_HOST"),
                        help="SFTP host of the SARS test environment (or env SARS_SFTP_HOST)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SARS_SFTP_PORT", "22")),
                        help="SFTP port (default 22, or env SARS_SFTP_PORT)")
    parser.add_argument("--username", default=os.environ.get("SARS_SFTP_USER"),
                        help="Username; prompted for if omitted")
    parser.add_argument("--download-dir", type=Path, default=Path("downloads") / OUTBOUND_DIR,
                        help="Local folder for downloaded files (default: downloads/SARSOUTBOUND)")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                        help=f"Delete SARSOUTBOUND files older than this many days (default {DEFAULT_RETENTION_DAYS})")
    parser.add_argument("--no-retention", action="store_true",
                        help="Skip the retention clean-up step")
    parser.add_argument("--upload", nargs="+", type=Path, default=[], metavar="FILE",
                        help="Local file(s) destined for SARS; uploaded to the root directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be deleted/uploaded without changing the server")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Do not ask for confirmation (content review and deletion)")
    parser.add_argument("--strict-host-key", action="store_true",
                        help="Reject hosts not present in known_hosts instead of warning")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Network timeout in seconds (default 30)")
    args = parser.parse_args(argv)
    if not args.host:
        args.host = input("SFTP host: ").strip()
    if not args.host:
        parser.error("--host is required (or set SARS_SFTP_HOST)")
    if args.retention_days < 1:
        parser.error("--retention-days must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    username, password = prompt_credentials(args.username)

    started = time.time()
    try:
        client, sftp = connect(args.host, args.port, username, password,
                               args.strict_host_key, args.timeout)
    except paramiko.AuthenticationException:
        print("Authentication failed: check the username and password.", file=sys.stderr)
        return 2
    except (paramiko.SSHException, OSError) as exc:
        print(f"Could not connect to {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 2
    finally:
        password = ""  # do not keep the password around longer than needed

    try:
        ensure_remote_dir(sftp, OUTBOUND_DIR)
        downloaded = download_inbound(sftp, args.download_dir)
        confirm_content(downloaded, args.yes)
        if args.no_retention:
            print("\nRetention step skipped (--no-retention).")
        else:
            apply_retention(sftp, args.retention_days, args.dry_run, args.yes)
        upload_to_root(sftp, args.upload, args.dry_run)
    except (paramiko.SSHException, OSError) as exc:
        print(f"\nSFTP operation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        sftp.close()
        client.close()

    print(f"\nDone in {time.time() - started:.1f}s.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
