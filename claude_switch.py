#!/usr/bin/env python3
"""claude-switch: switch the account Claude Code uses by swapping its saved login, keeping one shared history.

Claude Code keeps one login per profile: ~/.claude/.credentials.json on Windows and Linux, the macOS Keychain item
"Claude Code-credentials" on macOS, plus the `oauthAccount` entry of ~/.claude.json. claude-switch saves a copy of
each account's login in ~/.claude-switch/ and swaps it in on `use`. Sessions, history and settings stay shared.

Claude Code refresh tokens are single-use: every renewal writes a new pair and the old refresh token stops working.
A saved copy therefore dies as soon as anything renews the same login after the copy was taken. To stay correct,
claude-switch saves the active account's newest login on every command, holds Claude Code's own locks while it
swaps, refuses to file one account's login under another name, keeps the last 3 copies of each account, and marks
a saved login dead when the server answers invalid_grant.

Opt-in only: `refresh`, `keepalive` and `auto --refresh` renew the saved logins of accounts you are not using, with
the same request Claude Code sends (POST https://platform.claude.com/v1/oauth/token). Nothing runs on a schedule
unless you set that up yourself. Usage readings come from GET https://api.anthropic.com/api/oauth/usage, the
read-only endpoint behind Claude Code's /usage screen.

No token is ever printed or logged; logins are compared by a SHA-256 hash of the refresh token.

Testing: set CLAUDE_SWITCH_HOME to a fake home folder. The script then never touches the real profile, and only
then may CLAUDE_SWITCH_TOKEN_URL, CLAUDE_SWITCH_USAGE_URL, CLAUDE_SWITCH_BACKEND (file | keychain) and
CLAUDE_SWITCH_SECURITY (a stand-in for /usr/bin/security) point at fakes.
"""
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

__version__ = "0.1.0"
PROG = "claude-switch"

TEST_HOME = os.environ.get("CLAUDE_SWITCH_HOME", "")
HOME = TEST_HOME or os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
CREDS = os.path.join(CLAUDE_DIR, ".credentials.json")
CLAUDE_LOCK = os.path.join(CLAUDE_DIR, ".oauth_refresh.lock")  # Claude Code's own refresh lock (a directory)
STATE = os.path.join(HOME, ".claude.json")
STATE_LOCK = STATE + ".lock"  # Claude Code's lock for .claude.json (a directory)
STORE = os.path.join(HOME, ".claude-switch")
ACCOUNTS = os.path.join(STORE, "accounts.json")
ACTIVE = os.path.join(STORE, "active.txt")
HISTORY = os.path.join(STORE, "history")
REMOVED = os.path.join(STORE, "removed")
STORE_LOCK = os.path.join(STORE, ".lock")
USAGE_CACHE = os.path.join(STORE, "usage-cache.json")
META = os.path.join(STORE, "meta.json")
LOG = os.path.join(STORE, "keepalive.log")

KEEP = 3                    # copies of each account kept in history/
NEAR = 90.0                 # warn at this usage percentage
CACHE_SECONDS = 120         # usage readings are reused for this long
REFRESH_UNDER_HOURS = 3.0   # `refresh` renews a saved login when its access token has less left than this
ACCESS_TOKEN_HOURS = 8      # Claude Code's access token lifetime
END_WARN_DAYS = 3           # warn this many days before a login ends
AUTO_MARGIN = 5.0           # `auto` switches only when another account is this many points better

TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
if TEST_HOME:
    TOKEN_URL = os.environ.get("CLAUDE_SWITCH_TOKEN_URL", TOKEN_URL)
    USAGE_URL = os.environ.get("CLAUDE_SWITCH_USAGE_URL", USAGE_URL)
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code's public OAuth client id
DEFAULT_SCOPES = ["user:profile", "user:inference", "user:sessions:claude_code", "user:mcp_servers",
                  "user:file_upload"]
KEYCHAIN_SERVICE = "Claude Code-credentials"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

ACCTS = {}  # name -> email ("" until the first save), loaded from accounts.json


class Fail(SystemExit):
    """A clear, expected failure: the message goes to stderr and the exit code is 1."""


# ---------- files ----------

def read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def read_json(path, default=None, strict=False):
    """Parsed JSON. A missing file gives `default`. A broken file gives `default`, or stops when `strict`."""
    text = read_text(path)
    if text is None:
        return {} if default is None else default
    try:
        return json.loads(text)
    except ValueError:
        if strict:
            raise Fail(f"{path} is not valid JSON; fix or restore it, then try again")
        return {} if default is None else default


def write_text_atomic(path, text, private=True):
    """Write through a temp file and rename. Private files get mode 0600; others keep the old file's mode."""
    tmp = path + ".switch-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    if not private and os.path.exists(path):
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
    os.replace(tmp, path)


def write_json_atomic(path, data, private=True):
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", private)


def private_copy(src, dst):
    write_text_atomic(dst, read_text(src) or "")


def make_private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def slot(name, kind):  # kind: credentials | account
    return os.path.join(STORE, f"{name}.{kind}.json")


