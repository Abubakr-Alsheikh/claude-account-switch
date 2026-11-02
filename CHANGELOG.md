# Changelog

## 0.2.1 (2026-11-02)

- Plan names follow Anthropic's November 2026 renaming: the 20x tier shows as Ultra and the 5x tier as Max. The
  name Claude Code records for an account (`planDisplayName`) still comes first.

## 0.2.0 (2026-11-02)

Fixes from a review of 0.1.0 before its first tagged release.

- Stops with a clear message when Claude Code keeps the Windows login in Windows Credential Manager
  (`CLAUDE_CODE_FORCE_WINDOWS_CREDMAN`, or the `tengu_windows_credman` setting), instead of swapping a file Claude
  Code no longer reads.
- macOS: reads and writes the Keychain item under `claude-code-user`, the account Claude Code 2.1 uses, and still
  reads an item that older versions filed under your user name.
- Holds Claude Code's current and legacy renewal locks and its login-write lock (`.storage-write.lock`) in Claude
  Code's order, takes its own store lock first, keeps the hold short, and never removes a lock another process holds.
- `refresh` keeps the history copy before the token request, so a failed backup sends nothing; a failed write after
  the request leaves a `rescue-` copy.
- A proxy's error page no longer marks a login dead; only the token server's own `invalid_grant` answer does.
- Requests follow no redirects, so a token never reaches another address.
- Refuses names that differ only in case (`work`, `Work`), which shared one file on Windows and macOS.
- History rotation matches the exact account name, so `work` no longer deletes the copies of `work.old`; copies made
  in the same second keep separate names.
- Writes flush to disk and use a temp file per process, removed on failure.
- Broken or unexpected JSON in `usage-cache.json`, `meta.json` or `~/.claude.json` gives a message, not a traceback.
- `keepalive` logs failures too, and `keepalive.log` is private (0600).
- The running-process count no longer crashes on non-English Windows in UTF-8 mode.
- Tests: the fake home variable is now `CLAUDE_SWITCH_TEST_HOME`, and it never selects the real Keychain.
- README: what leaves your machine, how to uninstall, accurate platform and process-list claims.

## 0.1.0 (2026-10-30, not tagged)

First public version.

- `list`, `use`, `auto`, `add`, `save`, `sync`, `remove`, `refresh`, `keepalive`, `init`, `help`.
- Saves the outgoing login on every command and holds Claude Code's refresh and state locks while it swaps.
- Refuses to save one account's login under another name (refresh-token hash and email checks).
- Keeps the last 3 copies of each login; parks unknown logins instead of overwriting them.
- Marks a saved login dead when the token endpoint answers `invalid_grant`.
- Usage table with weekly and 5-hour bars, reset times, plan, last renewal, login end and warnings.
- Renewal of inactive logins is opt-in (`refresh`, `keepalive`, `auto --refresh`).
- Accounts and names come from `~/.claude-switch/accounts.json`; `save` records the email.
- Windows and Linux through `~/.claude/.credentials.json`; macOS through the login Keychain.
- Shell shortcuts printed by `init powershell|bash|zsh`.
- Saved logins are private files (0600 in a 0700 folder on Linux and macOS).
