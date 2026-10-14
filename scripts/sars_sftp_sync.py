#!/usr/bin/env python3
"""
SARS SFTP exchange helper.

What it does, in order:
  1. Prompts for a username and password and connects to the SARS test
     environment (sftptest.sars.gov.za) over SFTP.
  2. Looks in the ``SARSOUTBOUND`` sub-directory of the login root for ``.psv``
     files whose name starts with ``UIF_1_``, downloads them and prints a
     content summary (size, line count, column count per line, a preview) so
     the operator can confirm they are satisfied with the content.
  3. Maintains ``SARSOUTBOUND`` by deleting remote files that are older than
     the retention period (default: one month / 31 days). Deletion requires an
     explicit confirmation unless ``--yes`` is given; ``--dry-run`` only reports.
  4. Uploads any files given with ``--upload`` to the root directory, which is
     where files destined for SARS must be placed.

The SARS endpoint is an IBM DataPower appliance, which is a limited SFTP
server: it offers only the legacy ssh-rsa host key (hence paramiko < 4), may
not support stat/realpath/rename, and may not report file timestamps. The
script works around these where it can and never deletes a file whose age
it cannot establish.

Examples:
  python3 sars_sftp_sync.py                                   # test environment
  python3 sars_sftp_sync.py --host sftp.sars.gov.za           # production
  python3 sars_sftp_sync.py --dry-run --upload UIF_2_20261014.psv
  python3 sars_sftp_sync.py --verbose                         # debug a login problem
  python3 sars_sftp_sync.py --inspect downloads/SARSOUTBOUND/*.psv   # re-check local files offline
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

try:
    import paramiko
except ImportError:  # pragma: no cover - guidance for the operator
    sys.exit("paramiko is required: pip install 'paramiko>=3.4,<4'")


DEFAULT_HOST = "sftptest.sars.gov.za"
OUTBOUND_DIR = "SARSOUTBOUND"
INBOUND_PREFIX = "UIF_1_"
INBOUND_SUFFIX = ".psv"
EXPECTED_INBOUND_FILES = 2
DEFAULT_RETENTION_DAYS = 31
PREVIEW_LINES = 5
CHUNK = 32 * 1024
PREFETCH_LIMIT = 64 * 1024 * 1024  # pipeline reads only when the listed size is plausible


@dataclass
class RemoteFile:
    name: str
    size: int | None
    mtime: datetime | None

    @property
    def age(self) -> timedelta | None:
        return None if self.mtime is None else datetime.now() - self.mtime

    def describe(self) -> str:
        when = f"{self.mtime:%Y-%m-%d %H:%M}" if self.mtime else "unknown date   "
        size = f"{self.size:>10,} B" if self.size is not None else "   unknown size"
        return f"{when}  {size}  {self.name}"


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


def known_hosts_path() -> Path:
    return Path.home() / ".ssh" / "known_hosts"


def probe_auth_methods(host: str, port: int, username: str, timeout: float) -> list[str]:
    """Ask the server which authentication methods it accepts for this user."""
    transport = paramiko.Transport((host, port))
    try:
        transport.banner_timeout = timeout
        transport.start_client(timeout=timeout)
        try:
            transport.auth_none(username)
        except paramiko.BadAuthenticationType as exc:
            return list(exc.allowed_types)
        return ["none"]
    finally:
        transport.close()


def _check_host_key(transport: paramiko.Transport, host: str, port: int, strict: bool) -> None:
    key = transport.get_remote_server_key()
    hostname = host if port == 22 else f"[{host}]:{port}"
    keys = paramiko.HostKeys()
    if known_hosts_path().exists():
        try:
            keys.load(str(known_hosts_path()))
        except OSError:
            pass
    if keys.check(hostname, key):
        return
    msg = f"Unknown {key.get_name()} host key for {hostname}: {key.get_fingerprint().hex()}"
    if strict:
        raise paramiko.SSHException(msg + " (rejected: --strict-host-key)")
    print("WARNING: " + msg, file=sys.stderr)


def connect_interactive(host: str, port: int, username: str, password: str,
                        strict_host_key: bool, timeout: float
                        ) -> tuple[paramiko.Transport, paramiko.SFTPClient]:
    """Log in with SSH keyboard-interactive auth, answering every prompt with the password."""
    transport = paramiko.Transport((host, port))
    transport.banner_timeout = timeout
    transport.start_client(timeout=timeout)
    try:
        _check_host_key(transport, host, port, strict_host_key)
        transport.auth_interactive(
            username, lambda title, instructions, prompts: [password] * len(prompts))
        sftp = paramiko.SFTPClient.from_transport(transport)
        if sftp is None:
            raise paramiko.SSHException("server refused to open an SFTP session")
        return transport, sftp
    except Exception:
        transport.close()
        raise


def connect(host: str, port: int, username: str, password: str,
            strict_host_key: bool, timeout: float):
    """Return (closeable, sftp). Tries normal password auth, then keyboard-interactive."""
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if known_hosts_path().exists():
        try:
            client.load_host_keys(str(known_hosts_path()))
        except OSError:
            pass
    policy = paramiko.RejectPolicy() if strict_host_key else paramiko.WarningPolicy()
    client.set_missing_host_key_policy(policy)

    print(f"Connecting to {host}:{port} as {username} ...")
    try:
        client.connect(hostname=host, port=port, username=username, password=password,
                       look_for_keys=False, allow_agent=False, timeout=timeout,
                       banner_timeout=timeout, auth_timeout=timeout)
        sftp = client.open_sftp()
        closeable = client
    except paramiko.AuthenticationException:
        client.close()
        try:
            allowed = probe_auth_methods(host, port, username, timeout)
        except (paramiko.SSHException, OSError):
            allowed = []
        if "keyboard-interactive" not in allowed:
            raise
        print("Password authentication was rejected; retrying with keyboard-interactive ...")
        closeable, sftp = connect_interactive(host, port, username, password,
                                              strict_host_key, timeout)

    sftp.get_channel().settimeout(timeout)
    try:
        root = sftp.normalize(".")
    except OSError:
        root = "(server does not report it)"
    print(f"Connected. Root directory: {root}")
    return closeable, sftp


# --------------------------------------------------------------------------- #
# Remote helpers (tolerant of DataPower's partial SFTP support)
# --------------------------------------------------------------------------- #
def list_remote_files(sftp: paramiko.SFTPClient, directory: str) -> list[RemoteFile]:
    files: list[RemoteFile] = []
    for attr in sftp.listdir_attr(directory):
        if attr.st_mode is not None and stat.S_ISDIR(attr.st_mode):
            continue
        if attr.filename in (".", ".."):
            continue
        mtime = datetime.fromtimestamp(attr.st_mtime) if attr.st_mtime else None
        size = attr.st_size if attr.st_size is not None else None
        files.append(RemoteFile(name=attr.filename, size=size, mtime=mtime))
    return sorted(files, key=lambda f: f.name)


def path_candidates(directory: str, name: str) -> list[str]:
    """DataPower is picky about path form; try relative, absolute and ./-prefixed."""
    rel = f"{directory.rstrip('/')}/{name}" if directory not in ("", ".") else name
    return [rel, "/" + rel, "./" + rel]


def ensure_remote_dir(sftp: paramiko.SFTPClient, directory: str) -> None:
    try:
        st = sftp.stat(directory)
        if st.st_mode is not None and not stat.S_ISDIR(st.st_mode):
            sys.exit(f"Remote path '{directory}' exists but is not a directory.")
        return
    except FileNotFoundError:
        sys.exit(f"Remote directory '{directory}' was not found in the root directory.")
    except OSError:
        pass  # stat unsupported; fall back to listing
    try:
        sftp.listdir_attr(directory)
    except OSError as exc:
        sys.exit(f"Remote directory '{directory}' could not be read: {exc}")


def download_file(sftp: paramiko.SFTPClient, directory: str, f: RemoteFile, local_path: Path) -> str:
    """Download without relying on stat/realpath. Returns the remote path that worked."""
    errors = []
    for remote_path in path_candidates(directory, f.name):
        try:
            with sftp.open(remote_path, "rb") as rf:
                # Sizes reported by DataPower are unreliable; only pipeline reads for
                # plausible sizes, otherwise read sequentially until EOF.
                if f.size and f.size <= PREFETCH_LIMIT:
                    rf.prefetch(f.size)
                with open(local_path, "wb") as lf:
                    shutil.copyfileobj(rf, lf, CHUNK)
            return remote_path
        except OSError as exc:
            errors.append(f"{remote_path}: {exc}")
            local_path.unlink(missing_ok=True)
    raise OSError("could not open the remote file under any path form:\n    " + "\n    ".join(errors))


# --------------------------------------------------------------------------- #
# Step 2: download and verify UIF_1_*.psv
# --------------------------------------------------------------------------- #
def download_inbound(sftp: paramiko.SFTPClient, local_dir: Path, skip_existing: bool = False) -> list[Path]:
    local_dir.mkdir(parents=True, exist_ok=True)
    all_files = list_remote_files(sftp, OUTBOUND_DIR)

    print(f"\n{OUTBOUND_DIR} contains {len(all_files)} file(s):")
    for f in all_files:
        print("  " + f.describe())

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
        local_path = local_dir / f.name
        if skip_existing and local_path.is_file() and local_path.stat().st_size > 0 \
                and (f.size is None or local_path.stat().st_size == f.size):
            print(f"  {f.name}: already downloaded ({local_path.stat().st_size:,} B), skipped")
            downloaded.append(local_path)
            continue
        used = download_file(sftp, OUTBOUND_DIR, f, local_path)
        local_size = local_path.stat().st_size
        note = ""
        if f.size is not None and local_size != f.size:
            note = f"  (server listed {f.size:,} B; DataPower sizes are often wrong, check the content below)"
        print(f"  {f.name}: {local_size:,} B downloaded via {used}{note}")
        if local_size == 0:
            print(f"  WARNING: {f.name} came down empty.")
        downloaded.append(local_path)
    return downloaded


ID_NUMBER_RE = re.compile(r"^\d{13}$")
NULLISH = {"", "NULL", "null", "None"}


def summarise_psv(path: Path, repair_to: Path | None = None) -> bool:
    """Stream a pipe-separated file and print a summary. Returns True if it looks sane.

    Works line by line so multi-gigabyte files do not need to fit in memory.
    Recognises a record that was split over two lines by a line break inside a
    field (the two short lines together have exactly the usual column count) and,
    with ``repair_to``, writes a copy with such records joined by a space.
    """
    size = path.stat().st_size
    total = 0
    non_empty = 0
    col_hist: dict[int, int] = {}
    examples: dict[int, list[tuple[int, str]]] = {}
    preview: list[str] = []
    last_line = ""
    last_cols = 0
    decode_errors = 0
    header_cols = 0
    header: list[str] = []
    id_col = -1
    bad_ids: list[tuple[int, str]] = []
    bad_id_count = 0
    splits: list[tuple[int, str]] = []
    pending: tuple[int, str, int] | None = None  # (lineno, text, cols) of a short line awaiting its tail
    out = open(repair_to, "w", encoding="utf-8", newline="\n") if repair_to else None

    def emit(text: str) -> None:
        if out is not None:
            out.write(text + "\n")

    with open(path, "rb") as fh:
        for raw in fh:
            total += 1
            try:
                ln = raw.decode("utf-8-sig" if total == 1 else "utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                decode_errors += 1
                ln = raw.decode("latin-1").rstrip("\r\n")
            if total <= PREVIEW_LINES:
                preview.append(ln)
            if not ln.strip():
                emit(ln)
                continue
            non_empty += 1
            cols = ln.count("|") + 1
            record_line = total
            if total == 1:
                header_cols = cols
                header = ln.split("|")
                id_col = next((i for i, h in enumerate(header) if h.strip().lower() == "id number"), -1)
            # Join a short line with the previous short line when together they form one record.
            if pending is not None and header_cols and cols < header_cols \
                    and pending[2] + cols - 1 == header_cols:
                joined = pending[1] + " " + ln
                splits.append((pending[0], joined))
                record_line = pending[0]
                col_hist[pending[2]] -= 1
                if col_hist[pending[2]] == 0:
                    del col_hist[pending[2]]
                examples[pending[2]] = [e for e in examples.get(pending[2], []) if e[0] != pending[0]]
                pending = None
                ln, cols = joined, header_cols
            else:
                pending = None
            col_hist[cols] = col_hist.get(cols, 0) + 1
            examples.setdefault(cols, [])
            if len(examples[cols]) < 3:
                examples[cols].append((total, ln))
            if header_cols and cols < header_cols:
                pending = (total, ln, cols)
                continue  # do not emit yet; it may be the head of a split record
            if id_col >= 0 and total > 1 and cols == header_cols:
                value = ln.split("|")[id_col].strip()
                if value not in NULLISH and not ID_NUMBER_RE.match(value):
                    bad_id_count += 1
                    if len(bad_ids) < 3:
                        bad_ids.append((record_line, value))
            emit(ln)
            last_line, last_cols = ln, cols
        if pending is not None:
            emit(pending[1])
            last_line, last_cols = pending[1], pending[2]
    if out is not None:
        out.close()

    def clip(text: str) -> str:
        return text if len(text) <= 160 else text[:157] + "..."

    print(f"\n=== {path.name} ===")
    print(f"  size:        {size:,} bytes")
    print(f"  encoding:    utf-8" + (f" ({decode_errors} line(s) not valid UTF-8)" if decode_errors else ""))
    print(f"  lines:       {total:,} ({non_empty:,} non-empty)")
    if col_hist:
        dist = ", ".join(f"{c} cols x {n:,}" for c, n in sorted(col_hist.items(), key=lambda kv: -kv[1]))
        print(f"  columns:     {dist}")
    else:
        print("  columns:     n/a")
    print(f"  preview (first {PREVIEW_LINES} lines):")
    for ln in preview:
        print(f"    | {clip(ln)}")
    if total > PREVIEW_LINES:
        print(f"    | ... ({total - PREVIEW_LINES:,} more line(s))")
        print(f"  last line:   {clip(last_line)}")

    problems = []
    notes = []
    if size == 0:
        problems.append("file is empty")
    if non_empty and max(col_hist, default=1) == 1:
        problems.append("no pipe delimiters found")
    if splits:
        problems.append(f"{len(splits)} record(s) split over two lines by a line break inside a field")
        print(f"  split records ({len(splits)}); joined they read:")
        for lineno, joined in splits[:3]:
            print(f"    line {lineno:,}: {clip(joined)}")
    if len(col_hist) > 1:
        main_cols = max(col_hist, key=col_hist.get)
        odd = {c: n for c, n in col_hist.items() if c != main_cols}
        only_trailer = (sum(odd.values()) == 1 and last_cols != main_cols)
        if only_trailer:
            notes.append(f"the last line has {last_cols} columns instead of {main_cols}; "
                         "this looks like a trailer record")
        else:
            problems.append(f"{sum(odd.values()):,} row(s) do not have the usual {main_cols} columns")
            for c, n in sorted(odd.items()):
                print(f"  rows with {c} columns ({n:,}); first examples:")
                for lineno, ln in examples[c]:
                    print(f"    line {lineno:,}: {clip(ln)}")
    if bad_id_count:
        problems.append(f"{bad_id_count:,} ID Number value(s) are not 13 digits")
        print(f"  ID Number values that are not 13 digits ({bad_id_count:,}); first examples:")
        for lineno, value in bad_ids:
            print(f"    line {lineno:,}: {value!r}")
    if decode_errors:
        problems.append("some lines are not valid UTF-8")
    for n in notes:
        print(f"  note:        {n}")
    if repair_to:
        print(f"  repaired copy written to {repair_to} (split records joined; original untouched)")
    if problems:
        print("  checks:      " + "; ".join(problems))
        return False
    print("  checks:      passed")
    return True


def confirm_content(paths: list[Path], assume_yes: bool) -> None:
    if not paths:
        return
    all_ok = all([summarise_psv(p) for p in paths])
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
    undated = [f for f in files if f.mtime is None]
    stale = [f for f in files if f.mtime is not None and f.mtime < cutoff]

    print(f"\nRetention: removing files in {OUTBOUND_DIR} older than {retention_days} days "
          f"(before {cutoff:%Y-%m-%d %H:%M}).")
    if undated:
        print(f"  NOTE: the server reports no modification time for {len(undated)} file(s); "
              "they are left alone because their age cannot be established:")
        for f in undated:
            print(f"    {f.name}")
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
        last_error = None
        for remote_path in path_candidates(OUTBOUND_DIR, f.name):
            try:
                sftp.remove(remote_path)
                print(f"  Deleted {f.name}")
                break
            except OSError as exc:
                last_error = exc
        else:
            print(f"  Could not delete {f.name}: {last_error}")


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
        local_size = p.stat().st_size
        if dry_run:
            print(f"  Dry run: would upload {p} -> {remote_name} ({local_size:,} B)")
            continue
        # Write straight to the final name: DataPower may process a file as soon as it
        # is closed and may not support rename, so a temporary name is not safe here.
        with open(p, "rb") as lf, sftp.open(remote_name, "wb") as rf:
            shutil.copyfileobj(lf, rf, CHUNK)
        remote_size = None
        try:
            remote_size = next((f.size for f in list_remote_files(sftp, ".") if f.name == remote_name), None)
        except OSError:
            pass
        if remote_size is None:
            print(f"  {p.name}: {local_size:,} B uploaded (server does not report size for verification)")
        elif remote_size != local_size:
            sys.exit(f"Upload of {p.name} looks incomplete ({remote_size:,} of {local_size:,} bytes).")
        else:
            print(f"  {p.name}: {local_size:,} B uploaded and verified")


# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download SARS outbound UIF files, prune the outbound folder and upload files for SARS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:")[1] if "Examples:" in __doc__ else None,
    )
    parser.add_argument("--host", default=os.environ.get("SARS_SFTP_HOST", DEFAULT_HOST),
                        help=f"SFTP host (default {DEFAULT_HOST}, the test environment; "
                             "production is sftp.sars.gov.za; or env SARS_SFTP_HOST)")
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
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print the SSH negotiation (paramiko debug log) to stderr")
    parser.add_argument("--inspect", nargs="+", type=Path, default=[], metavar="FILE",
                        help="Only summarise these already-downloaded .psv files; do not connect")
    parser.add_argument("--repair", action="store_true",
                        help="With --inspect: also write <name>.repaired.psv with split records joined")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Do not re-download a file that already exists locally with the listed size")
    args = parser.parse_args(argv)
    if not args.host:
        parser.error("--host is required (or set SARS_SFTP_HOST)")
    if args.retention_days < 1:
        parser.error("--retention-days must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.inspect:
        ok = True
        for p in args.inspect:
            if not p.is_file():
                sys.exit(f"'{p}' is not a file.")
            target = p.with_name(p.stem + ".repaired" + p.suffix) if args.repair else None
            ok = summarise_psv(p, target) and ok
        return 0 if ok else 1
    if args.verbose:
        import logging
        logging.basicConfig(stream=sys.stderr, level=logging.DEBUG,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        logging.getLogger("paramiko").setLevel(logging.DEBUG)
    username, password = prompt_credentials(args.username)

    started = time.time()
    try:
        closeable, sftp = connect(args.host, args.port, username, password,
                                  args.strict_host_key, args.timeout)
    except paramiko.AuthenticationException:
        print("Authentication failed: the server rejected the username/password.", file=sys.stderr)
        try:
            allowed = probe_auth_methods(args.host, args.port, username, args.timeout)
            print(f"Authentication methods the server accepts for '{username}': "
                  f"{', '.join(allowed)}", file=sys.stderr)
            if "password" not in allowed and "keyboard-interactive" not in allowed:
                print("The server does not accept password logins for this user; "
                      "it may expect an SSH key or a different username format.", file=sys.stderr)
        except (paramiko.SSHException, OSError) as exc:
            print(f"(Could not query accepted authentication methods: {exc})", file=sys.stderr)
        print("Check: the exact username (including any domain prefix such as DOMAIN\\user), "
              "the password (no trailing spaces), and that the account has been activated. "
              "Re-run with --verbose to see the SSH negotiation.", file=sys.stderr)
        return 2
    except paramiko.ssh_exception.IncompatiblePeer as exc:
        print(f"Could not connect to {args.host}:{args.port}: {exc}", file=sys.stderr)
        print("The server only offers SSH algorithms that this paramiko version "
              f"({paramiko.__version__}) does not support. paramiko 4 and later dropped the "
              "legacy ssh-rsa (SHA-1) host key that the SARS server uses.\n"
              "Fix: pip install 'paramiko>=3.4,<4'", file=sys.stderr)
        return 2
    except (paramiko.SSHException, OSError) as exc:
        print(f"Could not connect to {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 2
    finally:
        password = ""  # do not keep the password around longer than needed

    try:
        ensure_remote_dir(sftp, OUTBOUND_DIR)
        downloaded = download_inbound(sftp, args.download_dir, args.skip_existing)
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
        closeable.close()

    print(f"\nDone in {time.time() - started:.1f}s.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
