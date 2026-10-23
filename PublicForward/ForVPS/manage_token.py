#!/usr/bin/env python3
"""
PublicForward/ForVPS/manage_token.py — manage gateway API tokens.

Every client of the PAF-Model gateway must send a valid token as
`Authorization: Bearer <token>`. It is separate from PAF_TOKEN (worker auth).

Usage:
  python manage_token.py create <name> [--expires 7d] [--token VALUE]
  python manage_token.py list [--reveal]
  python manage_token.py reveal <name>
  python manage_token.py expire <name> 7d|never
  python manage_token.py revoke <name>

Legacy single-token compatibility:
  python manage_token.py generate [--force] [--expires 7d]
  python manage_token.py show [--reveal]
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

import paf_auth


def _restart_hint() -> None:
    print("\nA RUNNING gateway keeps its old tokens in memory unless changed via its "
          "interactive console. In that console, type `token create <name>` / `token revoke <name>` "
          "for instant zero-restart updates.")


def _warn_if_overridden() -> None:
    if os.environ.get(paf_auth.ENV_VAR, "").strip():
        print(f"\nNOTE: {paf_auth.ENV_VAR} is set in this environment and adds "
              "its own token(s) alongside file tokens.")


def cmd_create(args: argparse.Namespace) -> int:
    name = args.name.strip()
    try:
        info, path = paf_auth.create_token(
            name=name,
            expiry_text=args.expires,
            token_value=args.token,
        )
    except Exception as exc:
        print(f"Error creating token: {exc}", file=sys.stderr)
        return 1

    print(f"New API token created for '{info.name}' (saved to {path}):\n")
    print(f"  {info.token}\n")
    print(f"Token {info.describe()}.\n")
    print("Clients send it as:")
    print("  Authorization: Bearer <token>")
    _warn_if_overridden()
    _restart_hint()
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    """Legacy generate: defaults to 'default' name."""
    tokens = paf_auth.read_tokens_file()
    name = args.name or "default"
    if name in [t.name for t in tokens.values()] and not args.force:
        print(f"A token with name '{name}' already exists.\n"
              "Use 'create <name>' with a different name or --force to replace it.",
              file=sys.stderr)
        return 1
    if name in [t.name for t in tokens.values()]:
        paf_auth.revoke_token(name)
    args.name = name
    args.token = None
    return cmd_create(args)


def cmd_list(args: argparse.Namespace) -> int:
    tokens = paf_auth.load_all_tokens()
    if not tokens:
        print(f"No API tokens configured in {paf_auth.TOKENS_FILE}.\n"
              "Run: python manage_token.py create <name>", file=sys.stderr)
        return 1

    lines = [
        f"{'NAME':<16} {'SOURCE':<8} {'STATUS':<10} {'EXPIRES AT':<28} {'TOKEN'}",
        "-" * 80,
    ]
    for tok, info in tokens.items():
        status = "EXPIRED" if info.is_expired() else "active"
        exp = info.describe()
        disp_token = tok if getattr(args, "reveal", False) else paf_auth.mask_token(tok)
        lines.append(f"{info.name:<16} {info.source:<8} {status:<10} {exp:<28} {disp_token}")

    print("\n".join(lines))
    if not getattr(args, "reveal", False):
        print("\n(Use --reveal or 'manage_token.py reveal <name>' to print full tokens)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    tokens = paf_auth.load_all_tokens()
    if not tokens:
        print("No API tokens configured. Run: python manage_token.py create <name>", file=sys.stderr)
        return 1
    if args.name:
        info = paf_auth.find_token_by_name(args.name, tokens)
        if not info:
            print(f"Token '{args.name}' not found.", file=sys.stderr)
            return 1
        disp_token = info.token if args.reveal else paf_auth.mask_token(info.token)
        print(f"Name   : {info.name}")
        print(f"Source : {info.source}")
        print(f"Token  : {disp_token}")
        print(f"Status : {info.describe()}")
        return 0
    return cmd_list(args)


def cmd_reveal(args: argparse.Namespace) -> int:
    tokens = paf_auth.load_all_tokens()
    info = paf_auth.find_token_by_name(args.name, tokens)
    if not info:
        print(f"Token '{args.name}' not found.", file=sys.stderr)
        return 1
    print(f"Token for '{info.name}':\n\n  {info.token}\n\nStatus: {info.describe()}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    name = args.name.strip()
    token = args.token
    if not token:
        token = getpass.getpass(f"Enter API token for '{name}': ").strip()
        confirm = getpass.getpass("Repeat API token: ").strip()
        if token != confirm:
            print("Tokens do not match.", file=sys.stderr)
            return 1
    try:
        info, path = paf_auth.create_token(name, args.expires, token_value=token)
        print(f"API token saved for '{info.name}' ({paf_auth.mask_token(info.token)}), {info.describe()}")
        _warn_if_overridden()
        _restart_hint()
        return 0
    except Exception as exc:
        print(f"Error setting token: {exc}", file=sys.stderr)
        return 1


def cmd_expire(args: argparse.Namespace) -> int:
    try:
        updated = paf_auth.update_token_expiry(args.name, args.expiry)
        if not updated:
            print(f"Token '{args.name}' not found in {paf_auth.TOKENS_FILE}.", file=sys.stderr)
            return 1
        print(f"Expiry updated: token '{args.name}' {updated.describe()}.")
        _warn_if_overridden()
        _restart_hint()
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_revoke(args: argparse.Namespace) -> int:
    removed = paf_auth.revoke_token(args.name)
    if not removed:
        print(f"Token '{args.name}' not found in {paf_auth.TOKENS_FILE}.", file=sys.stderr)
        return 1
    print(f"Token '{args.name}' revoked.")
    _warn_if_overridden()
    _restart_hint()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Manage PAF-Model gateway API tokens")
    sub = p.add_subparsers(dest="command", required=True)

    # create
    c = sub.add_parser("create", help="create a new named API token")
    c.add_argument("name", help="friendly label / client identifier (e.g. alice, bot-1)")
    c.add_argument("--expires", metavar="EXPIRY", help="token lifetime (e.g. 30m, 12h, 7d, 2w, or never)")
    c.add_argument("--token", help="custom token string (optional; auto-generated if omitted)")
    c.set_defaults(func=cmd_create)

    # list
    ls = sub.add_parser("list", help="list all API tokens")
    ls.add_argument("--reveal", action="store_true", help="print full unmasked tokens")
    ls.set_defaults(func=cmd_list)

    # reveal
    rv = sub.add_parser("reveal", help="show full token for a specific name")
    rv.add_argument("name", help="token name/label")
    rv.set_defaults(func=cmd_reveal)

    # expire
    e = sub.add_parser("expire", help="change expiry of a token")
    e.add_argument("name", help="token name/label")
    e.add_argument("expiry", help="e.g. 30m, 12h, 7d, 2w, or 'never'")
    e.set_defaults(func=cmd_expire)

    # revoke
    r = sub.add_parser("revoke", help="delete a token by name")
    r.add_argument("name", help="token name/label to revoke")
    r.set_defaults(func=cmd_revoke)

    # legacy compatibility: generate
    g = sub.add_parser("generate", help="legacy: generate token (defaults to name 'default')")
    g.add_argument("--name", default="default", help="token name (default: 'default')")
    g.add_argument("--force", action="store_true", help="replace existing token with same name")
    g.add_argument("--expires", metavar="EXPIRY", help="token lifetime")
    g.set_defaults(func=cmd_generate)

    # legacy compatibility: show
    sh = sub.add_parser("show", help="show token(s)")
    sh.add_argument("--name", help="specific token name")
    sh.add_argument("--reveal", action="store_true", help="print unmasked token")
    sh.set_defaults(func=cmd_show)

    # legacy compatibility: set
    s = sub.add_parser("set", help="set a custom token value")
    s.add_argument("name", help="token name/label")
    s.add_argument("token", nargs="?", help="token value (prompted if omitted)")
    s.add_argument("--expires", metavar="EXPIRY", help="token lifetime")
    s.set_defaults(func=cmd_set)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