def saved(name):
    return os.path.exists(slot(name, "credentials"))


# ---------- accounts config ----------

def load_accounts():
    data = read_json(ACCOUNTS, {}, strict=True)
    if not isinstance(data, dict):
        raise Fail(f"{ACCOUNTS} must hold an object like {{\"work\": \"me@example.com\"}}")
    ACCTS.clear()
    ACCTS.update({str(k): str(v or "") for k, v in data.items()})


def save_accounts():
    write_json_atomic(ACCOUNTS, ACCTS)


def email_of(name):
    return ACCTS.get(name, "")


def same_email(a, b):
    return bool(a) and bool(b) and a.strip().lower() == b.strip().lower()


def name_for_email(email):
    for n, e in ACCTS.items():
        if same_email(e, email):
            return n
    return ""


def check_name(name):
    if not name:
        raise Fail(f"give an account name, e.g. {PROG} save work")
    if not NAME_RE.match(name):
        raise Fail(f"bad account name {name!r}: use 1-32 letters, digits, dot, dash or underscore, starting with "
                   f"a letter or digit")


def known(name):
    if name not in ACCTS:
        listed = " ".join(ACCTS) or "none yet"
        raise Fail(f"unknown account {name!r}; known: {listed}")


# ---------- where Claude Code keeps the live login ----------

class FileStore:
    """~/.claude/.credentials.json (Windows, Linux, and Claude Code's fallback on macOS)."""
    label = "credentials file"

    def read(self, fresh=False):
        return read_text(CREDS)

    def write(self, text):
        os.makedirs(CLAUDE_DIR, exist_ok=True)
        write_text_atomic(CREDS, text)

    def remove(self):
        if os.path.exists(CREDS):
            os.remove(CREDS)


class KeychainStore:
    """The macOS login Keychain item Claude Code uses, through Apple's `security` tool.

    Claude Code reads the Keychain item first and falls back to ~/.claude/.credentials.json when no item exists.
    Writes go to the Keychain and are read back to confirm; an existing credentials file is rewritten too, so
    both places hold the same login.
    """
    label = "macOS Keychain"
    NOT_FOUND = 44
    STDIN_LIMIT = 4096 - 64  # `security -i` reads stdin lines into a 4096-byte buffer

    def __init__(self):
        self.exe = "/usr/bin/security"
        if TEST_HOME and os.environ.get("CLAUDE_SWITCH_SECURITY"):
            self.exe = os.environ["CLAUDE_SWITCH_SECURITY"]
        self.account = os.environ.get("USER") or self._os_user()
        self._memo = None

    @staticmethod
    def _os_user():
        try:
            import pwd
            return pwd.getpwuid(os.geteuid()).pw_name
        except (ImportError, KeyError, AttributeError):
            return "claude-code-user"

    def _run(self, args, stdin=None):
        cmd = ([sys.executable, self.exe] if self.exe.endswith(".py") else [self.exe]) + args
        try:
            return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            raise Fail("the macOS Keychain did not answer within 10 s. Is it locked (for example over SSH)? "
                       "Run `security unlock-keychain` and try again.")
        except OSError as exc:
            raise Fail(f"cannot run {self.exe}: {exc}")

    def _base(self):
        return ["-a", self.account, "-s", KEYCHAIN_SERVICE]

    def read(self, fresh=False):
        if self._memo is not None and not fresh:
            return self._memo
        p = self._run(["find-generic-password", "-a", self.account, "-w", "-s", KEYCHAIN_SERVICE])
        if p.returncode == 0:
            text = p.stdout[:-1] if p.stdout.endswith("\n") else p.stdout
            if text and text[0] != "{" and re.fullmatch(r"[0-9a-fA-F]+", text):
                try:  # `security -w` prints data with non-printable bytes as hex
                    text = bytes.fromhex(text).decode("utf-8")
                except ValueError:
                    pass
        elif p.returncode == self.NOT_FOUND:
            text = read_text(CREDS)  # Claude Code's own fallback
        else:
            raise Fail(f"could not read the login from the macOS Keychain (security exit {p.returncode}: "
                       f"{p.stderr.strip()[:120]}). Unlock the login keychain and try again.")
        self._memo = text
        return text

    def write(self, text):
        self._memo = None
        hexed = text.encode("utf-8").hex()
        line = (f'add-generic-password -U -a "{self._q(self.account)}" -s "{self._q(KEYCHAIN_SERVICE)}" '
                f'-X {hexed}\n')
        if len(line.encode("utf-8")) <= self.STDIN_LIMIT:
            p = self._run(["-i"], stdin=line)  # keeps the secret out of the process list
        else:
            p = self._run(["add-generic-password", "-U"] + self._base() + ["-X", hexed])
        back = self.read(fresh=True)
        if p.returncode != 0 or fingerprint(parse(back)) != fingerprint(parse(text)):
            raise Fail(f"writing the macOS Keychain item {KEYCHAIN_SERVICE!r} failed (security exit "
                       f"{p.returncode}); the saved copies in {STORE} are unchanged")
        if os.path.exists(CREDS):
            write_text_atomic(CREDS, text)
        self._memo = text

    @staticmethod
    def _q(value):
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def remove(self):
        self._memo = None
        p = self._run(["delete-generic-password"] + self._base())
        if p.returncode not in (0, self.NOT_FOUND):
            raise Fail(f"could not delete the macOS Keychain item (security exit {p.returncode})")
        if os.path.exists(CREDS):
            os.remove(CREDS)


