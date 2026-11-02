# claude-account-switch

Switch the account [Claude Code](https://code.claude.com/docs) uses with one command. Your sessions,
history, settings and `claude -c` keep working across the switch, because only the login changes.

```
$ claude-switch list
CLAUDE ACCOUNTS   active: work (me@work.example)

   name          email                     plan     weekly use (resets)         5-hour use (resets)    renewed      ends
   ------------  ------------------------  -------  --------------------------  ---------------------  -----------  -----
>> work          me@work.example           Ultra    #######...  71%  Sun 09:00  ###...  48%     17:00  10-30 12:10  11-24
   personal      me@home.example           Max      ##........  18%  Tue 21:00  ......   0%     19:40  10-30 09:02  11-19

Most weekly room: personal (18% used)  ->  claude-switch use personal  or  claude-switch auto
Commands: claude-switch use NAME | auto | add NAME | save NAME | sync | refresh | help
```

One Python file, standard library only. For Windows and Linux; macOS support is written and tested against a fake
Keychain, not yet on a real Mac. Unofficial: not made or supported by Anthropic.

## The problem

You pay for more than one Claude subscription (say, work and personal) and want plain `claude` to run on
whichever one has room left. Logging out and in loses the old login. A separate config folder per account splits
your history and your projects in two.

The obvious fix is to keep a copy of each account's login file and copy the right one back. That copy stops
working within hours, and the reason shapes this whole tool.

## Why a saved login dies: single-use refresh tokens

Claude Code signs in with OAuth. It holds a short-lived access token (about 8 hours) and a refresh token. When the
access token runs out, Claude Code trades the refresh token for a new pair and writes both back to the login file.
**The server accepts each refresh token once.** After a renewal the old refresh token is dead.

So a copy you saved on Monday dies the moment any running `claude` renews that login on Monday night. Copy it back
on Tuesday and Claude Code asks you to log in again.

claude-switch works around this:

- **Most commands save the outgoing login first.** `use`, `list`, `auto`, `add`, `sync`, `refresh` and `keepalive`
  store the active account's newest login before anything else, so your saved copy is never older than your last
  command.
- **It holds Claude Code's own locks while it swaps.** Claude Code takes `~/.claude/.oauth_refresh.lock` and
  `~/.claude.lock` before it renews a login, `~/.claude/.storage-write.lock` before it writes one, and
  `~/.claude.json.lock` before it writes its state. claude-switch takes the same locks in the same order, only for the
  moment of the swap, so no running session renews or rewrites the login halfway through a switch.
- **It refuses to file one account's login under another name.** It compares logins by a SHA-256 hash of the refresh
  token and by the email Claude Code records.
- **It keeps the last 3 copies of each account** in `~/.claude-switch/history/`.
- **It notices a dead login.** When the token endpoint answers `invalid_grant`, `list` marks the account DEAD and
  tells you how to repair it.
- **It parks logins it does not know** instead of overwriting them.

## How it works

Claude Code keeps the login in one place per user:

| Platform | Login | Account details |
|---|---|---|
| Windows, Linux | `~/.claude/.credentials.json` | `oauthAccount` in `~/.claude.json` |
| macOS | Keychain item `Claude Code-credentials` (account `claude-code-user`; older Claude Code versions used your user name, which claude-switch also reads), with `~/.claude/.credentials.json` as fallback | `oauthAccount` in `~/.claude.json` |

Claude Code 2.1.316 can also keep the Windows login in Windows Credential Manager, when
`CLAUDE_CODE_FORCE_WINDOWS_CREDMAN` is set or when Anthropic turns that on for your account. claude-switch does not
handle that store yet: it notices and stops before it changes anything.

claude-switch saves both parts per account in `~/.claude-switch/` and swaps them in on `use`. It changes nothing
else in `~/.claude.json`, so projects, history and settings stay shared.

## How it differs from other switchers

- **Per-account config folders or tokens.** Some switchers give each account its own `CLAUDE_CONFIG_DIR`, or a
  long-lived token in `CLAUDE_CODE_OAUTH_TOKEN`. That avoids the refresh-token race, but each account gets its own
  history and projects, `claude -c` cannot pick up a session from another account, and a token left in your
  environment silently overrides every login. claude-switch keeps one profile and swaps only the login.
- **[claude-swap](https://github.com/realiti4/claude-swap)** by realiti4 is a larger, feature-rich switcher (TUI,
  menu bar, automatic switching). claude-switch is smaller: one file, no dependencies, a usage table, and opt-in
  renewal of the accounts you are not using. We borrowed ideas from claude-swap and thank its author: the macOS
  Keychain handling (service and account names, writing through `security -i` in hex so the token stays out of
  the process list unless the login is longer than about 4 KB) follows its implementation, and so does the rule
  never to log out before adding an account.

## Install

Python 3.9 or newer.

```
pipx install git+https://github.com/Abubakr-Alsheikh/claude-account-switch@v0.2.1
```

The PyPI package named `claude-switch` is a different project. Install from GitHub as shown.

Or copy `claude_switch.py` anywhere and run `python claude_switch.py`. It has no dependencies.

## Quick start

```
# You are logged in to Claude Code as your first account. Name it:
claude-switch save work

# Add the second account:
claude-switch add personal     # saves "work" and moves its login aside
#   sign the BROWSER into the second account
claude auth login              # Claude Code signs in
claude-switch save personal    # records the login and its email

# From now on:
claude-switch list             # usage of every account
claude-switch use work         # plain `claude` runs on work
claude-switch auto             # pick the account with the most weekly room
```

In a running session, type `/exit` and then `claude -c` to continue the same conversation on the new account.

## Commands

| Command | What it does |
|---|---|
| `claude-switch list` | Table of every account: weekly and 5-hour use with bars and reset times, plan, last renewal, login end, warnings at 90%, and the account with the most weekly room. Default command. |
| `claude-switch use NAME` | Make NAME the login of `claude`. Saves the outgoing login first. |
| `claude-switch auto` | Switch to the saved account with the most weekly room. Stays put unless another account is 5 points better. |
| `claude-switch add NAME` | Before `claude auth login`: saves the current login and moves it aside so the new login cannot overwrite it. |
| `claude-switch save NAME` | Remember the account that is logged in now as NAME. The first save records its email. |
| `claude-switch sync` | Save the active account's newest login now. Safe any time. |
| `claude-switch remove NAME` | Forget an account. Its files move to `~/.claude-switch/removed/`. |
| `claude-switch refresh [NAME..] [--force]` | **Opt-in.** Renew the saved logins of accounts you are not using (default: those with under 3 hours left), then list. |
| `claude-switch keepalive` | **Opt-in.** `sync` + `refresh`, quiet, one log line. For a scheduler. |
| `claude-switch auto --refresh` | **Opt-in.** Renew inactive logins first, then pick. |
| `claude-switch init powershell\|bash\|zsh` | Print shell shortcuts (see below). |
| `claude-switch help` | All commands with the add steps. |

Account names are free-form: 1 to 32 letters, digits, `.`, `-` or `_`, starting with a letter or digit. Names that
differ only in case (`work`, `Work`) count as one name, because Windows and macOS file names ignore case.

## Add or repair an account

Use these steps the first time, and again when `list` says **DEAD** or **LOGIN ENDS SOON**:

1. `claude-switch add NAME` saves and parks the current login.
2. Sign your browser into the account you want as NAME. `claude auth login` uses whatever account the browser has.
3. `claude auth login`
4. `claude-switch save NAME`

Never run a plain `claude auth login` or `/login` without step 1. Logging in replaces the current login, and the
old one is gone unless claude-switch saved it.

## Login lifetime

A login ends about 4 weeks after you sign in (`refreshTokenExpiresAt` in the login file). Renewing the access token
does not move that date; only a fresh `claude auth login` does. `list` shows the end date and warns 3 days ahead.
Repair the account with the steps above before then.

## Renewing the logins you are not using (opt-in)

An inactive account's saved access token expires after about 8 hours. Its usage then shows as unknown, and
`auto` skips it.

`claude-switch refresh` renews those saved logins at the endpoint and with the fields Claude Code uses, identified by
the User-Agent `claude-switch`: `POST https://platform.claude.com/v1/oauth/token` with `grant_type=refresh_token` and
Claude Code's public client id.
It never renews the active login, because your running sessions own that one. A renewal rotates the refresh token
and claude-switch stores the new one right away (and keeps the previous copy in `history/`).

**Nothing renews on its own.** `list`, `use` and plain `auto` never call the token endpoint. Only `refresh`,
`keepalive` and `auto --refresh` do, and only when you run them. Read "Terms and risk" before you schedule
`keepalive`.

Usage readings come from `GET https://api.anthropic.com/api/oauth/usage`, the read-only endpoint behind Claude
Code's `/usage` screen, called with each account's own access token, including the accounts you are not using.
Readings are cached for 2 minutes.

### Scheduling keepalive

Every 3 hours keeps inactive accounts readable. The log is `~/.claude-switch/keepalive.log`; it never contains a
token.

Windows (Task Scheduler). Run these in Command Prompt (`cmd`), which fills in `%USERPROFILE%`. With pipx:

```
schtasks /Create /TN "claude-switch keepalive" /SC HOURLY /MO 3 /F /TR "%USERPROFILE%\.local\bin\claude-switch.exe keepalive"
schtasks /Delete /TN "claude-switch keepalive" /F
```

That command opens a console window for a moment every 3 hours. To run with no window, start the script with
`pythonw.exe` instead. `where pythonw` prints its full path. For a user named `alex` with Python 3.12 and the script
saved in `C:\Users\alex\tools\`, the task is:

```
schtasks /Create /TN "claude-switch keepalive" /SC HOURLY /MO 3 /F /TR "\"C:\Users\alex\AppData\Local\Programs\Python\Python312\pythonw.exe\" \"C:\Users\alex\tools\claude_switch.py\" keepalive"
```

Linux (cron, `crontab -e`):

```
0 */3 * * * $HOME/.local/bin/claude-switch keepalive
```

macOS (launchd): save as `~/Library/LaunchAgents/local.claude-switch.keepalive.plist`, then
`launchctl load ~/Library/LaunchAgents/local.claude-switch.keepalive.plist`. launchd does not expand `~` or `$HOME`,
so the program path must be absolute: run `which claude-switch` and use what it prints. The example below shows the
usual pipx location for a macOS user named `alex`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.claude-switch.keepalive</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/alex/.local/bin/claude-switch</string>
    <string>keepalive</string>
  </array>
  <key>StartInterval</key><integer>10800</integer>
</dict>
</plist>
```

## Shell shortcuts

`claude-switch init SHELL` prints short functions: `claude-accounts`, `claude-use`, `claude-auto`, `claude-add`,
`claude-save`, `claude-sync`, `claude-refresh`, `claude-help`. It never edits your profile; you add one line.

```
# ~/.bashrc or ~/.zshrc
eval "$(claude-switch init bash)"      # or: init zsh

# PowerShell $PROFILE
claude-switch init powershell | Out-String | Invoke-Expression
```

## Several claude processes

Each running `claude` keeps its login in memory. After a switch, open sessions go on with the old account until you
`/exit` and start again (`claude -c` resumes the same conversation). `use` and `list` tell you how many `claude`
processes are running.

A session that keeps running on the old account will renew that login when its access token ages. claude-switch
saves the newest copy the next time you run any command while that account is active. If you switched away and the
old session renews afterwards, the saved copy of that account is now dead: `list` shows DEAD after the next
`refresh`, and the repair steps fix it. Close old sessions after a switch to avoid this.

## Platform support

- **Windows:** the author's daily use, with Claude Code 2.1.316. CI runs the tests too.
- **Linux:** CI with a fake profile, a fake token server and Python 3.9 to 3.15.
- **macOS:** uses the login Keychain through Apple's `/usr/bin/security`. Writes are read back and checked; a locked
  Keychain (for example over SSH) stops the command with a clear message instead of falling back. CI runs the
  Keychain code against a fake `security` tool on every platform. The author has not tested it on a real Mac yet;
  reports are welcome.
- `CLAUDE_CONFIG_DIR` profiles are not supported. claude-switch manages the default profile and refuses to run when
  that variable is set.

## Files

All in `~/.claude-switch/` (folder mode 0700, files 0600 on Linux and macOS):

| File | Holds |
|---|---|
| `accounts.json` | `{"name": "email"}` |
| `NAME.credentials.json`, `NAME.account.json` | the saved login and account details |
| `history/` | the last 3 copies of each login; copies stay after `remove` |
| `active.txt`, `meta.json`, `usage-cache.json` | which name is active, renewal dates, usage readings |
| `parked-*.credentials.json` | logins set aside by `add` or because no name matched; delete them once you no longer need them |
| `rescue-*.credentials.json` | a renewed login that could not be written to its usual file; copy it over that file |
| `removed/` | the files of accounts you removed |
| `keepalive.log` | one line per keepalive run, failures included |
| `claude.json.bak` | `~/.claude.json` as it was before the last switch |

## Troubleshooting

| You see | Do this |
|---|---|
| `claude` asks you to log in after a switch | The saved login was renewed elsewhere after you saved it. Repair: `claude-switch add NAME`, browser, `claude auth login`, `claude-switch save NAME`. |
| `DEAD since ...` | Same repair. |
| `LOGIN ENDS SOON` | Same repair, before the date. |
| `unknown, refresh needed` | The saved access token expired. `claude-switch refresh NAME` (opt-in), or switch to it. |
| `the login now is X, not Y` | You are logged in as another account than the name you gave. Use the right name, or `add` first. |
| `already saved as OTHER` | This login already has a name. Use that name. |
| `is OTHER's saved login, not NAME's` | `~/.claude.json` and the login disagree. `claude-switch use OTHER` puts them back in line. |
| `SAME LOGIN AS ...` | Two names hold one login. Repair one of them. |
| `CLAUDE_CODE_OAUTH_TOKEN is set` | That variable overrides every login. Remove it from your shell and your user environment. |
| `busy: ... is held by another process` | Claude Code is renewing or writing the login right now. Try again in a minute. A crashed process's lock expires by itself. |
| `differs from the saved account ... only in case` | Use the existing name as written in `list`, or pick another name. |
| `keeps this login in Windows Credential Manager` | Claude Code moved its Windows login out of the file claude-switch swaps. claude-switch stopped without changing anything. Please open an issue. |
| macOS: `could not read the login from the macOS Keychain` | Unlock the login keychain (`security unlock-keychain`), then retry. |

## What leaves your machine

claude-switch talks to two Anthropic addresses over HTTPS, identifies itself with the User-Agent `claude-switch`, and
follows no redirects:

- `GET https://api.anthropic.com/api/oauth/usage` from `list`, `auto` and `refresh`, with each saved account's access
  token, at most once per account every 2 minutes.
- `POST https://platform.claude.com/v1/oauth/token` from `refresh`, `keepalive` and `auto --refresh` only, with that
  account's saved refresh token.

`use`, `add`, `save`, `sync`, `remove`, `init` and `help` make no network calls. Anthropic can see requests for
several accounts coming from one machine. No telemetry.

## Uninstall

1. `claude-switch use NAME` for the account you want to keep. Claude Code stays on it.
2. Delete any keepalive task: `schtasks /Delete /TN "claude-switch keepalive" /F`, your `crontab -e` line, or
   `launchctl unload` plus the plist.
3. `pipx uninstall claude-account-switch`, and remove the shortcut line from your shell profile.
4. Delete `~/.claude-switch/`. It holds working copies of your other logins.

claude-switch changes only the login (`~/.claude/.credentials.json` or the Keychain item) and `oauthAccount` in
`~/.claude.json`. `~/.claude-switch/claude.json.bak` holds `~/.claude.json` from before the last switch.

## Terms and risk

- claude-switch is **unofficial**. It is not made, endorsed or supported by Anthropic.
- It reads and writes the same files and Keychain item as Claude Code and calls the same endpoints, with Claude
  Code's public OAuth client id. Anthropic can change any of these without notice, and the tool may break.
- `refresh`, `keepalive` and `auto --refresh` send token requests from a script that is not Claude Code. They are off
  unless you run them. Decide for yourself whether that fits your use.
- `list` and `auto` read usage for accounts you are not using, from a script that is not Claude Code.
- You must follow [Anthropic's terms](https://www.anthropic.com/legal) and usage policies for every account you use.
- It is meant for one person moving between their own subscriptions. Do not share logins or tokens, and do not
  resell or pool access.
- Logins are secrets. See [SECURITY.md](SECURITY.md) for how they are stored.

## Development

```
python -m unittest discover -s tests -v
```

The tests build a fake home folder (`CLAUDE_SWITCH_TEST_HOME`), a local fake token and usage server that rotates
refresh tokens like the real one, and a fake `security` tool. They never touch your real profile, your Keychain or
the network. See [CONTRIBUTING.md](CONTRIBUTING.md) before you send a change.

## Maintainer

Abubakr Alsheikh ([@Abubakr-Alsheikh](https://github.com/Abubakr-Alsheikh)), abubakr.alsheikh@outlook.com. Questions and bug reports:
[GitHub issues](https://github.com/Abubakr-Alsheikh/claude-account-switch/issues).

## License

MIT, copyright (c) 2026 Abubakr Alsheikh. See [LICENSE](LICENSE).
