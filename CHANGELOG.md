# Changelog

## 0.1.0 (unreleased)

First public release.

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
