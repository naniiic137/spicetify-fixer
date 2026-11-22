"""spicetify-fixer: one click to get Spotify + Spicetify (+ Marketplace) working, then open Spotify.

Run it with:  py spicetify_fixer.py            (fix everything, then open Spotify)
              py spicetify_fixer.py --check    (dry run: only report what it would do)
              py spicetify_fixer.py --help     (all flags)

Python 3.11, standard library only. Windows only (Spotify desktop / Microsoft Store builds).

How it works, in eight steps (each one is logged to the console and to
%LOCALAPPDATA%\\spicetify-fixer\\spicetify-fixer.log):

  1. Find Spotify: the Microsoft Store build (Appx) and/or the desktop build (%APPDATA%\\Spotify).
  2. If it is the Store build, explain why it keeps breaking and offer a one-time switch
     to the desktop build (asked once, the answer is remembered).
  3. Keep the desktop build up to date with winget.
  4. Install Spicetify if it is missing, then upgrade it.
  5. Point Spicetify's config at the Spotify that is really installed.
  6. Re-patch Spotify (backup / restore / apply), answering every prompt by itself.
  7. Make sure the Marketplace is installed and enabled.
  8. Open Spotify.

The patch decisions in step 6 mirror Spicetify's own source code (v2.46.x):
  * "Spotify's files" = the Apps folder inside spotify_path. Stock files are *.spa archives;
    a patched Apps folder has unpacked folders (xpui, login) and no *.spa files.
  * The Store build is never patched in place: Spicetify writes the patched copy to
    %APPDATA%\\spicetify\\AppX and starts Spotify with --app-directory pointing there.
  * "spicetify restore" refuses to run when the backup is from another Spotify version, and when
    Spotify has just updated it would copy OLD files over the new ones. So "restore" is only used
    when Spotify is still patched and the backup matches it. After an update (stock files),
    "backup apply" is the right fix: Spicetify clears the old backup by itself.
"""

from __future__ import annotations

import argparse
import configparser
import ctypes
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime

__version__ = "1.0.0"

# --------------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------------

STORE_PACKAGE = "SpotifyAB.SpotifyMusic"
STORE_APP_ID = "Spotify"  # Application Id inside the Store package (shell:AppsFolder\<PFN>!Spotify)
WINGET_ID = "Spotify.Spotify"
DOWNLOAD_PAGE = "https://www.spotify.com/download/windows/"
SPICETIFY_INSTALLER = "https://raw.githubusercontent.com/spicetify/cli/main/install.ps1"
MARKETPLACE_INSTALLER = "https://raw.githubusercontent.com/spicetify/marketplace/main/resources/install.ps1"
SPICETIFY_LATEST_API = "https://api.github.com/repos/spicetify/cli/releases/latest"
MARKETPLACE_LATEST_API = "https://api.github.com/repos/spicetify/marketplace/releases/latest"  # 22/11
SHORTCUT_NAME = "Spotify (Spicetify fixed)"

# Every Spicetify prompt is a "[y/N]" question read from stdin (cmd.go ReadAnswer). In v2.46.x the
# only one is the Store build's "Continue backing up anyway?". Spicetify reads stdin with a fresh
# buffer per question, so later questions may see end-of-input, which simply means "default".
# Feeding several "y" lines can never block: the process just ignores what it does not read.
SPICETIFY_ANSWERS = "y\n" * 8

# Subprocess time limits (seconds). Nothing is allowed to hang forever.
TIMEOUT_QUICK = 60          # tasklist, Get-AppxPackage, spicetify -v / -c / config
TIMEOUT_WINGET_SHOW = 180   # winget show (may refresh its source first)
TIMEOUT_WINGET_INSTALL = 900
TIMEOUT_SPICETIFY = 300     # backup / apply / restore / upgrade
TIMEOUT_INSTALLER = 600     # Spicetify / Marketplace PowerShell installers
PREFS_WAIT_SECONDS = 120    # first start of a new Spotify, until it writes its prefs file

# winget exit codes that mean "nothing to do" rather than a failure.
WINGET_OK_CODES = {
    0,
    0x8A15002B,  # APPINSTALLER_CLI_ERROR_UPDATE_NOT_APPLICABLE (already the newest version)
    0x8A150061,  # APPINSTALLER_CLI_ERROR_PACKAGE_ALREADY_INSTALLED
}

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
VERSION_RE = re.compile(r"\d+(?:\.\d+){1,3}")

log = logging.getLogger("spicetify-fixer")


# ---- colours in the console (22/11): cyan = a step, green = already fine, yellow = will be done / needs fixing,
# red = a problem. Plain information keeps no colour. Off with --no-color, NO_COLOR, or when the output isn't a console.
CYAN, GREEN, YELLOW, RED, BOLD, RESET = "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"
_GREEN_WORDS = ("up to date", "is installed and enabled", ": present", "already", "done.", "patched and ready",
                "nothing to do", "no fix needed", "looks right", "found the desktop spotify", "opened spotify", " ok")
_YELLOW_WORDS = ("[check] would", "would run", "would start", "would open", "would switch", "would ask",
                 "needs fixing", "will ", "updating", "switching", "installing", "patching", "out of date",
                 "older than", "newer version", "plan would be", "re-patch", "not patched")
_RED_WORDS = ("failed", "error", "could not", "timed out", "refuses", "not found", "missing")


def color_for(message: str, level: int) -> str:
    """The colour for one console line ('' = none)."""
    text = message.lower()
    if level >= logging.ERROR or any(w in text for w in _RED_WORDS):
        return RED
    if level >= logging.WARNING:
        return YELLOW
    if text.startswith("== step"):
        return BOLD + CYAN
    if any(w in text for w in _YELLOW_WORDS):
        return YELLOW
    if any(w in text for w in _GREEN_WORDS):
        return GREEN
    return ""


class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        c = color_for(record.getMessage(), record.levelno)
        return f"{c}{line}{RESET}" if c and line.strip() else line


def enable_colors(stream) -> bool:
    """Turns on colours in a Windows console (virtual terminal mode). False: leave the text plain."""
    if os.environ.get("NO_COLOR") or not getattr(stream, "isatty", lambda: False)():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        handle = k.GetStdHandle(-11)  # the console's output
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:  # noqa: BLE001 - colours are a nicety, never a failure
        return False


class FixerError(Exception):
    """A problem the owner has to see. The message says what happened and what to do."""


# --------------------------------------------------------------------------------------------
# Small helpers: versions, admin, file versions
# --------------------------------------------------------------------------------------------

def parse_version(text: str | None) -> tuple[int, ...] | None:
    """'1.2.50.335.g1234abcd' -> (1, 2, 50, 335). Only the first four numbers count."""
    if not text:
        return None
    match = VERSION_RE.search(str(text).strip().strip('"'))
    if not match:
        return None
    parts = [int(p) for p in match.group(0).split(".")][:4]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)


def normalize_store_version(text: str | None) -> tuple[int, ...] | None:
    """The Store package squeezes Spotify's version: Appx '1.310.2586.0' is Spotify 1.3.10.2586.

    The second number holds minor + patch together (310 -> 3 and 10), so split its first digit off.
    """
    parts = parse_version(text)
    if not parts:
        return None
    major, squeezed, build, _ = parts
    digits = str(squeezed)
    if len(digits) >= 2:
        return (major, int(digits[0]), int(digits[1:]), build)
    return parts