def pick_store():
    choice = os.environ.get("CLAUDE_SWITCH_BACKEND", "") if TEST_HOME else ""
    if choice == "keychain" or (not choice and sys.platform == "darwin"):
        return KeychainStore()
    return FileStore()


LIVE = None  # set in main()


# ---------- token facts (never the token itself) ----------

def parse(text):
    try:
        d = json.loads(text) if text else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def oauth(data):
    o = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return o if isinstance(o, dict) else {}


def fingerprint(data):
    """Hash of the refresh token, used only to compare two logins. Empty when there is no login."""
    tok = oauth(data).get("refreshToken") or ""
    return hashlib.sha256(tok.encode()).hexdigest() if tok else ""


def live_data(fresh=False):
    return parse(LIVE.read(fresh))


def slot_data(name):
    return parse(read_text(slot(name, "credentials")))


def when(ms, fmt="%m-%d %H:%M"):
    if not ms:
        return "?"
    return datetime.datetime.fromtimestamp(ms / 1000).strftime(fmt)


def current_email():
    acct = read_json(STATE, {}).get("oauthAccount") if os.path.exists(STATE) else None
    return (acct or {}).get("emailAddress", "") if isinstance(acct, dict) else ""


def active():
    return (read_text(ACTIVE) or "").strip()


def set_active(name):
    write_text_atomic(ACTIVE, name)


def meta():
    return read_json(META, {})


def set_meta(name, **kw):
    m = meta()
    m.setdefault(name, {}).update(kw)
    write_json_atomic(META, m)


def refreshed_at(name, data):
    """When this login was last renewed: our own record, else expiresAt minus the access token lifetime."""
    rec = meta().get(name, {})
    if rec.get("refreshed_at") and rec.get("fp") == fingerprint(data)[:16]:
        return rec["refreshed_at"] * 1000
    exp = oauth(data).get("expiresAt")
    return exp - ACCESS_TOKEN_HOURS * 3600 * 1000 if exp else 0


def owner_of(data, skip=""):
    """The saved name whose stored login is this exact login (same refresh token), if any."""
    fp = fingerprint(data)
    if not fp:
        return ""
    for name in ACCTS:
        if name != skip and saved(name) and fingerprint(slot_data(name)) == fp:
            return name
    return ""


def live_name():
    """The account the live login belongs to, by the email in ~/.claude.json."""
    return name_for_email(current_email())


def is_live(name):
    """Whether `name` is the login Claude Code uses now. Nothing is live while no login is present."""
    fp = fingerprint(live_data())
    if not fp:
        return False
    return live_name() == name or (saved(name) and fp == fingerprint(slot_data(name)))


def plan_of(name):
    acct = read_json(slot(name, "account"), {})
    if isinstance(acct, dict) and acct.get("planDisplayName"):
        return acct["planDisplayName"]
    o = oauth(live_data() if is_live(name) else slot_data(name))
    sub, tier = o.get("subscriptionType") or "", o.get("rateLimitTier") or ""
    for mult in ("20x", "5x"):
        if mult in tier:
            return f"{sub.capitalize() or 'Max'} {mult}"
    return sub.capitalize() or "?"


# ---------- locks ----------

class DirLock:
    """mkdir lock, the kind Claude Code uses (proper-lockfile): a directory older than `stale` seconds is dead."""

    def __init__(self, path, stale, wait=30.0):
        self.path, self.stale, self.wait, self.held = path, stale, wait, False

    def __enter__(self):
        end = time.time() + self.wait
        while True:
            try:
                os.mkdir(self.path)
                self.held = True
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.rmdir(self.path)
                        continue
                except OSError:
                    continue
            if time.time() > end:
                raise Fail(f"busy: {self.path} is held by another process; try again in a minute")
            time.sleep(0.25)

    def __exit__(self, *exc):
        if self.held:
            try:
                os.rmdir(self.path)
            except OSError:
                pass


def store_lock():
    return DirLock(STORE_LOCK, stale=120)


def claude_lock():
    os.makedirs(CLAUDE_DIR, exist_ok=True)
    return DirLock(CLAUDE_LOCK, stale=60)


# ---------- saving ----------

def keep_history(name):
    src = slot(name, "credentials")
    if not os.path.exists(src):
        return
    make_private_dir(HISTORY)
    shutil.copy2(src, os.path.join(HISTORY, f"{name}.{stamp()}.credentials.json"))
    old = sorted(glob.glob(os.path.join(HISTORY, f"{name}.*.credentials.json")))
    for f in old[:-KEEP]:
        os.remove(f)


