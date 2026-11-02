# Security

## What claude-switch stores

A saved login holds an OAuth access token and refresh token. Anyone with a copy can use that Claude account until
the login ends. claude-switch treats these files like passwords.

- Saved logins live in `~/.claude-switch/` (`NAME.credentials.json`, `history/`, `parked-*`, `rescue-*`,
  `removed/`), in the same JSON shape Claude Code uses.
- Every write goes to a new temp file that is flushed to disk and then renamed into place, so a crash never leaves
  half a login behind, and the temp file is deleted when a write fails.
- On Linux and macOS, claude-switch creates that folder with mode 0700 and every file it writes with mode 0600. On
  Windows, the files inherit your user profile's permissions, which other non-admin users cannot read by default.
- On macOS the live login stays in the login Keychain, where Claude Code keeps it. claude-switch writes it with
  `security -i` and passes the value in hex on stdin, so the token does not appear in the process list. A value
  longer than about 4 KB goes as a command-line argument instead (still hex), because `security -i` cuts longer lines.
- `~/.claude-switch/` is not encrypted. Full-disk encryption and a locked screen protect it, the same as
  Claude Code's own `~/.claude/.credentials.json`.

## What claude-switch never does

- It never prints or logs a token. Output and `keepalive.log` show account names, emails, dates and HTTP status
  codes only. The test suite checks every output and log for token text.
- It compares logins by a SHA-256 hash of the refresh token and stores only the first 16 hex characters of that hash
  in `meta.json`.
- It sends tokens only to `platform.claude.com` (renewal, opt-in) and `api.anthropic.com` (usage readings), over
  HTTPS with certificate checks, and follows no redirects, so a token never reaches another address. No telemetry.
- It never edits your shell profile or environment variables.

## Before you share anything

Do not paste files from `~/.claude-switch/` or `~/.claude/.credentials.json` into an issue. Command output and
`keepalive.log` hold no tokens, but they do hold your account emails; remove those if you prefer.

If a login leaks, treat it like a leaked password: secure that Claude account on claude.ai and follow Anthropic's
help pages for ending its sessions. claude-switch cannot revoke a token. Then run `claude-switch add NAME` to make a
new login and delete the leaked copies from `~/.claude-switch/`, including `history/`.

## Reporting a vulnerability

Open a private security advisory on this repository (https://github.com/Abubakr-Alsheikh/claude-account-switch/security/advisories/new), or email the maintainer,
Abubakr Alsheikh, at abubakr.alsheikh@outlook.com. Please do not open a public issue for a security problem.