def version_text(version: tuple[int, ...] | None) -> str:
    return ".".join(str(p) for p in version) if version else "unknown"


def compare_versions(a: tuple[int, ...] | None, b: tuple[int, ...] | None) -> int | None:
    """-1 if a < b, 0 if equal, 1 if a > b, None if either is unknown."""
    if a is None or b is None:
        return None
    return (a > b) - (a < b)


def is_admin() -> bool:
    """True when this process runs elevated (as administrator)."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def get_file_version(path: str) -> str | None:
    """Read an .exe's file version (like '1.2.50.335') with the Windows version API."""
    try:
        from ctypes import wintypes

        api = ctypes.windll.version
        size = api.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not api.GetFileVersionInfoW(path, 0, size, buf):
            return None
        ptr, length = ctypes.c_void_p(), wintypes.UINT()
        if not api.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)) or not ptr.value:
            return None
        info = (wintypes.DWORD * 4).from_address(ptr.value)  # VS_FIXEDFILEINFO: sig, struc, MS, LS
        ms, ls = info[2], info[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (AttributeError, OSError, ValueError):
        return None


def read_prefs_version(prefs_path: str) -> str | None:
    """Spotify's prefs file has a line like: app.last-launched-version="1.2.50.335.g1234"."""
    try:
        with open(prefs_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("app.last-launched-version"):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        return None
    return None


def parse_winget_version(output: str) -> tuple[int, ...] | None:
    """Find the package version in `winget show` output. Works in any UI language.

    English prints 'Version: 1.2.3.4.gabc', French 'Version : ...'. We look for the 'Version' line
    first and fall back to the first Spotify-style version (four numbers + .g<hash>) anywhere.
    """
    for line in output.splitlines():
        match = re.match(r"^\s*Version\s*:\s*(\S+)", line, re.IGNORECASE)
        if match:
            parsed = parse_version(match.group(1))
            if parsed:
                return parsed
    match = re.search(r"\b\d+\.\d+\.\d+\.\d+\.g[0-9a-f]+\b", output)
    return parse_version(match.group(0)) if match else None


def winget_ok(code: int | None) -> bool:
    return code is not None and (code & 0xFFFFFFFF) in WINGET_OK_CODES


def clean_output(text: str) -> str:
    return ANSI_RE.sub("", text or "").replace("\r", "")


def decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("mbcs" if os.name == "nt" else "latin-1", errors="replace")


def same_path(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    norm = lambda p: os.path.normcase(os.path.normpath(os.path.expandvars(p.strip().strip('"'))))
    return norm(a) == norm(b)


# --------------------------------------------------------------------------------------------
# Paths (built from the environment so tests can point everything at a temp folder)
# --------------------------------------------------------------------------------------------

@dataclass
class Paths:
    appdata: str
    localappdata: str

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Paths":
        env = env if env is not None else os.environ
        home = os.path.expanduser("~")
        return cls(
            appdata=env.get("APPDATA") or os.path.join(home, "AppData", "Roaming"),
            localappdata=env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local"),
        )

    # Desktop Spotify
    @property
    def desktop_dir(self) -> str:
        return os.path.join(self.appdata, "Spotify")

    @property
    def desktop_exe(self) -> str:
        return os.path.join(self.desktop_dir, "Spotify.exe")

    @property
    def desktop_prefs(self) -> str:
        return os.path.join(self.desktop_dir, "prefs")

    # Store Spotify
    def store_prefs(self, family_name: str) -> str:
        return os.path.join(self.localappdata, "Packages", family_name, "LocalState", "Spotify", "prefs")

    @property
    def store_alias_exe(self) -> str:
        return os.path.join(self.localappdata, "Microsoft", "WindowsApps", "Spotify.exe")

    # Spicetify
    @property
    def spicetify_dir(self) -> str:
        return os.path.join(self.localappdata, "spicetify")

    @property
    def spicetify_exe(self) -> str:
        return os.path.join(self.spicetify_dir, "spicetify.exe")

    @property
    def spicetify_userdata(self) -> str:
        return os.path.join(self.appdata, "spicetify")

    @property
    def default_config(self) -> str:
        return os.path.join(self.spicetify_userdata, "config-xpui.ini")

    # This tool
    @property
    def fixer_dir(self) -> str:
        return os.path.join(self.localappdata, "spicetify-fixer")

    @property
    def state_file(self) -> str:
        return os.path.join(self.fixer_dir, "state.json")

    @property
    def log_file(self) -> str:
        return os.path.join(self.fixer_dir, "spicetify-fixer.log")


# --------------------------------------------------------------------------------------------
# State file: remembers the one-time "switch to desktop?" answer
# --------------------------------------------------------------------------------------------

def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}  # missing or damaged: start fresh, never crash on it


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state = dict(state, updated=datetime.now().isoformat(timespec="seconds"))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------------------------
# Running commands (every call has a timeout; --check skips anything that changes the PC)
# --------------------------------------------------------------------------------------------

@dataclass
class Result:
    code: int | None
    out: str = ""
    timed_out: bool = False
    skipped: bool = False  # True when --check did not really run it

    @property
    def ok(self) -> bool:
        return self.code == 0


class Runner:
    """Runs commands. `changes=True` marks a command that changes the PC: in --check mode it is
    only logged ("WOULD run") and reported as a success, so the dry run can keep going."""

    def __init__(self, check: bool = False):
        self.check = check

    def run(self, cmd: list[str], *, changes: bool, timeout: int, input_text: str | None = None,
            env: dict | None = None, label: str | None = None) -> Result:
        shown = label or " ".join(cmd)
        if changes and self.check:
            log.info("  [check] WOULD run: %s", shown)
            return Result(code=0, skipped=True)
        log.debug("  run: %s", shown)
        return self._execute(cmd, timeout=timeout, input_text=input_text, env=env, shown=shown)

    def _execute(self, cmd: list[str], *, timeout: int, input_text: str | None, env: dict | None,
                 shown: str) -> Result:
        try:
            proc = subprocess.run(
                cmd,
                input=input_text.encode() if input_text is not None else None,
                stdin=None if input_text is not None else subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                env=env,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as exc:
            out = clean_output(decode(exc.stdout) + decode(exc.stderr))
            log.warning("  timed out after %ss: %s", timeout, shown)
            return Result(code=None, out=out, timed_out=True)
        except OSError as exc:
            log.warning("  could not start %s: %s", shown, exc)
            return Result(code=None, out=str(exc))
        out = clean_output(decode(proc.stdout) + decode(proc.stderr))
        for line in out.splitlines():
            if line.strip():
                log.debug("    | %s", line.rstrip())
        log.debug("  exit code %s", proc.returncode)
        return Result(code=proc.returncode, out=out)


def powershell(script: str) -> list[str]:
    return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]


# --------------------------------------------------------------------------------------------
# Spotify detection
# --------------------------------------------------------------------------------------------

@dataclass
class SpotifyInstall:
    kind: str                      # "desktop" or "store"
    version: tuple[int, ...] | None
    folder: str                    # what Spicetify calls spotify_path
    prefs: str                     # what Spicetify calls prefs_path
    exe: str = ""
    family_name: str = ""          # Store only, e.g. SpotifyAB.SpotifyMusic_zpdnekdrzrea0
    raw_version: str = ""

    @property
    def label(self) -> str:
        name = "Microsoft Store Spotify" if self.kind == "store" else "desktop Spotify"
        return f"{name} {version_text(self.version)}"


def detect_store(runner: Runner) -> SpotifyInstall | None:
    """Ask Windows for the Store package (read-only)."""
    script = (f"Get-AppxPackage -Name {STORE_PACKAGE} | "
              "Select-Object Name,Version,PackageFamilyName,InstallLocation | ConvertTo-Json -Compress")
    result = runner.run(powershell(script), changes=False, timeout=TIMEOUT_QUICK, label="Get-AppxPackage")
    text = result.out.strip()
    if not result.ok or not text.startswith(("{", "[")):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    if not data or not data.get("InstallLocation"):
        return None
    folder = data["InstallLocation"]
    family = data.get("PackageFamilyName") or ""
    return SpotifyInstall(
        kind="store",
        version=normalize_store_version(data.get("Version")),
        raw_version=str(data.get("Version") or ""),
        folder=folder,
        prefs="",  # filled in by the caller (needs Paths)
        exe=os.path.join(folder, "Spotify.exe"),
        family_name=family,
    )


def detect_desktop(paths: Paths) -> SpotifyInstall | None:
    if not os.path.isfile(paths.desktop_exe):
        return None
    raw = get_file_version(paths.desktop_exe) or read_prefs_version(paths.desktop_prefs) or ""
    return SpotifyInstall(
        kind="desktop",
        version=parse_version(raw),
        raw_version=raw,
        folder=paths.desktop_dir,
        prefs=paths.desktop_prefs,
        exe=paths.desktop_exe,
    )


# --------------------------------------------------------------------------------------------
# Spicetify config (read-only parsing; changes always go through `spicetify config`)
# --------------------------------------------------------------------------------------------

def read_config(path: str) -> configparser.ConfigParser | None:
    parser = configparser.ConfigParser(delimiters=("=",), interpolation=None, strict=False,
                                       comment_prefixes=(";", "#"), inline_comment_prefixes=None)
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error):
        return None
    return parser


def cfg_get(cfg: configparser.ConfigParser | None, section: str, key: str) -> str:
    if cfg is None or not cfg.has_section(section):
        return ""
    return cfg.get(section, key, fallback="").strip()


def config_fix_needed(cfg: configparser.ConfigParser | None, target: SpotifyInstall) -> list[str]:
    """Return the reasons the config's paths are wrong for `target` (empty list = fine)."""
    reasons = []
    spotify_path = cfg_get(cfg, "Setting", "spotify_path")
    prefs_path = cfg_get(cfg, "Setting", "prefs_path")
    if target.kind == "desktop":
        if "windowsapps" in spotify_path.lower() or STORE_PACKAGE.lower() in spotify_path.lower():
            reasons.append(f"spotify_path points at the Store package ({spotify_path})")
        if STORE_PACKAGE.lower() in prefs_path.lower():
            reasons.append(f"prefs_path points at the Store package ({prefs_path})")
    if not spotify_path:
        reasons.append("spotify_path is empty")
    elif not os.path.isdir(os.path.join(os.path.expandvars(spotify_path), "Apps")):
        reasons.append(f"spotify_path folder is missing ({spotify_path})")
    if not prefs_path:
        reasons.append("prefs_path is empty")
    elif not os.path.isfile(os.path.expandvars(prefs_path)):
        reasons.append(f"prefs_path file is missing ({prefs_path})")
    if not reasons and (not same_path(spotify_path, target.folder) or not same_path(prefs_path, target.prefs)):
        reasons.append("paths point at a different Spotify than the one in use")
    return reasons


def config_fix_command(target: SpotifyInstall) -> list[str]:
    return ["config", "spotify_path", target.folder, "prefs_path", target.prefs]


# --------------------------------------------------------------------------------------------
# Patch state and the restore / backup / apply decision
# --------------------------------------------------------------------------------------------

def apps_state(folder: str) -> str:
    """Same rules as Spicetify's spotifystatus.Get():
    'stock' (only .spa), 'applied' (only folders), 'mixed' (both), 'invalid' (empty), 'missing'."""
    try:
        with os.scandir(folder) as it:
            entries = list(it)
    except OSError:
        return "missing"
    spa = sum(1 for e in entries if e.is_file() and e.name.endswith(".spa"))
    dirs = sum(1 for e in entries if e.is_dir())
    if spa and dirs:
        return "mixed"
    if spa:
        return "stock"
    if dirs:
        return "applied"
    return "invalid"


def marketplace_version(folder: str) -> tuple[int, ...] | None:
    """The installed Marketplace's version, read from its extension.js ("Marketplace v1.0.8"), or None."""
    path = os.path.join(folder, "extension.js")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    m = re.search(r'window\.Marketplace\s*=\s*\{[^}]*?version\s*:\s*"(\d+(?:\.\d+)+)"', text) or \
        re.search(r"Marketplace v(\d+(?:\.\d+)+)", text)
    return parse_version(m.group(1)) if m else None


def backup_state(backup_dir: str, backup_version: str, current_version: tuple[int, ...] | None) -> str:
    """Same idea as Spicetify's backupstatus.Get(): 'empty', 'backed' (matches Spotify) or 'outdated'."""
    try:
        with os.scandir(backup_dir) as it:
            has_spa = any(e.is_file() and e.name.endswith(".spa") for e in it)
    except OSError:
        has_spa = False
    if not has_spa:
        return "empty"
    if compare_versions(parse_version(backup_version), current_version) == 0:
        return "backed"
    return "outdated"


APPLY = ("apply",)
BACKUP_APPLY = ("backup", "apply")
RESTORE_BACKUP_APPLY = ("restore", "backup", "apply")
REINSTALL = ("reinstall",)  # not a Spicetify command: Spotify itself must be reinstalled first


@dataclass
class PatchState:
    source: str          # state of <spotify_path>\Apps (Spotify's own files)
    dest: str            # where the patched copy lives (same folder for desktop, AppX for Store)
    backup: str          # empty / backed / outdated
    backup_with: str     # Spicetify version that made the backup
    spicetify_version: str
    is_store: bool = False

    @property
    def can_backup(self) -> bool:
        return self.source in ("stock", "mixed")

    @property
    def patched(self) -> bool:
        return self.dest == "applied"


def decide_patch(st: PatchState) -> tuple[str, ...]:
    """Pick the Spicetify commands that will bring Spotify to a patched state.

    * Spotify missing / empty                     -> reinstall Spotify, then backup apply
    * Spotify patched, backup matches, same Spicetify -> apply  (refresh themes, apps, extensions)
    * Spotify patched, backup matches, other Spicetify -> restore backup apply (re-preprocess)
    * Spotify not patched (fresh install / just updated) -> backup apply (old backup is cleared)
    * Spotify patched but no usable backup        -> reinstall Spotify (Spicetify cannot recover)
    """
    if st.source in ("missing", "invalid"):
        return REINSTALL
    if st.patched:
        if st.backup == "backed":
            return APPLY if st.backup_with == st.spicetify_version else RESTORE_BACKUP_APPLY
        return BACKUP_APPLY if st.can_backup else REINSTALL
    return BACKUP_APPLY if st.can_backup else REINSTALL


def retry_plan(failed: tuple[str, ...], st: PatchState) -> tuple[str, ...] | None:
    """One retry after a failed patch: restore backup apply when a matching backup exists,
    otherwise backup apply when Spotify's files are stock. None = nothing sensible left to try."""
    if failed != RESTORE_BACKUP_APPLY and st.backup == "backed":
        return RESTORE_BACKUP_APPLY
    if failed != BACKUP_APPLY and st.can_backup:
        return BACKUP_APPLY
    return None


def installer_answers(admin: bool) -> str:
    """Answers for the official Spicetify installer (install.ps1), read before writing this:
      * only when elevated: 'Do you want to abort the installation process?' -> n
      * 'Do you also want to install Spicetify Marketplace?'                  -> y
      * Marketplace installer, only if another theme is active: 'replace it?' -> y
    PowerShell's PromptForChoice reads these lines from stdin; extra lines are ignored."""
    return ("n\n" if admin else "") + "y\n" * 4


# --------------------------------------------------------------------------------------------
# User interaction (console or, under pythonw, a Windows message box)
# --------------------------------------------------------------------------------------------

def has_console() -> bool:
    return sys.stdin is not None and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()


def message_box(text: str, title: str = "spicetify-fixer", yes_no: bool = False, error: bool = False) -> bool:
    """Show a Windows message box. Returns True for Yes/OK."""
    flags = 0x40000  # MB_TOPMOST
    flags |= 0x4 | 0x20 if yes_no else (0x10 if error else 0x40)  # YESNO+QUESTION / ERROR / INFO
    try:
        return ctypes.windll.user32.MessageBoxW(None, text, title, flags) in (1, 6)  # IDOK, IDYES
    except (AttributeError, OSError):
        return False


def ask_yes_no(question: str, default_yes: bool = True) -> bool | None:
    """Ask once. None = nobody could be asked (no console and no message box)."""
    if has_console():
        try:
            answer = input(f"{question} {'[Y/n]' if default_yes else '[y/N]'} ").strip().lower()
        except EOFError:
            return None
        if not answer:
            return default_yes
        return answer.startswith("y")
    try:
        ctypes.windll.user32  # noqa: B018 - only checks that a GUI is available
    except (AttributeError, OSError):
        return None
    return message_box(question, yes_no=True)


# --------------------------------------------------------------------------------------------
# The fixer
# --------------------------------------------------------------------------------------------

@dataclass
class Fixer:
    args: argparse.Namespace
    paths: Paths
    runner: Runner
    admin: bool = False
    state: dict = field(default_factory=dict)
    store: SpotifyInstall | None = None
    desktop: SpotifyInstall | None = None
    active: SpotifyInstall | None = None
    original_store: SpotifyInstall | None = None
    simulated_switch: bool = False   # --check: pretend the desktop switch happened
    just_installed: bool = False
    paths_changed: bool = False
    spicetify: str = ""
    spicetify_version: str = ""
    config_path: str = ""
    warnings: list = field(default_factory=list)

    @property
    def check(self) -> bool:
        return self.runner.check

    # ---------- generic helpers ----------

    def step(self, number: int, title: str) -> None:
        log.info("")
        log.info("== Step %s: %s", number, title)

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        log.warning("  WARNING: %s", text)

    def open_download_page(self) -> None:
        if self.check:
            log.info("  [check] WOULD open %s", DOWNLOAD_PAGE)
            return
        log.info("  Opening %s", DOWNLOAD_PAGE)
        try:
            webbrowser.open(DOWNLOAD_PAGE)
        except webbrowser.Error:
            pass

    def child_env(self) -> dict:
        """Environment for child processes: make sure a freshly installed spicetify is on PATH."""
        env = dict(os.environ)
        env["PATH"] = env.get("PATH", "") + os.pathsep + self.paths.spicetify_dir
        return env

    def winget(self) -> str | None:
        return shutil.which("winget")

    def winget_cmd(self, verb: str, *extra: str) -> list[str]:
        return [self.winget() or "winget", verb, "--id", WINGET_ID, "-e", "--source", "winget", *extra,
                "--silent", "--accept-package-agreements", "--accept-source-agreements",
                "--disable-interactivity"]

    def latest_spotify(self) -> tuple[int, ...] | None:
        if not self.winget():
            return None
        res = self.runner.run(
            [self.winget(), "show", "--id", WINGET_ID, "-e", "--source", "winget",
             "--accept-source-agreements", "--disable-interactivity"],
            changes=False, timeout=TIMEOUT_WINGET_SHOW, label=f"winget show {WINGET_ID}")
        return parse_winget_version(res.out) if res.ok else None

    def spotify_running(self) -> bool:
        res = self.runner.run(["tasklist", "/FI", "IMAGENAME eq Spotify.exe", "/FO", "CSV", "/NH"],
                              changes=False, timeout=TIMEOUT_QUICK, label="tasklist (is Spotify running?)")
        return "spotify.exe" in res.out.lower()

    def close_spotify(self) -> None:
        """Close Spotify politely, then force it. Spotify must be closed before patching or updating."""
        if not self.spotify_running():
            return
        log.info("  Closing Spotify...")
        self.runner.run(["taskkill", "/IM", "Spotify.exe"], changes=True, timeout=TIMEOUT_QUICK)
        if self.check:
            return
        for _ in range(10):
            time.sleep(0.5)
            if not self.spotify_running():
                return
        self.runner.run(["taskkill", "/F", "/T", "/IM", "Spotify.exe"], changes=True, timeout=TIMEOUT_QUICK)
        time.sleep(1.5)

    def start_process(self, cmd: list[str]) -> None:
        """Start a program and let it live on after this tool exits."""
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)

    def spicetify_cmd(self, *args: str) -> list[str]:
        flags = ["--bypass-admin"] if self.admin else []
        return [self.spicetify or self.paths.spicetify_exe, *flags, *args]

    def run_spicetify(self, *args: str, changes: bool = True, timeout: int = TIMEOUT_SPICETIFY) -> Result:
        """Run Spicetify with 'y' answers on stdin so no prompt can ever wait for a key press."""
        return self.runner.run(self.spicetify_cmd(*args), changes=changes, timeout=timeout,
                               input_text=SPICETIFY_ANSWERS, env=self.child_env(),
                               label="spicetify " + " ".join(args))

    # ---------- step 1 ----------

    def find_spotify(self) -> None:
        self.step(1, "Find Spotify")
        self.store = detect_store(self.runner)
        if self.store:
            self.store.prefs = self.paths.store_prefs(self.store.family_name)
            log.info("  Found the Microsoft Store Spotify (package %s, version %s = Spotify %s)",
                     self.store.family_name, self.store.raw_version, version_text(self.store.version))
        self.desktop = detect_desktop(self.paths)
        if self.desktop:
            log.info("  Found the desktop Spotify %s at %s", version_text(self.desktop.version), self.desktop.exe)
        if not self.store and not self.desktop:
            log.info("  Spotify is not installed.")
        # The desktop build is the one Spicetify supports, so it wins when both exist.
        self.active = self.desktop or self.store

    # ---------- step 2 ----------

    def store_switch(self) -> None:
        self.step(2, "Microsoft Store version check")
        if not self.store and not self.desktop:
            self.install_desktop_fresh()
            return
        if not self.store:
            log.info("  Not the Store version: nothing to switch.")
            return
        if self.desktop:
            log.info("  Both builds are installed; the desktop one is used and patched.")
            return
        if not self.decide_switch():
            log.info("  Keeping the Store Spotify and your login; it is patched in place.")
            log.info("  The Store replaces Spotify when it updates, which removes the patch: open Spotify with this")
            log.info("  tool (or its shortcut) and it re-patches only when needed. --switch-to-desktop moves to the")
            log.info("  desktop Spotify instead (that one needs a new login once).")
            return
        self.switch_to_desktop()

    def decide_switch(self) -> bool:
        """22/11 (the owner): with the Store Spotify, keep it and its login and just patch it. Switch to the
        desktop Spotify only with --switch-to-desktop (or to finish a switch that was started and cut off)."""
        if self.args.switch_to_desktop:
            log.info("  --switch-to-desktop: moving to the desktop Spotify (you log in once there).")
            if self.check:
                log.info("  [check] WOULD switch to the desktop Spotify.")
            return True
        if self.state.get("store_switch") == "started":
            log.info("  A switch to the desktop Spotify was started earlier and cut off; finishing it now.")
            return True
        return False

    def require_installable(self) -> None:
        """Checks done BEFORE anything is removed, so a failure can never leave you without Spotify."""
        if self.admin:
            raise FixerError(
                "This window runs as administrator, and Spotify's installer refuses to run as administrator.\n"
                "Close it and run the tool again from a normal (not elevated) window or shortcut.")
        if not self.winget():
            self.open_download_page()
            raise FixerError(
                "winget was not found, so the desktop Spotify cannot be installed automatically.\n"
                f"The download page is open ({DOWNLOAD_PAGE}): install Spotify from there "
                "(not as administrator), then run this tool again.")

    def switch_to_desktop(self) -> None:
        self.require_installable()
        if not self.check:  # remembered, so a switch cut off half-way is finished on the next run
            self.state["store_switch"] = "started"
            save_state(self.paths.state_file, self.state)
        self.close_spotify()
        log.info("  Removing the Microsoft Store Spotify (current user only)...")
        res = self.runner.run(powershell(f"Get-AppxPackage {STORE_PACKAGE} | Remove-AppxPackage"),
                              changes=True, timeout=TIMEOUT_WINGET_INSTALL,
                              label=f"Get-AppxPackage {STORE_PACKAGE} | Remove-AppxPackage")
        if not res.ok:
            raise FixerError("Removing the Store Spotify failed (details in the log). Nothing else was changed.\n"
                             "You can remove it in Settings > Apps, then run this tool again.")
        self.original_store, self.store = self.store, None
        self.install_desktop(reason="Installing the desktop Spotify with winget (a few minutes)...")
        if not self.check:
            self.state["store_switch"] = "done"
            save_state(self.paths.state_file, self.state)
        if self.check:
            self.simulated_switch = True
        else:
            self.make_shortcut(only_refresh=True)  # the old icon pointed at the removed Store app

    def install_desktop_fresh(self) -> None:
        log.info("  No Spotify found: installing the desktop version.")
        self.require_installable()
        self.install_desktop(reason="Installing the desktop Spotify with winget (a few minutes)...")
        if self.check:
            self.simulated_switch = True

    def install_desktop(self, reason: str, force: bool = False) -> None:
        log.info("  %s", reason)
        extra = ("--force",) if force else ()
        res = self.runner.run(self.winget_cmd("install", *extra), changes=True, timeout=TIMEOUT_WINGET_INSTALL,
                              label=f"winget install --id {WINGET_ID} -e{' --force' if force else ''} --silent ...")
        if self.check:
            # Pretend the install worked so the rest of the dry run can be reported.
            self.desktop = SpotifyInstall("desktop", self.latest_spotify(), self.paths.desktop_dir,
                                          self.paths.desktop_prefs, self.paths.desktop_exe)
            self.active = self.desktop
            return
        self.desktop = detect_desktop(self.paths)
        if not winget_ok(res.code) or not self.desktop:
            self.open_download_page()
            raise FixerError(
                "Installing the desktop Spotify with winget failed (details in the log).\n"
                f"The download page is open ({DOWNLOAD_PAGE}): run SpotifySetup.exe from there "
                "(not as administrator), then run this tool again.")
        log.info("  Installed the desktop Spotify %s.", version_text(self.desktop.version))
        self.active = self.desktop
        self.just_installed = True
        self.ensure_desktop_prefs()

    def ensure_desktop_prefs(self) -> None:
        """Spicetify needs Spotify's prefs file, and reads the Spotify version from it. A new or just
        updated Spotify writes it on its first start, so start it once and wait (then close it)."""
        if not self.desktop:
            return
        prefs_ver = parse_version(read_prefs_version(self.desktop.prefs))
        exe_ver = self.desktop.version
        if prefs_ver and (exe_ver is None or prefs_ver == exe_ver):
            return
        if prefs_ver and self.state.get("prefs_synced_for") == self.desktop.raw_version:
            return  # already started once for this exact Spotify build; never do it on every run
        if self.check:
            log.info("  [check] WOULD start Spotify once so it writes its prefs file, then close it")
            return
        log.info("  Starting Spotify once so it writes its settings (up to %ss)...", PREFS_WAIT_SECONDS)
        self.start_process([self.desktop.exe])
        deadline = time.monotonic() + PREFS_WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(2)
            prefs_ver = parse_version(read_prefs_version(self.desktop.prefs))
            if prefs_ver and (exe_ver is None or prefs_ver == exe_ver):
                time.sleep(3)  # let Spotify finish writing
                break
        else:
            self.warn("Spotify did not write its prefs file in time; Spicetify may complain.")
        self.close_spotify()
        self.state["prefs_synced_for"] = self.desktop.raw_version
        save_state(self.paths.state_file, self.state)

    # ---------- step 3 ----------

    def update_spotify(self) -> None:
        self.step(3, "Spotify up to date")
        if self.args.skip_spotify_update:
            log.info("  Skipped (--skip-spotify-update).")
            return
        if not self.active:
            return
        if self.active.kind == "store":
            log.info("  The Store version is updated by the Microsoft Store itself; nothing to do here.")
            return
        if self.just_installed or self.simulated_switch:
            log.info("  Just installed from winget, so it is the newest version winget offers.")
            return
        latest = self.latest_spotify()
        installed = self.active.version
        log.info("  Installed: %s   winget offers: %s", version_text(installed), version_text(latest))
        diff = compare_versions(installed, latest)
        if diff is None:
            log.info("  Could not compare versions; leaving Spotify as it is.")
            return
        if diff >= 0:
            log.info("  Up to date.")
            return
        if self.admin:
            self.warn("Spotify is out of date, but its installer refuses to run as administrator. "
                      "Run this tool from a normal window to update it.")
            return
        self.close_spotify()
        log.info("  Updating Spotify with winget...")
        res = self.runner.run(self.winget_cmd("upgrade"), changes=True, timeout=TIMEOUT_WINGET_INSTALL,
                              label=f"winget upgrade --id {WINGET_ID} -e --silent ...")
        if not winget_ok(res.code) and not self.check:
            # winget may not know a Spotify installed by hand; "install" updates it in place.
            res = self.runner.run(self.winget_cmd("install"), changes=True, timeout=TIMEOUT_WINGET_INSTALL,
                                  label=f"winget install --id {WINGET_ID} -e --silent ...")
        if self.check:
            return
        self.desktop = detect_desktop(self.paths) or self.desktop
        self.active = self.desktop
        if not winget_ok(res.code) or compare_versions(self.desktop.version, latest) == -1:
            self.warn("The winget update did not work; the download page is open so you can update by hand.")
            self.open_download_page()
        else:
            log.info("  Updated to %s.", version_text(self.desktop.version))

    # ---------- step 4 ----------

    def latest_spicetify(self) -> tuple[int, ...] | None:
        """Read the newest Spicetify version from GitHub (read-only, 15 s limit)."""
        try:
            req = urllib.request.Request(SPICETIFY_LATEST_API, headers={"User-Agent": "spicetify-fixer",
                                                                        "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return parse_version(json.loads(resp.read().decode("utf-8")).get("tag_name", ""))
        except (OSError, ValueError):
            return None

    def latest_marketplace(self) -> tuple[int, ...] | None:
        """Read the newest Marketplace version from GitHub (read-only, 15 s limit)."""
        try:
            req = urllib.request.Request(MARKETPLACE_LATEST_API, headers={"User-Agent": "spicetify-fixer",
                                                                          "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return parse_version(json.loads(resp.read().decode("utf-8")).get("tag_name", ""))
        except (OSError, ValueError):
            return None

    def find_spicetify(self) -> str:
        if os.path.isfile(self.paths.spicetify_exe):
            return self.paths.spicetify_exe
        return shutil.which("spicetify") or ""

    def read_spicetify_version(self) -> str:
        res = self.run_spicetify("-v", changes=False, timeout=TIMEOUT_QUICK)
        match = VERSION_RE.search(res.out)
        return match.group(0) if res.ok and match else ""

    def setup_spicetify(self) -> None:
        self.step(4, "Spicetify installed and up to date")
        self.spicetify = self.find_spicetify()
        if not self.spicetify:
            log.info("  Spicetify is missing: running the official installer (%s).", SPICETIFY_INSTALLER)
            log.info("  Its questions are answered for you (Marketplace: yes).")
            res = self.runner.run(powershell(f"iwr -useb {SPICETIFY_INSTALLER} | iex"), changes=True,
                                  timeout=TIMEOUT_INSTALLER, input_text=installer_answers(self.admin),
                                  env=self.child_env(), label=f"iwr -useb {SPICETIFY_INSTALLER} | iex")
            if self.check:
                self.spicetify = self.paths.spicetify_exe
                return
            self.spicetify = self.find_spicetify()
            if not res.ok or not self.spicetify:
                raise FixerError("The Spicetify installer failed (details in the log).\n"
                                 f"Try it by hand in PowerShell: iwr -useb {SPICETIFY_INSTALLER} | iex")
        self.spicetify_version = self.read_spicetify_version()
        log.info("  Spicetify %s at %s", self.spicetify_version or "(version unknown)", self.spicetify)
        if self.args.skip_spicetify_update:
            log.info("  Update skipped (--skip-spicetify-update).")
            return
        latest = self.latest_spicetify()
        if latest and compare_versions(parse_version(self.spicetify_version), latest) in (0, 1):
            log.info("  Up to date (newest is %s).", ".".join(map(str, latest[:3])))
            return
        log.info("  Newest Spicetify is %s: upgrading.", ".".join(map(str, latest[:3])) if latest else
                 "unknown (GitHub did not answer)")
        self.close_spotify()
        res = self.run_spicetify("upgrade", "-n", timeout=TIMEOUT_SPICETIFY)
        if not res.ok and not self.check:
            # "upgrade" also tries to re-patch on its own and can exit 1 when that part fails; step 6
            # does the patching properly, so this is only a warning.
            log.info("  spicetify upgrade reported a problem (see log); step 6 repairs the patch.")
        if not self.check:
            self.spicetify_version = self.read_spicetify_version() or self.spicetify_version
            log.info("  Spicetify is now %s.", self.spicetify_version)

    # ---------- step 5 ----------

    def load_config(self) -> configparser.ConfigParser | None:
        if not self.config_path:
            res = self.run_spicetify("-c", changes=False, timeout=TIMEOUT_QUICK)
            path = res.out.strip().splitlines()[-1].strip() if res.ok and res.out.strip() else ""
            self.config_path = path if path and os.path.isfile(path) else self.paths.default_config
        return read_config(self.config_path)

    def fix_config(self) -> None:
        self.step(5, "Spicetify config paths")
        if not self.active:
            return
        cfg = self.load_config()
        if cfg is None:
            if self.check:
                log.info("  [check] No config yet; Spicetify creates it on its first run.")
                return
            # A first run of Spicetify creates the config with detected paths.
            self.run_spicetify("config", changes=False, timeout=TIMEOUT_QUICK)
            cfg = self.load_config()
        log.info("  Config: %s", self.config_path)
        log.info("  spotify_path = %s", cfg_get(cfg, "Setting", "spotify_path") or "(empty)")
        log.info("  prefs_path   = %s", cfg_get(cfg, "Setting", "prefs_path") or "(empty)")
        target = self.active
        reasons = config_fix_needed(cfg, target)
        if self.simulated_switch:
            reasons = reasons or ["the Spotify in use changes to the desktop build"]
        if not reasons:
            log.info("  Paths are correct.")
            return
        for reason in reasons:
            log.info("  Needs fixing: %s", reason)
        log.info("  Setting them to: %s  |  %s", target.folder, target.prefs)
        res = self.run_spicetify(*config_fix_command(target), timeout=TIMEOUT_QUICK)
        if not res.ok:
            raise FixerError("Could not update Spicetify's config paths (details in the log).")
        self.paths_changed = True

    # ---------- step 6 ----------

    def store_patch_state(self, store: SpotifyInstall) -> PatchState:
        """--check only: the patch state the Store build would have if it is kept."""
        saved = self.active, self.paths_changed
        self.active, self.paths_changed = store, False
        try:
            return self.patch_state()
        finally:
            self.active, self.paths_changed = saved

    def patch_state(self) -> PatchState:
        cfg = self.load_config()
        target = self.active
        source = apps_state(os.path.join(target.folder, "Apps"))
        if target.kind == "store":
            dest = apps_state(os.path.join(self.paths.spicetify_userdata, "AppX"))
            current = target.version
        else:
            dest = source
            current = parse_version(read_prefs_version(target.prefs)) or target.version
        backup = backup_state(os.path.join(self.paths.spicetify_userdata, "Backup"),
                              cfg_get(cfg, "Backup", "version"), current)
        if self.paths_changed and backup != "empty":
            backup = "outdated"  # that backup belongs to the other Spotify build
        return PatchState(source=source, dest=dest, backup=backup,
                          backup_with=cfg_get(cfg, "Backup", "with"),
                          spicetify_version=self.spicetify_version, is_store=target.kind == "store")

    def run_plan(self, plan: tuple[str, ...]) -> bool:
        self.close_spotify()
        res = self.run_spicetify(*plan, "-n")
        if self.check:
            return True
        if not res.ok:
            tail = [l for l in res.out.splitlines() if l.strip()][-3:]
            log.info("  spicetify %s failed%s", " ".join(plan), (": " + " / ".join(tail)) if tail else "")
            return False
        if self.patch_state().dest != "applied":
            log.info("  spicetify %s finished, but Spotify's files still look unpatched.", " ".join(plan))
            return False
        return True

    def patch(self) -> None:
        self.step(6, "Patch Spotify with Spicetify")
        if self.admin:
            self.warn("Running as administrator: Spicetify is run with --bypass-admin. This is NOT "
                      "recommended (Spotify can show a black window). Use a normal window next time.")
        if self.simulated_switch:
            log.info("  The new desktop Spotify starts unpatched, so the plan would be: spicetify backup apply")
            log.info("  [check] WOULD first start the new Spotify once so it writes its prefs file, then close it")
            self.run_spicetify(*BACKUP_APPLY, "-n")
            if self.original_store:
                kept = self.store_patch_state(self.original_store)
                log.info("  (If you answer No and keep the Store version, the plan would be: spicetify %s)",
                         " ".join(decide_patch(kept)))
            self.block_updates()
            return
        if self.active.kind == "desktop":
            self.ensure_desktop_prefs()
        st = self.patch_state()
        log.info("  Spotify files: %s | patched copy: %s | backup: %s (made with Spicetify %s, now %s)",
                 st.source, st.dest, st.backup, st.backup_with or "-", st.spicetify_version or "?")
        plan = decide_patch(st)
        if plan == REINSTALL:
            self.reinstall_spotify(st)
            plan = BACKUP_APPLY
            st = self.patch_state() if not self.check else st
        log.info("  Plan: spicetify %s", " ".join(plan))
        if not self.run_plan(plan):
            st = self.patch_state()
            second = retry_plan(plan, st)
            if not second:
                raise FixerError("Spicetify could not patch Spotify (details in the log).")
            log.info("  Retrying once with: spicetify %s", " ".join(second))
            if not self.run_plan(second):
                raise FixerError("Spicetify could not patch Spotify, even after a retry (details in the log).")
        log.info("  Spotify is patched.")
        self.block_updates()

    def reinstall_spotify(self, st: PatchState) -> None:
        log.info("  Spotify's files are %s and there is no backup that matches them.", st.source)
        if self.active.kind == "store":
            raise FixerError("The Store Spotify is damaged and Spicetify cannot repair it.\n"
                             "Run this tool with --switch-to-desktop (recommended), or repair Spotify in "
                             "Settings > Apps > Spotify > Advanced options > Repair.")
        self.require_installable()
        self.close_spotify()
        self.install_desktop(reason="Reinstalling the desktop Spotify so it is back to stock...", force=True)

    def block_updates(self) -> None:
        if not self.args.block_spotify_updates:
            return
        if self.active.kind != "desktop":
            log.info("  --block-spotify-updates only works with the desktop Spotify; skipped.")
            return
        log.info("  Blocking Spotify's own updates (this tool still updates it through winget).")
        if not self.run_spicetify("spotify-updates", "block").ok and not self.check:
            self.warn("Could not block Spotify updates (details in the log).")

    # ---------- step 7 ----------

    def marketplace(self) -> None:
        self.step(7, "Marketplace")
        cfg = self.load_config()
        apps = [a.strip() for a in cfg_get(cfg, "AdditionalOptions", "custom_apps").split("|") if a.strip()]
        folder = os.path.join(self.paths.spicetify_userdata, "CustomApps", "marketplace")
        enabled = "marketplace" in apps
        present = os.path.isfile(os.path.join(folder, "index.js"))
        log.info("  custom_apps = %s | folder %s: %s", "|".join(apps) or "(empty)", folder,
                 "present" if present else "missing")
        if enabled and present:
            have = marketplace_version(folder)
            if self.args.skip_marketplace_update:
                log.info("  Marketplace %s is installed and enabled (update skipped: --skip-marketplace-update).",
                         "v" + version_text(have[:3]) if have else "")
                return
            newest = self.latest_marketplace()
            if newest is None or have is None or compare_versions(have, newest) >= 0:
                if newest is None:
                    log.info("  Marketplace %s is installed and enabled (couldn't reach GitHub to look for a newer one).",
                             "v" + version_text(have[:3]) if have else "")
                elif have is None:
                    log.info("  Marketplace is installed and enabled (its version isn't written in its files; "
                             "newest is v%s).", version_text(newest[:3]))
                else:
                    log.info("  Marketplace v%s is installed and enabled. Up to date (newest is v%s).",
                             version_text(have[:3]), version_text(newest[:3]))
                return
            log.info("  Marketplace v%s is older than v%s: updating it with its official installer.",
                     version_text(have[:3]), version_text(newest[:3]))
        else:
            log.info("  Installing the Marketplace with its official installer (%s).", MARKETPLACE_INSTALLER)
        bypass = " -BypassAdmin" if self.admin else ""
        script = f"& ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing '{MARKETPLACE_INSTALLER}').Content)){bypass}"
        self.close_spotify()
        res = self.runner.run(powershell(script), changes=True, timeout=TIMEOUT_INSTALLER,
                              input_text=installer_answers(False), env=self.child_env(),
                              label=f"iwr -useb {MARKETPLACE_INSTALLER} | iex")
        if self.check:
            log.info("  [check] WOULD then run: spicetify apply -n")
            return
        if not res.ok or not os.path.isfile(os.path.join(folder, "index.js")):
            raise FixerError("The Marketplace installer failed (details in the log).\n"
                             f"Try it by hand in PowerShell: iwr -useb {MARKETPLACE_INSTALLER} | iex")
        # The installer runs backup/apply itself; re-check and apply once more without a restart.
        plan = decide_patch(self.patch_state())
        if plan == REINSTALL or not self.run_plan(plan):
            raise FixerError("The Marketplace is installed but applying it failed (details in the log).")
        now = marketplace_version(folder)
        log.info("  Marketplace %sinstalled and applied.", f"v{version_text(now[:3])} " if now else "")

    # ---------- step 8 ----------

    def launch(self) -> None:
        self.step(8, "Open Spotify")
        if self.args.no_launch:
            log.info("  Skipped (--no-launch).")
            return
        cfg = self.load_config()
        flags = [f for f in cfg_get(cfg, "Setting", "spotify_launch_flags").split("|") if f.strip()]
        target = self.active
        if target.kind == "desktop":
            cmd = [target.exe, *flags]
        elif os.path.isfile(self.paths.store_alias_exe) or self.check:
            # Same as Spicetify: the Store build only loads the patch with --app-directory.
            cmd = [self.paths.store_alias_exe,
                   f"--app-directory={os.path.join(self.paths.spicetify_userdata, 'AppX')}", *flags]
        else:
            cmd = ["explorer.exe", f"shell:AppsFolder\\{target.family_name}!{STORE_APP_ID}"]
        if self.check:
            log.info("  [check] WOULD start: %s", " ".join(cmd))
            return
        log.info("  Starting %s", target.label)
        self.start_process(cmd)

    # ---------- shortcut ----------

    def make_shortcut(self, only_refresh: bool = False) -> None:
        """Desktop + Start Menu shortcuts that run this script silently with pythonw.
        only_refresh=True (after the switch to desktop) re-saves existing shortcuts so their icon
        points at the new Spotify.exe, and creates nothing new."""
        log.info("== %s", "Refresh shortcut icons" if only_refresh else "Make shortcuts")
        if not only_refresh and not self.store and not self.desktop:
            self.store = detect_store(self.runner)
        script = os.path.abspath(__file__)
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        target = pythonw if os.path.isfile(pythonw) else (shutil.which("pyw") or shutil.which("py") or sys.executable)
        icon_exe = ""
        for candidate in (self.paths.desktop_exe, self.store.exe if self.store else ""):
            if candidate and os.path.isfile(candidate):
                icon_exe = candidate
                break
        env = self.child_env()
        env.update({"SF_TARGET": target, "SF_ARGS": f'"{script}"', "SF_DIR": os.path.dirname(script),
                    "SF_ICON": f"{icon_exe},0" if icon_exe else f"{target},0", "SF_NAME": SHORTCUT_NAME,
                    "SF_ONLY_REFRESH": "1" if only_refresh else ""})
        # Values travel in environment variables, so no path ever needs PowerShell quoting.
        ps = ("$s = New-Object -ComObject WScript.Shell; "
              "foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) { "
              "$p = Join-Path $d ($env:SF_NAME + '.lnk'); "
              "if ($env:SF_ONLY_REFRESH -and -not (Test-Path -LiteralPath $p)) { continue }; "
              "$l = $s.CreateShortcut($p); "
              "$l.TargetPath = $env:SF_TARGET; $l.Arguments = $env:SF_ARGS; $l.WorkingDirectory = $env:SF_DIR; "
              "$l.IconLocation = $env:SF_ICON; $l.Description = 'Fix Spicetify, then open Spotify'; $l.Save(); "
              "Write-Output ('created ' + $l.FullName) }")
        log.info("  Target: %s %s", target, script)
        log.info("  Icon:   %s", env["SF_ICON"])
        res = self.runner.run(powershell(ps), changes=True, timeout=TIMEOUT_QUICK, env=env,
                              label=f"{'refresh' if only_refresh else 'create'} '{SHORTCUT_NAME}' shortcuts "
                                    "(Desktop + Start Menu)")
        if not res.ok:
            if only_refresh:
                self.warn("Could not refresh the shortcut icons; run --make-shortcut again.")
                return
            raise FixerError("Could not create the shortcuts (details in the log).")
        for line in res.out.splitlines():
            if line.startswith("created "):
                log.info("  %s", line)

    # ---------- main flow ----------

    def run(self) -> None:
        if self.admin:
            self.warn("This runs as administrator. Spotify's installer refuses admin, and Spicetify "
                      "advises against it. A normal window is better.")
        self.find_spotify()
        self.store_switch()
        self.update_spotify()
        self.setup_spicetify()
        self.fix_config()
        self.patch()
        self.marketplace()
        self.launch()


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="spicetify_fixer.py",
        description="Get Spotify + Spicetify (+ Marketplace) working, then open Spotify. No prompts.")
    p.add_argument("--check", action="store_true", help="dry run: report what it would do, change nothing")
    p.add_argument("--make-shortcut", action="store_true",
                   help=f'create "{SHORTCUT_NAME}" shortcuts on the Desktop and in the Start Menu, then exit')
    p.add_argument("--switch-to-desktop", action="store_true",
                   help="switch from the Store Spotify to the desktop one without asking")
    p.add_argument("--never-switch", action="store_true", help="keep the Store Spotify and stop asking")
    p.add_argument("--skip-spotify-update", action="store_true", help="do not update Spotify")
    p.add_argument("--skip-spicetify-update", action="store_true", help="do not upgrade Spicetify")
    p.add_argument("--block-spotify-updates", action="store_true",
                   help="stop Spotify updating itself (spicetify spotify-updates block); this tool still updates it")
    p.add_argument("--no-launch", action="store_true", help="do not open Spotify at the end")
    p.add_argument("--no-pause", action="store_true", help="on failure, do not keep the window open")
    p.add_argument("--verbose", action="store_true", help="show every command's output in the console")
    p.add_argument("--skip-marketplace-update", action="store_true", help="don't update the Marketplace")
    p.add_argument("--no-color", action="store_true", help="plain text in the console (no green / yellow / red)")
    p.add_argument("--version", action="version", version=f"spicetify-fixer {__version__}")
    return p


def setup_logging(paths: Paths, verbose: bool, color: bool = True) -> str:
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        use_color = color and enable_colors(sys.stdout)
        console.setFormatter(ColorFormatter("%(message)s") if use_color else logging.Formatter("%(message)s"))
        log.addHandler(console)
    try:
        os.makedirs(paths.fixer_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(paths.log_file, maxBytes=512_000, backupCount=3,
                                                       encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(handler)
    except OSError:
        pass
    return paths.log_file


def finish_with_error(message: str, log_path: str, no_pause: bool) -> None:
    log.error("")
    log.error("FAILED: %s", message)
    log.error("Full log: %s", log_path)
    if no_pause:
        return
    if has_console():
        try:
            input("\nPress Enter to close this window...")
        except EOFError:
            pass
    else:
        message_box(f"{message}\n\nFull log:\n{log_path}", error=True)


def main(argv: list[str] | None = None, *, paths: Paths | None = None, runner: Runner | None = None,
         admin: bool | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.switch_to_desktop and args.never_switch:
        build_parser().error("--switch-to-desktop and --never-switch cannot be used together")
    paths = paths or Paths.from_env()
    log_path = setup_logging(paths, args.verbose, color=not args.no_color)
    runner = runner or Runner(check=args.check)
    fixer = Fixer(args=args, paths=paths, runner=runner,
                  admin=is_admin() if admin is None else admin, state=load_state(paths.state_file))
    log.info("spicetify-fixer %s  (%s)%s", __version__, datetime.now().strftime("%Y-%m-%d %H:%M"),
             "  --check: dry run, nothing will be changed" if args.check else "")
    try:
        if os.name != "nt" and not args.check:
            raise FixerError("This tool is for Windows.")
        if args.make_shortcut:
            fixer.make_shortcut()
        else:
            fixer.run()
    except FixerError as exc:
        finish_with_error(str(exc), log_path, args.no_pause)
        return 1
    except Exception as exc:  # anything unexpected still gets a readable message and the log
        log.debug("unexpected error", exc_info=True)
        finish_with_error(f"Unexpected error: {exc}", log_path, args.no_pause)
        return 1
    log.info("")
    if fixer.warnings:
        log.info("Done, with %s warning(s):", len(fixer.warnings))
        for text in fixer.warnings:
            log.info("  - %s", text)
    else:
        log.info("Done." + ("  (dry run: nothing was changed)" if args.check else ""))
    log.info("Log: %s", log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
