"""Fake-profile tests for claude_switch.py. They never touch the real profile, the real Keychain or the network.

    python -m unittest discover -s tests -v

Each test class builds a fake home in a temp folder (CLAUDE_SWITCH_TEST_HOME) and talks to a local fake OAuth server that
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
        self.post_mode = ""        # "html401": answer the next token requests like a proxy's error page
        self.redirect_usage = False  # answer usage requests with a redirect to /elsewhere
        self.elsewhere_hits = 0
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
                if server.post_mode == "html401":
                    page = b"<html><body>Proxy authentication required</body></html>"
                    self.send_response(401)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(page)))
                    self.end_headers()
                    return self.wfile.write(page)
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
                if self.path.startswith("/elsewhere"):
                    server.elsewhere_hits += 1
                    return self.reply(200, {})
                if server.redirect_usage:
                    self.send_response(302)
                    self.send_header("Location", server.url + "/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
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
        env.update(CLAUDE_SWITCH_TEST_HOME=self.home, CLAUDE_SWITCH_TOKEN_URL=self.server.url + "/v1/oauth/token",
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
        self.key = "Claude Code-credentials|claude-code-user"

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

    def test_older_claude_code_item_under_the_login_name(self):
        """Claude Code before 2.1 filed the item under $USER: read it, and write back to the same item."""
        p = self.p
        legacy = "Claude Code-credentials|tester"
        data = self.server.creds(EMAILS["alpha"])
        wjson(self.db, {legacy: json.dumps(data)})
        wjson(p.state, {"oauthAccount": {"emailAddress": EMAILS["alpha"]}})
        p.run("save", "alpha")
        self.assertEqual(rjson(p.slot("alpha")), data, "save reads the older item")
        p.run("add", "beta")
        self.assertEqual(self.keychain(), {}, "add deletes the item under either account")
        self.kc_login(EMAILS["beta"])
        p.run("save", "beta")
        p.run("use", "alpha")
        self.assertEqual(json.loads(self.keychain()[self.key]), data, "use writes where Claude Code 2.1 reads")

    def test_locked_keychain_stops_clearly(self):
        p = self.p
        self.kc_login(EMAILS["alpha"])
        out, code = p.run("save", "alpha", ok=False, FAKE_SECURITY_FAIL="36")
        self.assertTrue(code != 0 and "Unlock the login keychain" in out, "a locked Keychain gives a clear error")
        self.assertFalse(os.path.exists(p.slot("alpha")), "nothing saved from a failed read")


class ReviewFixTest(unittest.TestCase):
    """Fixes from the review before 0.2.0. Each test gets its own fake home."""

    @classmethod
    def setUpClass(cls):
        cls.server = FakeServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def setUp(self):
        self.server.post_mode, self.server.redirect_usage = "", False
        self.p = Profile(self.server)
        self.addCleanup(shutil.rmtree, self.p.home, True)

    def two_accounts(self):
        """alpha saved and inactive, beta live."""
        p = self.p
        p.login(EMAILS["alpha"])
        p.run("save", "alpha")
        p.run("add", "beta")
        p.login(EMAILS["beta"])
        p.run("save", "beta")
        return p

    def history(self):
        folder = os.path.join(self.p.store, "history")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def test_credential_manager_stops_every_command(self):
        p = self.p
        p.login(EMAILS["alpha"])
        out, code = p.run("list", ok=False, CLAUDE_CODE_FORCE_WINDOWS_CREDMAN="1")
        self.assertTrue(code != 0 and "Windows Credential Manager" in out, out)
        st = rjson(p.state)
        st["cachedGrowthBookFeatures"] = {"tengu_windows_credman": True}
        wjson(p.state, st)
        out, code = p.run("save", "alpha", ok=False)
        self.assertTrue(code != 0 and "Windows Credential Manager" in out, out)
        self.assertFalse(os.path.exists(p.slot("alpha")), "nothing saved")
        st["cachedGrowthBookFeatures"] = {"tengu_windows_credman": False}
        wjson(p.state, st)
        p.run("save", "alpha")

    def test_plan_names(self):
        """Anthropic's names since November 2026: the 20x tier is Ultra, the 5x tier is Max."""
        p = self.two_accounts()
        c = rjson(p.slot("alpha"))
        c["claudeAiOauth"]["rateLimitTier"] = "default_claude_max_5x"
        wjson(p.slot("alpha"), c)
        out, _ = p.run("list")
        # Table rows start with ">> " (active) or "   ", then the account name.
        rows = {line[3:].split()[0]: line for line in out.splitlines() if line[3:].split()[:1] in (["alpha"], ["beta"])}
        self.assertIn(" Max ", rows["alpha"], out)
        self.assertIn(" Ultra ", rows["beta"], out)
        self.assertNotIn("20x", out)
        acct = rjson(os.path.join(p.store, "beta.account.json"))
        acct["planDisplayName"] = "Team Premium"
        wjson(os.path.join(p.store, "beta.account.json"), acct)
        out, _ = p.run("list")
        self.assertIn("Team Pr", out, "the name Claude Code records wins")

    def test_names_that_differ_only_in_case_are_refused(self):
        p = self.p
        p.login(EMAILS["alpha"])
        p.run("save", "work")
        for cmd in ("add", "save"):
            out, code = p.run(cmd, "Work", ok=False)
            self.assertTrue(code != 0 and "only in case" in out, f"{cmd}: {out}")
        self.assertEqual(list(rjson(os.path.join(p.store, "accounts.json"))), ["work"])

    def test_history_of_a_dotted_name_stays_apart(self):
        p = self.p
        p.login(EMAILS["alpha"])
        p.run("save", "work")
        p.run("add", "work.old")
        p.login(EMAILS["beta"])
        p.run("save", "work.old")
        p.run("use", "work")
        for _ in range(4):
            p.run("refresh", "work.old", "--force")
        p.run("refresh", "work.old", "--force")
        old_copies = [f for f in self.history() if f.startswith("work.old.")]
        self.assertEqual(len(old_copies), 3, self.history())
        p.run("use", "work.old")
        p.run("refresh", "work", "--force")
        self.assertEqual(len([f for f in self.history() if f.startswith("work.old.")]), 3,
                         "renewing work leaves work.old's copies alone")
        self.assertEqual(len([f for f in self.history() if f.startswith("work.2")]), 1, self.history())

    def test_history_failure_sends_nothing(self):
        p = self.two_accounts()
        before, posts = rjson(p.slot("alpha")), len(self.server.posts)
        shutil.rmtree(os.path.join(p.store, "history"), ignore_errors=True)
        with open(os.path.join(p.store, "history"), "w") as fh:  # a file where the folder should be
            fh.write("x")
        out, _ = p.run("refresh", "alpha", "--force")
        self.assertIn("could not keep a history copy", out)
        self.assertEqual(len(self.server.posts), posts, "no token request after the backup failed")
        self.assertEqual(rjson(p.slot("alpha")), before, "saved copy unchanged")
        self.assertIn(before["claudeAiOauth"]["refreshToken"], self.server.rt, "the saved login is still alive")

    def test_proxy_error_page_does_not_mark_a_login_dead(self):
        p = self.two_accounts()
        self.server.post_mode = "html401"
        out, _ = p.run("refresh", "alpha", "--force")
        self.assertIn("FAILED: HTTP 401, saved copy unchanged", out)
        out, _ = p.run("list")
        self.assertNotIn("DEAD", out)

    def test_usage_redirect_is_not_followed(self):
        p = self.two_accounts()
        if os.path.exists(os.path.join(p.store, "usage-cache.json")):
            os.remove(os.path.join(p.store, "usage-cache.json"))
        self.server.redirect_usage = True
        out, _ = p.run("list")
        self.assertIn("usage HTTP 302", out)
        self.assertEqual(self.server.elsewhere_hits, 0, "the token never went to the redirect target")

    def test_broken_json_files_give_no_traceback(self):
        p = self.two_accounts()
        for name in ("usage-cache.json", "meta.json"):
            with open(os.path.join(p.store, name), "w") as fh:
                fh.write("[1, 2]")
        out, _ = p.run("list")
        self.assertNotIn("Traceback", out)
        with open(p.state, "w") as fh:
            fh.write("[]")
        out, code = p.run("use", "alpha", ok=False)
        self.assertTrue(code != 0 and "must hold a JSON object" in out and "Traceback" not in out, out)

    def test_use_takes_and_releases_claude_codes_locks(self):
        p = self.two_accounts()
        p.run("use", "alpha")
        for path in (os.path.join(p.home, ".claude", ".oauth_refresh.lock"), os.path.join(p.home, ".claude.lock"),
                     os.path.join(p.home, ".claude", ".storage-write.lock"), p.state + ".lock"):
            self.assertFalse(os.path.exists(path), path)

    def test_a_live_storage_lock_makes_use_wait_then_stop(self):
        """A Claude Code session writing its login keeps the lock fresh; use must wait, not take it over."""
        p = self.two_accounts()
        lock = os.path.join(p.home, ".claude", ".storage-write.lock")
        os.mkdir(lock)
        done = threading.Event()

        def keep_fresh():
            while not done.wait(2):
                os.utime(lock)

        threading.Thread(target=keep_fresh, daemon=True).start()
        self.addCleanup(done.set)
        start = time.time()
        out, code = p.run("use", "alpha", ok=False)
        self.assertTrue(code != 0 and "busy" in out, out)
        self.assertGreater(time.time() - start, 15, "use waited for the lock")
        self.assertEqual(p.email(), EMAILS["beta"], "nothing switched")
        self.assertTrue(os.path.isdir(lock), "the other holder's lock is left alone")

    @unittest.skipIf(os.name == "nt", "file modes are a POSIX matter")
    def test_keepalive_log_is_private(self):
        p = self.two_accounts()
        p.run("keepalive")
        self.assertEqual(os.stat(os.path.join(p.store, "keepalive.log")).st_mode & 0o777, 0o600)

    def test_test_home_never_picks_the_keychain_by_itself(self):
        p = self.p
        p.login(EMAILS["alpha"])
        out, _ = p.run("help", CLAUDE_SWITCH_BACKEND="")
        self.assertIn(".credentials.json", out, "a test home uses the file backend unless told otherwise")
        out, code = p.run("list", ok=False, CLAUDE_SWITCH_BACKEND="keychain", CLAUDE_SWITCH_SECURITY="")
        self.assertTrue(code != 0 and "needs CLAUDE_SWITCH_SECURITY" in out, out)


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
