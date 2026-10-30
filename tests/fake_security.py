"""A stand-in for macOS /usr/bin/security, enough for claude_switch's Keychain backend.

Items live in $CLAUDE_SWITCH_HOME/fake-keychain.json as {"service|account": "value"}.
Supports find-generic-password (-w), add-generic-password -U -X, delete-generic-password, and `-i` (commands on stdin).
FAKE_SECURITY_FAIL=<exit code> makes every call fail with that code (a locked Keychain is 36 or 51).
"""
import json
import os
import shlex
import sys

DB = os.path.join(os.environ["CLAUDE_SWITCH_HOME"], "fake-keychain.json")
NOT_FOUND = 44


def load():
    try:
        with open(DB, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def store(items):
    with open(DB, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(items, fh)


def opts(args):
    out, i = {}, 0
    while i < len(args):
        a = args[i]
        if a in ("-a", "-s", "-X", "-w") and a != "-w":
            out[a] = args[i + 1]
            i += 2
        else:
            out[a] = True
            i += 1
    return out


def run(args):
    if not args:
        return 2
    cmd, o = args[0], opts(args[1:])
    key = f"{o.get('-s', '')}|{o.get('-a', '')}"
    items = load()
    if cmd == "find-generic-password":
        if key not in items:
            sys.stderr.write("security: SecKeychainSearchCopyNext: The specified item could not be found.\n")
            return NOT_FOUND
        if o.get("-w"):
            sys.stdout.write(items[key] + "\n")
        return 0
    if cmd == "add-generic-password":
        if key in items and not o.get("-U"):
            return 45
        items[key] = bytes.fromhex(o["-X"]).decode("utf-8")
        store(items)
        return 0
    if cmd == "delete-generic-password":
        if key not in items:
            return NOT_FOUND
        del items[key]
        store(items)
        return 0
    return 2


def main():
    fail = os.environ.get("FAKE_SECURITY_FAIL")
    if fail:
        sys.stderr.write("security: The user name or passphrase you entered is not correct.\n")
        return int(fail)
    args = sys.argv[1:]
    if args == ["-i"]:
        code = 0
        for line in sys.stdin.read().splitlines():
            if line.strip():
                code = run(shlex.split(line)) or code
        return code
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
