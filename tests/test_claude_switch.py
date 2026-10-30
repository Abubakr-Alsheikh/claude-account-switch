"""Fake-profile tests for claude_switch.py. They never touch the real profile, the real Keychain or the network.

    python -m unittest discover -s tests -v

Each test class builds a fake home in a temp folder (CLAUDE_SWITCH_HOME) and talks to a local fake OAuth server that
rotates refresh tokens the way the real one does (each refresh token works once).
"""
import datetime
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "claude_switch.py")
FAKE_SECURITY = os.path.join(ROOT, "tests", "fake_security.py")
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
EMAILS = {"alpha": "alpha@example.com", "beta": "beta@example.com"}
TOKEN_MARK = "tok-"  # every fake token starts with this; no output may contain it


class FakeServer:
    """Token endpoint with single-use refresh tokens, and the usage endpoint."""

    def __init__(self):
        self.rt, self.at, self.n, self.posts = {}, {}, 0, []
        self.usage = {"alpha@example.com": 20.0, "beta@example.com": 70.0}
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.posts.append({k: v for k, v in body.items() if k != "refresh_token"})
                acct = server.rt.pop(body.get("refresh_token"), None)  # single use
                if (self.headers["Content-Type"] != "application/json" or body.get("grant_type") != "refresh_token"
                        or not acct):
                    return self.reply(400, {"error": "invalid_grant",
                                            "error_description": "Refresh token not found or invalid"})
                rt, at = server.issue(acct)
                self.reply(200, {"access_token": at, "refresh_token": rt, "expires_in": 28800,
                                 "refresh_token_expires_in": 2592000, "scope": body.get("scope", ""),
                                 "token_type": "Bearer"})

            def do_GET(self):
                tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                acct = server.at.get(tok)
                if not acct or self.headers.get("anthropic-beta") != "oauth-2025-04-20":
                    return self.reply(401, {"error": "unauthorized"})
                reset = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)).isoformat()
                u = server.usage[acct]
                self.reply(200, {"five_hour": {"utilization": u / 2, "resets_at": reset},
                                 "seven_day": {"utilization": u, "resets_at": reset}})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def issue(self, email):
        self.n += 1
        rt, at = f"{TOKEN_MARK}rt-{self.n}", f"{TOKEN_MARK}at-{self.n}"
        self.rt[rt] = email
        self.at[at] = email
        return rt, at

    def creds(self, email, expired=False):
        rt, at = self.issue(email)
        exp = int((time.time() + (-60 if expired else 8 * 3600)) * 1000)
        return {"mcpOAuth": {"x": 1}, "claudeAiOauth": {
            "accessToken": at, "refreshToken": rt, "expiresAt": exp, "refreshTokenExpiresAt": exp + 30 * 86400 * 1000,
            "scopes": ["user:inference", "user:profile"], "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_20x"}}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def wjson(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, separators=(",", ":"))