def snapshot(name):
    """Store the live login under `name`. Refuses when the live login is another saved account's login."""
    text = LIVE.read(fresh=True)
    data = parse(text)
    if not fingerprint(data):
        return False
    other = owner_of(data, skip=name)
    if other:
        print(f"! the live login is {other}'s saved login, not {name}'s; {name} was not overwritten. "
              f"Run: {PROG} use {other}  (or {PROG} add {name} to log {name} in again)")
        return False
    if fingerprint(slot_data(name)) == fingerprint(data):
        return True  # already up to date
    keep_history(name)
    write_text_atomic(slot(name, "credentials"), text)
    write_json_atomic(slot(name, "account"), read_json(STATE, {}, strict=True).get("oauthAccount") or {})
    set_meta(name, dead="")
    return True


def owns_state(name):
    return bool(name) and name in ACCTS and same_email(current_email(), email_of(name))


def save_outgoing():
    """Save the active account's newest login if the live login still belongs to it."""
    cur = active()
    if owns_state(cur):
        with store_lock():
            return snapshot(cur)
    return False


def park_unknown(saved_out):
    """Keep a copy of a live login that belongs to no saved name, before it gets replaced."""
    text = LIVE.read(fresh=True)
    data = parse(text)
    if fingerprint(data) and not saved_out and not owner_of(data):
        parked = os.path.join(STORE, f"parked-{stamp()}.credentials.json")
        write_text_atomic(parked, text)
        print(f"  the current login matched no saved account; kept a copy at {parked}")


# ---------- renewal of inactive saved logins (opt-in) ----------

def post_json(url, body, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": PROG})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def refresh_one(name, force=False):
    """Renew one saved login that no running session uses. Returns a one-line result (no secrets)."""
    path = slot(name, "credentials")
    if not saved(name):
        return "not saved"
    if is_live(name):
        return "skipped: active login (running sessions renew it)"
    with store_lock():
        if is_live(name):  # again under the lock: a `use` may have made it live meanwhile
            return "skipped: active login (running sessions renew it)"
        full = slot_data(name)
        o = oauth(full)
        rt = o.get("refreshToken")
        if not rt:
            return "no refresh token in the saved copy"
        left_h = ((o.get("expiresAt") or 0) / 1000 - time.time()) / 3600
        if not force and left_h > REFRESH_UNDER_HOURS:
            return f"fresh, access token good for {left_h:.1f} h more"
        body = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": o.get("clientId") or CLIENT_ID,
                "scope": " ".join(o.get("scopes") or DEFAULT_SCOPES)}
        try:
            data = post_json(TOKEN_URL, body)
        except urllib.error.HTTPError as exc:
            try:
                code = json.loads(exc.read()).get("error", "")
            except (ValueError, OSError, AttributeError):
                code = ""
            code = code if isinstance(code, str) else (code or {}).get("type", "")
            if exc.code in (400, 401) and code in ("invalid_grant", "invalid_request", ""):
                set_meta(name, dead=time.strftime("%Y-%m-%d %H:%M"))
                return (f"FAILED: HTTP {exc.code} {code}. This saved login is dead (something renewed it after it "
                        f"was saved). Run: {PROG} add {name}")
            return f"FAILED: HTTP {exc.code} {code}, saved copy unchanged; try later"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return f"FAILED: {type(exc).__name__}, saved copy unchanged; try later"
        if not isinstance(data, dict) or not data.get("access_token") or not data.get("expires_in"):
            return "FAILED: the reply had no access token or expiry, saved copy unchanged"
        now = time.time()
        new = dict(o)
        new["accessToken"] = data["access_token"]
        new["refreshToken"] = data.get("refresh_token") or rt  # Claude Code keeps the old one when none comes back
        new["expiresAt"] = int((now + float(data["expires_in"])) * 1000)
        if isinstance(data.get("refresh_token_expires_in"), (int, float)):
            new["refreshTokenExpiresAt"] = int((now + data["refresh_token_expires_in"]) * 1000)
        full["claudeAiOauth"] = new
        keep_history(name)
        text = json.dumps(full, separators=(",", ":"), ensure_ascii=False)
        try:
            write_text_atomic(path, text)
        except OSError as exc:  # the old pair is already spent: keep the new one somewhere, never lose it
            rescue = os.path.join(STORE, f"rescue-{name}-{stamp()}.credentials.json")
            write_text_atomic(rescue, text)
            return f"FAILED to write the saved copy ({exc}); the new login is in {rescue}: copy it over {path}"
        rotated = new["refreshToken"] != rt
        acct = data.get("account")
        got = (acct.get("email_address") or "") if isinstance(acct, dict) else ""
        set_meta(name, refreshed_at=int(now), fp=fingerprint(full)[:16], dead="")
    drop_usage_cache(name)
    who = f", ! the server says this login is {got}" if got and not same_email(got, email_of(name)) else ""
    return (f"renewed, access token until {when(new['expiresAt'])}{who}"
            f"{', refresh token rotated' if rotated else ', same refresh token'}"
            f", login ends {when(new.get('refreshTokenExpiresAt'))}")


