#!/usr/bin/env python3
"""
PublicForward/ForVPS/manage_token.py — manage the single shared API token.

Every client of the PAF-Model gateway must send this token as
`Authorization: Bearer <token>`. It is separate from PAF_TOKEN (worker auth).

Usage:
  python manage_token.py generate [--force]   # create a random token and save it
  python manage_token.py set [TOKEN]          # save your own token (prompts if omitted)
  python manage_token.py show [--reveal]      # show status (masked unless --reveal)
  python manage_token.py revoke                # delete the saved token file

The token is stored in `.api_token` next to vps_server.py (chmod 600, git-ignored).
This script edits the token FILE; a running gateway reads it only at startup.
To change the token on a running gateway without a restart, type
`token generate | rotate | set <value> | revoke` in that gateway's console.
(Note: the PAF_API_TOKEN env var and the --api-token flag override the file.)
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

import paf_auth


def _restart_hint() -> None:
    print("\nA RUNNING gateway keeps its old token until restarted. To change the "
          "token without a restart, type `token generate` / `token rotate` / "
          "`token set <value>` in the gateway's own console instead.")


def cmd_generate(args: argparse.Namespace) -> int:
    if paf_auth.TOKEN_FILE.exists() and not args.force:
        print(f"A token already exists at {paf_auth.TOKEN_FILE}.\n"
              "Use --force to replace it (all clients using the old token will "
              "stop working).", file=sys.stderr)
        return 1
    token = paf_auth.generate_token()
    path = paf_auth.save_token(token)
    print(f"New API token saved to {path}\n")
    print(f"  {token}\n")
    print("Share it only with people allowed to use the API. Clients send it as:")
    print("  Authorization: Bearer <token>")
    _warn_if_overridden()
    _restart_hint()
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    token = args.token
    if not token:
        token = getpass.getpass("Enter API token: ").strip()
        confirm = getpass.getpass("Repeat API token: ").strip()
        if token != confirm:
            print("Tokens do not match.", file=sys.stderr)
            return 1
    err = paf_auth.validate_token_format(token)
    if err:
        print(f"Invalid token: {err}", file=sys.stderr)
        return 1
    path = paf_auth.save_token(token)
    print(f"API token saved to {path} ({paf_auth.mask_token(token)})")
    _warn_if_overridden()
    _restart_hint()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    token, source = paf_auth.load_token()
    if not token:
        print("No API token configured (every request gets 503; without an "
              "interactive terminal the gateway refuses to start).\n"
              "Run: python manage_token.py generate  (or type `token generate` "
              "in the running gateway's console)", file=sys.stderr)
        return 1
    labels = {
        "env": f"environment variable {paf_auth.ENV_VAR}",
        "file": f"file {paf_auth.TOKEN_FILE}",
    }
    print(f"Source : {labels.get(source, source)}")
    print(f"Token  : {token if args.reveal else paf_auth.mask_token(token)}")
    if not args.reveal:
        print("(use --reveal to print the full token)")
    return 0


def cmd_revoke(_: argparse.Namespace) -> int:
    removed = paf_auth.delete_token_file()
    print("Token file deleted." if removed else "No token file found.")
    _warn_if_overridden()
    print("Without a token the gateway refuses to start (or rejects every request "
          "if it is already running with the old one until restarted).")
    _restart_hint()
    return 0


def _warn_if_overridden() -> None:
    if os.environ.get(paf_auth.ENV_VAR, "").strip():
        print(f"\nWARNING: {paf_auth.ENV_VAR} is set in this environment and "
              "overrides the token file.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Manage the PAF-Model gateway API token")
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="generate a new random token and save it")
    g.add_argument("--force", action="store_true", help="replace an existing token")
    g.set_defaults(func=cmd_generate)

    s = sub.add_parser("set", help="save a token of your choice")
    s.add_argument("token", nargs="?", help="token value (prompted if omitted)")
    s.set_defaults(func=cmd_set)

    sh = sub.add_parser("show", help="show the active token source (masked)")
    sh.add_argument("--reveal", action="store_true", help="print the full token")
    sh.set_defaults(func=cmd_show)

    r = sub.add_parser("revoke", help="delete the saved token file")
    r.set_defaults(func=cmd_revoke)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())