def rjson(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def rtext(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Profile:
    """A fake home folder plus helpers to run the CLI against it."""

    def __init__(self, server, backend="file"):
        self.server = server
        self.home = tempfile.mkdtemp(prefix="claude-switch-test-")
        self.creds = os.path.join(self.home, ".claude", ".credentials.json")
        self.state = os.path.join(self.home, ".claude.json")
        self.store = os.path.join(self.home, ".claude-switch")
        self.backend = backend
        self.outputs = []

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")}
        env.update(CLAUDE_SWITCH_HOME=self.home, CLAUDE_SWITCH_TOKEN_URL=self.server.url + "/v1/oauth/token",
                   CLAUDE_SWITCH_USAGE_URL=self.server.url + "/api/oauth/usage", CLAUDE_SWITCH_BACKEND=self.backend,
                   CLAUDE_SWITCH_SECURITY=FAKE_SECURITY, USER="tester", PYTHONIOENCODING="utf-8")
        env.update(extra)
        return env

    def run(self, *args, ok=True, **extra):
        p = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=self.env(**extra),
                           timeout=60)
        out = p.stdout + p.stderr
        self.outputs.append(out)
        if ok and p.returncode != 0:
            raise AssertionError(f"`{' '.join(args)}` exited {p.returncode}:\n{out}")
        return out, p.returncode

    def login(self, email, data=None):
        """What `claude auth login` does: write a new login and name its account in ~/.claude.json."""
        self.write_live(data or self.server.creds(email))
        st = rjson(self.state) if os.path.exists(self.state) else {"projects": {"/work/x": {"history": [1, 2]}}}
        st["oauthAccount"] = {"emailAddress": email}
        wjson(self.state, st)

    def write_live(self, data):
        wjson(self.creds, data)

    def read_live(self):
        return rjson(self.creds)

    def slot(self, name):
        return os.path.join(self.store, f"{name}.credentials.json")

    def email(self):
        return rjson(self.state)["oauthAccount"]["emailAddress"]

    def renew_live(self):
        """What a running claude session does when its token ages: renew the live login and write it back."""
        c = self.read_live()
        email = self.server.rt.pop(c["claudeAiOauth"]["refreshToken"])
        rt, at = self.server.issue(email)
        c["claudeAiOauth"].update(accessToken=at, refreshToken=rt)
        self.write_live(c)
        return rt

    def all_output(self):
        log = os.path.join(self.store, "keepalive.log")
        return "\n".join(self.outputs) + (rtext(log) if os.path.exists(log) else "")


class FlowTest(unittest.TestCase):
    """The full life of two accounts on the file backend (Windows and Linux)."""

    @classmethod
    def setUpClass(cls):
        cls.server = FakeServer()
        cls.p = Profile(cls.server)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        shutil.rmtree(cls.p.home, ignore_errors=True)

    def test_flow(self):
        p, server = self.p, self.server
        a, b = EMAILS["alpha"], EMAILS["beta"]

        # 1. alpha is logged in; save it. beta cannot be filed under alpha's login.
        p.login(a)
        out, _ = p.run("save", "alpha")
        self.assertIn("saved alpha", out, "save alpha")
        self.assertEqual(rjson(os.path.join(p.store, "accounts.json")), {"alpha": a}, "save records the email")
        out, code = p.run("save", "beta", ok=False)
        self.assertTrue(code != 0 and "already saved as alpha" in out, "save refuses a second name for one login")
        if os.name != "nt":
            self.assertEqual(os.stat(p.slot("alpha")).st_mode & 0o777, 0o600, "saved login is private (0600)")
            self.assertEqual(os.stat(p.store).st_mode & 0o777, 0o700, "store folder is private (0700)")

        # 2. add beta: park alpha, simulate `claude auth login` as beta, save beta
        p.run("add", "beta")
        self.assertFalse(os.path.exists(p.creds), "add moves the live login aside")
        self.assertEqual(rjson(os.path.join(p.store, "accounts.json"))["beta"], "", "add registers a pending name")
        p.login(b)
        out, _ = p.run("save", "beta")
        self.assertIn("saved beta", out, "save beta after a new login")
        out, code = p.run("save", "alpha", ok=False)
        self.assertTrue(code != 0 and f"not {a}" in out, "save refuses a name whose email is not logged in")

        # 3. list shows both with usage
        out, _ = p.run("list")
        self.assertTrue(">> beta" in out and " 70% " in out and " 20% " in out,
                        "list shows the active mark and both usage readings")
        self.assertIn("Most weekly room: alpha", out, "list names the account with the most room")

        # 4. a running beta session renews its login, then the user switches to alpha
        newest = p.renew_live()
        p.run("use", "alpha")
        self.assertEqual(rjson(p.slot("beta"))["claudeAiOauth"]["refreshToken"], newest,
                         "use saves the outgoing account's newest login")
        self.assertEqual(p.email(), a, "use swaps oauthAccount")
        self.assertEqual(rjson(p.state)["projects"], {"/work/x": {"history": [1, 2]}},
                         "use keeps the rest of .claude.json (shared history)")
        self.assertEqual(p.read_live()["mcpOAuth"], {"x": 1}, "use keeps the credentials file's other keys")
        self.assertFalse(os.path.exists(os.path.join(p.home, ".claude", ".oauth_refresh.lock")),
                         "use releases Claude Code's lock")

        # 5. back to beta: its saved login must still be alive at the server
        p.run("use", "beta")
        self.assertIn(p.read_live()["claudeAiOauth"]["refreshToken"], server.rt, "switched-back login is still valid")
        out, _ = p.run("sync")
        self.assertIn("saved beta", out, "sync")

        # 6. refresh: alpha (inactive) has an expired access token
        c1 = rjson(p.slot("alpha"))
        c1["claudeAiOauth"]["expiresAt"] = int((time.time() - 60) * 1000)
        wjson(p.slot("alpha"), c1)
        os.remove(os.path.join(p.store, "usage-cache.json"))
        out, _ = p.run("list")
        self.assertIn("unknown, refresh needed", out, "list says refresh needed for an expired inactive login")
        out, _ = p.run("auto", ok=False)
        self.assertIn("auto: staying on beta", out, "auto without --refresh never renews and skips unknown readings")
        self.assertEqual(server.posts, [], "nothing called the token endpoint before an opt-in command")
        beta_before = rjson(p.slot("beta"))
        out, _ = p.run("refresh")
        self.assertTrue("alpha  renewed" in out and "refresh token rotated" in out, "refresh renews the inactive one")
        self.assertIn("beta  skipped: active", out, "refresh skips the active account")
        self.assertEqual(rjson(p.slot("beta")), beta_before, "refresh leaves the active account's copy alone")
        self.assertTrue(any(f.startswith("alpha.") for f in os.listdir(os.path.join(p.store, "history"))),
                        "refresh keeps a backup first")
        self.assertIn(" 20% ", out, "the renewed token reads usage")
        out, _ = p.run("refresh", "alpha", "--force")
        self.assertIn("alpha  renewed", out, "the rotated refresh token works a second time")
        self.assertLessEqual(len([f for f in os.listdir(os.path.join(p.store, "history"))
                                  if f.startswith("alpha.")]), 3, "history keeps at most 3 copies")
        out, _ = p.run("refresh", "alpha")
        self.assertIn("fresh, access token good", out, "refresh skips a login with hours left")
        self.assertEqual(rjson(p.slot("alpha"))["mcpOAuth"], {"x": 1}, "refresh keeps the credentials' other keys")
        post = server.posts[-1]
        self.assertEqual((post["client_id"], post["grant_type"], post["scope"]),
                         (CLIENT_ID, "refresh_token", "user:inference user:profile"), "refresh request shape")

        # 7. a dead saved login is reported and left unchanged
        c1 = rjson(p.slot("alpha"))
        c1["claudeAiOauth"]["refreshToken"] = TOKEN_MARK + "used-elsewhere"
        wjson(p.slot("alpha"), c1)
        out, _ = p.run("refresh", "alpha", "--force")
        self.assertTrue("FAILED: HTTP 400 invalid_grant" in out and "claude-switch add alpha" in out,
                        "dead login reported")
        self.assertEqual(rjson(p.slot("alpha")), c1, "dead login file unchanged")
        out, _ = p.run("list")
        self.assertIn("DEAD since", out, "list flags the dead login")
        rt, at = server.issue(a)
        c1["claudeAiOauth"].update(refreshToken=rt, accessToken=at, expiresAt=int((time.time() + 3600) * 1000))
        wjson(p.slot("alpha"), c1)

        # 8. keepalive (the scheduled job) logs one line, no secrets
        os.remove(os.path.join(p.store, "usage-cache.json"))
        out, code = p.run("keepalive")
        log = rtext(os.path.join(p.store, "keepalive.log"))
        self.assertTrue(code == 0 and "alpha: renewed" in log and "beta: skipped" in log, "keepalive renews and logs")

        # 9. auto picks the account with the most weekly room (alpha 20% vs beta 70%)
        out, _ = p.run("auto")
        self.assertTrue("alpha has the most weekly room" in out and p.email() == a, "auto switches to the most room")
        out, _ = p.run("auto")
        self.assertIn("staying on alpha", out, "auto stays when the active one is best")

        # 10. a stale Claude Code lock (crashed process) does not block a switch
        lock = os.path.join(p.home, ".claude", ".oauth_refresh.lock")
        os.mkdir(lock)
        old = time.time() - 120
        os.utime(lock, (old, old))
        p.run("use", "beta")
        self.assertEqual(p.email(), b, "use takes over a stale lock")

        # 11. snapshot refuses to file one account's login under another name
        st = rjson(p.state)
        st["oauthAccount"] = {"emailAddress": a}  # state says alpha but the live login is beta's
        wjson(p.state, st)
        out, _ = p.run("save", "alpha", ok=False)
        self.assertIn("is beta's saved login", out, "save refuses another account's login")
        st["oauthAccount"] = {"emailAddress": b}
        wjson(p.state, st)

        # 12. an unknown live login is parked, not lost, when you switch away from it
        p.login("gamma@example.com")
        out, _ = p.run("use", "alpha")
        self.assertTrue("matched no saved account" in out
                        and any(f.startswith("parked-") for f in os.listdir(p.store)), "unknown login is parked")

        # 13. remove forgets an inactive account and keeps its files
        out, code = p.run("remove", "alpha", ok=False)
        self.assertTrue(code != 0 and "is the live login" in out, "remove refuses the live account")
        p.run("remove", "beta")
        self.assertNotIn("beta", rjson(os.path.join(p.store, "accounts.json")), "remove drops the name")
        self.assertTrue(any(f.startswith("beta.") for f in os.listdir(os.path.join(p.store, "removed"))),
                        "remove keeps the files in removed/")

        self.assertNotIn(TOKEN_MARK, p.all_output(), "no token in any output or log")


class KeychainTest(unittest.TestCase):
    """The macOS backend, against a fake `security` tool."""

    @classmethod
    def setUpClass(cls):
        cls.server = FakeServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def setUp(self):
        self.p = Profile(self.server, backend="keychain")
        self.db = os.path.join(self.p.home, "fake-keychain.json")
        self.key = "Claude Code-credentials|tester"

    def tearDown(self):
        shutil.rmtree(self.p.home, ignore_errors=True)

    def keychain(self):
        return rjson(self.db) if os.path.exists(self.db) else {}

    def kc_login(self, email):
        """What `claude auth login` does on macOS: the login goes to the Keychain item."""
        data = self.server.creds(email)
        items = self.keychain()
        items[self.key] = json.dumps(data)
        wjson(self.db, items)
        st = rjson(self.p.state) if os.path.exists(self.p.state) else {"projects": {}}
        st["oauthAccount"] = {"emailAddress": email}
        wjson(self.p.state, st)
        return data

    def test_keychain_flow(self):
        p = self.p
        alpha = self.kc_login(EMAILS["alpha"])
        out, _ = p.run("save", "alpha")
        self.assertIn("saved alpha", out)
        self.assertEqual(rjson(p.slot("alpha")), alpha, "save reads the login from the Keychain")
        p.run("add", "beta")
        self.assertNotIn(self.key, self.keychain(), "add deletes the Keychain item after parking it")
        self.kc_login(EMAILS["beta"])
        p.run("save", "beta")
        p.run("use", "alpha")
        self.assertEqual(json.loads(self.keychain()[self.key]), alpha, "use writes the Keychain item")
        self.assertFalse(os.path.exists(p.creds), "use creates no plaintext credentials file on macOS")
        self.assertEqual(p.email(), EMAILS["alpha"])
        self.assertNotIn(TOKEN_MARK, p.all_output(), "no token in any output")

    def test_keychain_rewrites_existing_fallback_file(self):
        p = self.p
        self.kc_login(EMAILS["alpha"])
        p.run("save", "alpha")
        p.run("add", "beta")
        self.kc_login(EMAILS["beta"])
        p.run("save", "beta")
        wjson(p.creds, {"stale": True})
        p.run("use", "alpha")
        self.assertEqual(p.read_live(), rjson(p.slot("alpha")), "an existing fallback file gets the same login")

    def test_file_fallback_when_no_item(self):
        p = self.p
        p.login(EMAILS["alpha"])  # Claude Code wrote the file because the Keychain was unusable
        out, _ = p.run("save", "alpha")
        self.assertIn("saved alpha", out, "with no Keychain item, the credentials file is read")

    def test_locked_keychain_stops_clearly(self):
        p = self.p
        self.kc_login(EMAILS["alpha"])
        out, code = p.run("save", "alpha", ok=False, FAKE_SECURITY_FAIL="36")
        self.assertTrue(code != 0 and "Unlock the login keychain" in out, "a locked Keychain gives a clear error")
        self.assertFalse(os.path.exists(p.slot("alpha")), "nothing saved from a failed read")


class CliTest(unittest.TestCase):
    """Argument handling, help and the shell shortcuts."""

    @classmethod
    def setUpClass(cls):
        cls.server = FakeServer()
        cls.p = Profile(cls.server)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        shutil.rmtree(cls.p.home, ignore_errors=True)

    def test_help(self):
        out, _ = self.p.run("help")
        for word in ("use NAME", "add NAME", "save NAME", "refresh", "keepalive", "init powershell|bash|zsh"):
            self.assertIn(word, out)

    def test_list_with_no_accounts(self):
        out, _ = self.p.run("list")
        self.assertIn("No accounts yet", out)

    def test_bad_names_and_commands(self):
        out, code = self.p.run("add", "../evil", ok=False)
        self.assertTrue(code != 0 and "bad account name" in out)
        out, code = self.p.run("use", "nobody", ok=False)
        self.assertTrue(code != 0 and "unknown account" in out)
        out, code = self.p.run("frobnicate", ok=False)
        self.assertTrue(code != 0 and "unknown command" in out)
        out, code = self.p.run("refresh", "--yes", ok=False)
        self.assertTrue(code != 0 and "unknown option" in out)

    def test_custom_config_dir_is_refused(self):
        out, code = self.p.run("list", ok=False, CLAUDE_CONFIG_DIR=os.path.join(self.p.home, "other"))
        self.assertTrue(code != 0 and "CLAUDE_CONFIG_DIR is set" in out)

    def test_token_override_warning(self):
        self.p.login(EMAILS["alpha"])
        self.p.run("save", "alpha")
        out, _ = self.p.run("use", "alpha", CLAUDE_CODE_OAUTH_TOKEN="x")
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN is set", out)

    def test_init_shells(self):
        for shell, fn in (("bash", "claude-use() {"), ("zsh", "claude-accounts() {"),
                          ("powershell", "function claude-auto {")):
            out, _ = self.p.run("init", shell)
            self.assertIn(fn, out, shell)
            for name in ("claude-use", "claude-accounts", "claude-auto", "claude-add", "claude-help"):
                self.assertIn(name, out, f"{shell}: {name}")


if __name__ == "__main__":
    unittest.main()