# ---------- usage ----------

def drop_usage_cache(name):
    c = read_json(USAGE_CACHE, {})
    if c.pop(name, None) is not None:
        write_json_atomic(USAGE_CACHE, c)


def usage(name):
    """(reading, note). reading has five_hour / seven_day dicts, or None. note says how old or why unknown."""
    cache = read_json(USAGE_CACHE, {})
    hit = cache.get(name)
    if hit and time.time() - hit.get("at", 0) < CACHE_SECONDS:
        return hit["data"], ""
    live = is_live(name)
    o = oauth(live_data() if live else slot_data(name))
    stale = f" (last seen {int((time.time() - hit['at']) // 60)} min ago)" if hit else ""
    old = (hit or {}).get("data")
    if not o.get("accessToken"):
        return old, "no saved login" + stale
    if (o.get("expiresAt") or 0) / 1000 < time.time() + 30:
        why = "token expired, a running session renews it" if live else "unknown, refresh needed"
        return old, why + stale
    req = urllib.request.Request(USAGE_URL, headers={"Authorization": "Bearer " + o["accessToken"],
                                                     "anthropic-beta": "oauth-2025-04-20",
                                                     "User-Agent": PROG})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return old, f"usage HTTP {exc.code}" + stale
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return old, f"usage failed: {type(exc).__name__}" + stale
    if not isinstance(d, dict):
        return old, "usage reply not understood" + stale
    keep = {k: d.get(k) for k in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")}
    cache = read_json(USAGE_CACHE, {})
    cache[name] = {"at": time.time(), "data": keep}
    write_json_atomic(USAGE_CACHE, cache)
    return keep, ""


def pct(block):
    u = (block or {}).get("utilization") if isinstance(block, dict) else None
    return float(u) if isinstance(u, (int, float)) else None


def reset_at(block):
    s = (block or {}).get("resets_at") if isinstance(block, dict) else None
    if not s:
        return "?"
    try:
        s = re.sub(r"\.\d+", "", s.replace("Z", "+00:00"))  # Python < 3.11 rejects some fractions
        t = datetime.datetime.fromisoformat(s).astimezone()
    except ValueError:
        return "?"
    now = datetime.datetime.now().astimezone()
    return t.strftime("%H:%M") if t.date() == now.date() else t.strftime("%a %H:%M")


def near_limit(d):
    out = []
    for key, label in (("five_hour", "5-hour"), ("seven_day", "week"), ("seven_day_opus", "Opus week"),
                       ("seven_day_sonnet", "Sonnet week")):
        b = (d or {}).get(key) or {}
        if not isinstance(b, dict):
            continue
        if b.get("locked_reason"):
            out.append(f"{label} LOCKED")
        elif (pct(b) or 0) >= NEAR:
            out.append(f"{label} at {pct(b):g}%")
    return out


def bar(v, width):
    if v is None:
        return "?".ljust(width)
    full = max(0, min(width, int(round(v / 100 * width))))
    return "#" * full + "." * (width - full)


# ---------- environment checks ----------

def warn_running():
    """Claude Code sessions keep their login in memory: tell the user how many are open."""
    if TEST_HOME:
        return
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq claude.exe", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=10).stdout
            n = sum(1 for line in out.splitlines() if line.lower().startswith('"claude.exe"'))
        else:
            out = subprocess.run(["pgrep", "-x", "claude"], capture_output=True, text=True, timeout=10).stdout
            n = len(out.split())
    except (OSError, subprocess.SubprocessError):
        return
    if n:
        print(f"{n} claude processes running: after a switch, /exit and `claude -c` in each open session.")


def warn_token_override():
    """CLAUDE_CODE_OAUTH_TOKEN overrides every login. Warn; never edit the user's environment."""
    found = []
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        found.append("this shell")
    if os.name == "nt" and not TEST_HOME:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                winreg.QueryValueEx(key, "CLAUDE_CODE_OAUTH_TOKEN")
            found.append("your Windows user environment")
        except OSError:
            pass
    if found:
        print(f"! CLAUDE_CODE_OAUTH_TOKEN is set in {' and '.join(found)}. Claude Code uses it instead of any "
              f"saved login, so switching has no effect until you remove it.")


def check_profile():
    if os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        raise Fail(f"CLAUDE_CONFIG_DIR is set. {PROG} manages the default profile (~/.claude) only; unset it "
                   f"in this shell and try again.")


# ---------- commands ----------

def cmd_save(name):
    check_name(name)
    email = current_email()
    data = live_data(fresh=True)
    if not email or not fingerprint(data):
        raise Fail(f"no live login found in the {LIVE.label}. Run `claude auth login` first.")
    if email_of(name) and not same_email(email, email_of(name)):
        raise Fail(f"The login now is {email}, not {email_of(name)}. To log {name} in: {PROG} add {name}")
    other = name_for_email(email)
    if other and other != name:
        raise Fail(f"The login now is {email}, already saved as {other}. Run: {PROG} save {other}")
    with store_lock():
        if not snapshot(name):
            raise Fail(f"{name} was not saved (see the line above).")
        load_accounts()
        ACCTS[name] = email
        save_accounts()
    set_active(name)
    print(f"saved {name} ({email})")


def cmd_use(name):
    known(name)
    if not saved(name):
        raise Fail(f"{name} is not saved yet. Run: {PROG} add {name}")
    warn_token_override()
    cur = active()
    if cur == name and owns_state(name) and fingerprint(live_data()):
        save_outgoing()
        print(f"claude already uses {name} ({email_of(name)})")
        return
    # Hold Claude Code's refresh lock: no running session can renew (and write back) the outgoing login mid-swap.
    with claude_lock(), store_lock():
        saved_out = snapshot(cur) if owns_state(cur) else False
        park_unknown(saved_out)
        with DirLock(STATE_LOCK, stale=10):
            state = read_json(STATE, {}, strict=True)
            LIVE.write(read_text(slot(name, "credentials")))
            if os.path.exists(STATE):
                private_copy(STATE, os.path.join(STORE, "claude.json.bak"))
            state["oauthAccount"] = read_json(slot(name, "account"), {})
            write_json_atomic(STATE, state, private=False)
        set_active(name)
    print(f"claude now uses {name} ({current_email()}). Open sessions: /exit, then claude -c")
    if (oauth(live_data(fresh=True)).get("expiresAt") or 0) / 1000 < time.time():
        print(f"  its access token has expired; claude renews it on first use. If claude asks you to log in "
              f"again, the saved login was used elsewhere since; run: {PROG} add {name}")
    warn_running()


def cmd_add(name):
    check_name(name)
    warn_token_override()
    with claude_lock():
        park_unknown(save_outgoing())
        text = LIVE.read(fresh=True)
        if text:
            parked = os.path.join(STORE, f"parked-for-login-{stamp()}.credentials.json")
            write_text_atomic(parked, text)
            LIVE.remove()
    with store_lock():
        load_accounts()
        ACCTS.setdefault(name, "")
        save_accounts()
    set_active("")
    who = email_of(name) or "the account you want to save as " + name
    print(f"The current login is saved and moved aside. Now:\n"
          f"  1. sign the BROWSER into {who}\n"
          f"  2. claude auth login\n"
          f"  3. {PROG} save {name}\n"
          f"Open claude sessions show 'not logged in' until step 3; they pick the new login up after it.")


def cmd_remove(name):
    known(name)
    if is_live(name) and fingerprint(live_data()):
        raise Fail(f"{name} is the live login. Switch to another account first: {PROG} use OTHER")
    with store_lock():
        make_private_dir(REMOVED)
        for kind in ("credentials", "account"):
            if os.path.exists(slot(name, kind)):
                os.replace(slot(name, kind), os.path.join(REMOVED, f"{name}.{stamp()}.{kind}.json"))
        load_accounts()
        ACCTS.pop(name, None)
        save_accounts()
        m = meta()
        if m.pop(name, None) is not None:
            write_json_atomic(META, m)
        drop_usage_cache(name)
    if active() == name:
        set_active("")
    print(f"removed {name}; its saved files are in {REMOVED}")


def cmd_sync(quiet=False):
    cur = active()
    ok = save_outgoing()
    if not quiet:
        print(f"saved {cur}'s newest login" if ok else
              f"nothing saved: the active name is {cur or 'unset'} and the login is {current_email() or 'none'}")
    return ok


def cmd_refresh(names, force=False, quiet=False):
    names = names or [n for n in ACCTS if saved(n)]
    results = {n: refresh_one(n, force) for n in names}
    if not quiet:
        for n, r in results.items():
            print(f"{n}  {r}")
        print()
    return results


def cmd_keepalive():
    cmd_sync(quiet=True)
    results = cmd_refresh([], quiet=True)
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + ("; ".join(f"{n}: {r}" for n, r in results.items()) or "nothing saved")
    with open(LOG, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
    lines = (read_text(LOG) or "").splitlines()
    if len(lines) > 500:
        write_text_atomic(LOG, "\n".join(lines[-300:]) + "\n")
    return 1 if any(r.startswith("FAILED") for r in results.values()) else 0


def cmd_list():
    save_outgoing()
    has_login = bool(fingerprint(live_data()))
    live = live_name() if has_login else ""
    email = current_email() if has_login else ""
    print(f"CLAUDE ACCOUNTS   active: {live or '?'} ({email or 'no login'})" +
          ("" if not active() or active() == live else f"   ! active.txt says {active()}"))
    holder = owner_of(live_data())
    if holder and live and holder != live:
        print(f"! the login holds {holder}'s saved login but ~/.claude.json names {live}. Fix: {PROG} use {holder}")
    if not ACCTS:
        print()
        print(f"No accounts yet. Log in with `claude auth login`, then: {PROG} save NAME")
        return
    print()
    print("   name          email                     plan     weekly use (resets)         5-hour use (resets)    "
          "renewed      ends")
    print("   ------------  ------------------------  -------  --------------------------  ---------------------  "
          "-----------  -----")
    seen, warnings, notes, rooms, m = {}, [], [], [], meta()
    for n, e in ACCTS.items():
        mark = ">>" if n == live else "  "
        if not saved(n) and n != live:
            hint = f"not saved: {PROG} add {n}" if e else f"waiting: claude auth login, then {PROG} save {n}"
            print(f"{mark} {n[:12]:12}  {(e or '?')[:24]:24}  {hint}")
            continue
        data = live_data() if n == live else slot_data(n)
        o = oauth(data)
        d, note = usage(n)
        w, ss = pct((d or {}).get("seven_day")), pct((d or {}).get("five_hour"))
        wtxt = "  ?" if w is None else f"{w:3.0f}%"
        stxt = "  ?" if ss is None else f"{ss:3.0f}%"
        flag = "!" if (w or 0) >= NEAR else " "
        wcol = f"{bar(w, 10)} {wtxt}{flag} {reset_at((d or {}).get('seven_day')):>9}" if d else "unknown"
        scol = f"{bar(ss, 6)} {stxt}  {reset_at((d or {}).get('five_hour')):>9}" if d else ""
        ends_ms = o.get("refreshTokenExpiresAt") or 0
        print(f"{mark} {n[:12]:12}  {e[:24]:24}  {plan_of(n)[:7]:7}  {wcol:26}  {scol:21}  "
              f"{when(refreshed_at(n, data)):11}  {when(ends_ms, '%m-%d')}")
        if note:
            notes.append(f"{n}: {note}" + (f"; {PROG} refresh {n}" if "refresh" in note else ""))
        if n == live and saved(n) and fingerprint(slot_data(n)) != fingerprint(data):
            notes.append(f"{n}: saved copy behind the live login ({PROG} sync)")
        if not saved(n):
            notes.append(f"{n}: active but NOT SAVED: {PROG} save {n}")
        dead = (m.get(n) or {}).get("dead")
        if dead:
            notes.append(f"{n}: DEAD since {dead}: {PROG} add {n}")
        if ends_ms and ends_ms / 1000 - time.time() < END_WARN_DAYS * 86400:
            warnings.append(f"{n} LOGIN ENDS SOON ({when(ends_ms)}): {PROG} add {n} before then")
        fp = fingerprint(slot_data(n)) if saved(n) else ""
        if fp and fp in seen:
            notes.append(f"{n}: SAME LOGIN AS {seen[fp]}: re-add one of them")
        if fp:
            seen.setdefault(fp, n)
        for x in near_limit(d):
            warnings.append(f"{n} {x}")
        if w is not None and not note and not near_limit(d):
            rooms.append((w, n))
    if email and not live:
        notes.append(f"the live login ({email}) is not saved under any name: {PROG} save NAME")
    if not has_login:
        notes.append(f"Claude Code has no login now: {PROG} use NAME, or claude auth login then {PROG} save NAME")
    print()
    if rooms:
        w, n = min(rooms)
        print(f"Most weekly room: {n} ({w:g}% used)" +
              ("  (active)" if n == live else f"  ->  {PROG} use {n}  or  {PROG} auto"))
    if warnings:
        print("Warnings: " + "; ".join(warnings))
    for x in notes:
        print("Note: " + x)
    warn_running()
    print(f"Commands: {PROG} use NAME | auto | add NAME | save NAME | sync | refresh | help")


def cmd_auto(refresh=False):
    cmd_sync(quiet=True)
    if refresh:
        cmd_refresh([], quiet=True)
    live = live_name() if fingerprint(live_data()) else ""
    rows = []
    for n in ACCTS:
        if not saved(n) and n != live:
            continue
        d, note = usage(n)
        w, s = pct((d or {}).get("seven_day")), pct((d or {}).get("five_hour"))
        if d is None or w is None or note or near_limit(d):
            continue
        rows.append((w, s or 0.0, n))
    if not rows:
        raise Fail(f"auto: no saved account has a fresh usage reading under {NEAR:g}%. Run {PROG} list "
                   f"(or {PROG} auto --refresh to renew inactive logins first)")
    rows.sort()
    best_w, _, best = rows[0]
    cur = next((r for r in rows if r[2] == live), None)
    if best == live:
        print(f"auto: staying on {live}, it has the most weekly room ({best_w:g}% used)")
        return
    if cur and cur[0] - best_w < AUTO_MARGIN:
        print(f"auto: staying on {live} ({cur[0]:g}% weekly); {best} ({best_w:g}%) is not "
              f"{AUTO_MARGIN:g} points better")
        return
    print(f"auto: {best} has the most weekly room ({best_w:g}% used)")
    cmd_use(best)


# ---------- shell shortcuts ----------

SHORTCUTS = [("claude-accounts", "list", False), ("claude-use", "use", True), ("claude-auto", "auto", True),
             ("claude-add", "add", True), ("claude-save", "save", True), ("claude-sync", "sync", False),
             ("claude-refresh", "refresh", True), ("claude-help", "help", False)]


def self_command(shell):
    """How a shell should call this tool: the installed command, else this Python and this file."""
    if shutil.which(PROG):
        return PROG if shell != "powershell" else f"& {PROG}"
    py, me = sys.executable, os.path.abspath(__file__)
    if shell == "powershell":
        return f"& '{py}' '{me}'"
    return f"'{py}' '{me}'"


def cmd_init(shell):
    if shell not in ("powershell", "bash", "zsh"):
        raise Fail(f"usage: {PROG} init powershell|bash|zsh")
    run = self_command(shell)
    if shell == "powershell":
        print(f"# {PROG} shortcuts. Add to your profile:  {run} init powershell | Out-String | Invoke-Expression")
        for fn, sub, args in SHORTCUTS:
            print(f"function {fn} {{ {run} {sub}{' @args' if args else ''} }}")
    else:
        rc = "~/.bashrc" if shell == "bash" else "~/.zshrc"
        print(f'# {PROG} shortcuts. Add to {rc}:  eval "$({run} init {shell})"')
        rest = ' "$@"'
        for fn, sub, args in SHORTCUTS:
            print(f"{fn}() {{ {run} {sub}{rest if args else ''}; }}")


HELP = """claude-switch {version}: switch the account Claude Code uses; sessions and history stay shared

  See
    claude-switch list                 every account: weekly and 5-hour use, resets, plan, login dates, warnings
    claude-switch help                 this text

  Switch
    claude-switch use NAME             plain `claude` runs on NAME from now on (open sessions: /exit, claude -c)
    claude-switch auto                 switch to the saved account with the most weekly room

  Add or repair an account (first time, or when list says DEAD or LOGIN ENDS SOON)
    1. claude-switch add NAME          save the current login and move it aside
    2. sign the BROWSER into the account you want as NAME
    3. claude auth login               sign in
    4. claude-switch save NAME         remember it as NAME (records its email)
    claude-switch remove NAME          forget an account (its files move to the store's removed/ folder)

  Keep logins healthy
    claude-switch sync                 save the active account's newest login now (safe any time)

  Opt-in: these call Claude's OAuth token endpoint for you (README, "Terms and risk")
    claude-switch refresh [NAME..] [--force]   renew the saved logins of accounts you are not using
    claude-switch keepalive            sync + refresh, quiet, one log line: for a scheduler every 3 h
    claude-switch auto --refresh       refresh first, then pick

  Shell shortcuts (claude-accounts, claude-use, claude-auto, claude-add, claude-save, claude-help, ...)
    claude-switch init powershell|bash|zsh     print them; the first line says how to load them

  Rules
    - Never run `claude auth login` or /login without `claude-switch add NAME` first: you lose the old login.
    - Logins end about 4 weeks after sign-in and renewing does not move that date; list warns {warn} days ahead.

  Login kept in: {where}
  Store: {store}
"""


def usage_error(msg):
    raise Fail(f"{msg}\nRun `{PROG} help` for the commands.")


def main(argv=None):
    global LIVE
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "list"
    flags = {a for a in argv[1:] if a.startswith("--")}
    args = [a for a in argv[1:] if not a.startswith("--")]
    target = args[0] if args else ""
    if action in ("--version", "version"):
        print(f"{PROG} {__version__}")
        return 0
    if action == "init":
        cmd_init(target)
        return 0
    LIVE = pick_store()
    if action in ("help", "-h", "--help"):
        where = CREDS if isinstance(LIVE, FileStore) else f"macOS Keychain item {KEYCHAIN_SERVICE!r}"
        print(HELP.format(version=__version__, warn=END_WARN_DAYS, where=where, store=STORE))
        return 0
    check_profile()
    make_private_dir(STORE)
    load_accounts()
    allowed = {"list": set(), "use": set(), "auto": {"--refresh"}, "add": set(), "save": set(), "sync": set(),
               "refresh": {"--force"}, "keepalive": set(), "remove": set()}
    if action not in allowed:
        usage_error(f"unknown command {action!r}")
    bad = flags - allowed[action]
    if bad:
        usage_error(f"unknown option {sorted(bad)[0]} for {action}")
    if action in ("use", "add", "save", "remove") and len(args) != 1:
        usage_error(f"usage: {PROG} {action} NAME")
    if action == "save":
        cmd_save(target)
    elif action == "use":
        cmd_use(target)
    elif action == "add":
        cmd_add(target)
    elif action == "remove":
        cmd_remove(target)
    elif action == "sync":
        cmd_sync()
    elif action == "auto":
        cmd_auto(refresh="--refresh" in flags)
    elif action == "refresh":
        for a in args:
            known(a)
        cmd_sync(quiet=True)
        cmd_refresh(args, force="--force" in flags)
        cmd_list()
    elif action == "keepalive":
        return cmd_keepalive()
    else:
        cmd_list()
    return 0


def entry():
    """Console-script entry point."""
    sys.exit(main())


if __name__ == "__main__":
    entry()
