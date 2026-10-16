# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Admin CLI for the mock platform's registry. Stands in for a console.

    python -m mock_platform.admin --db reg.sqlite service add litellm-main
    python -m mock_platform.admin --db reg.sqlite user add alice@corp.example
    python -m mock_platform.admin --db reg.sqlite user role admin@corp.example admin
    python -m mock_platform.admin --db reg.sqlite key personal alice@corp.example --key-env ALICE_VIRTUAL_KEY --label alice-personal
    python -m mock_platform.admin --db reg.sqlite key app --key-env OPENWEBUI_APP_KEY --label open-webui-app --secret-env FORWARD_USER_INFO_HEADER_JWT_SECRET
    python -m mock_platform.admin --db reg.sqlite user disable alice@corp.example
    python -m mock_platform.admin --db reg.sqlite show

Keys and secrets are read from environment variables, never from the command
line, so they stay out of shell history and process listings. A key may be the
raw `sk-` value or the SHA-256 LiteLLM reports for it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .registry import Registry


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"environment variable {name} is empty")
    return value


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="mock_platform.admin")
    p.add_argument("--db", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)

    svc = sub.add_parser("service").add_subparsers(dest="action", required=True)
    s_add = svc.add_parser("add", help="register a LiteLLM deployment; prints its credential once")
    s_add.add_argument("label")

    usr = sub.add_parser("user").add_subparsers(dest="action", required=True)
    u_add = usr.add_parser("add")
    u_add.add_argument("email")
    u_add.add_argument("--project", default="default")
    u_add.add_argument("--role", choices=("user", "admin"), default="user")
    u_add.add_argument("--employee-id", help="the person's employee ID; also their org")
    for action in ("enable", "disable"):
        usr.add_parser(action).add_argument("email")
    u_role = usr.add_parser("role", help="set a person's platform role")
    u_role.add_argument("email")
    u_role.add_argument("role", choices=("user", "admin"))

    key = sub.add_parser("key").add_subparsers(dest="action", required=True)
    k_personal = key.add_parser("personal", help="link a person's own virtual key")
    k_personal.add_argument("email")
    k_personal.add_argument("--key-env", required=True)
    k_personal.add_argument("--label", required=True)
    k_app = key.add_parser("app", help="register a shared application key")
    k_app.add_argument("--key-env", required=True)
    k_app.add_argument("--label", required=True)
    k_app.add_argument("--secret-env", required=True, help="the app's token signing secret")
    k_app.add_argument("--issuer", default="open-webui")
    k_rm = key.add_parser("remove")
    k_rm.add_argument("--key-env", required=True)

    sub.add_parser("show")

    a = p.parse_args(argv)
    reg = Registry(a.db)

    if a.cmd == "service":
        credential = reg.add_service(a.label)
        print(credential)  # shown once; only its hash is kept
    elif a.cmd == "user" and a.action == "add":
        print(json.dumps(reg.add_user(a.email, a.project, a.role, a.employee_id).__dict__))
    elif a.cmd == "user" and a.action == "role":
        print(json.dumps(reg.set_role(a.email, a.role).__dict__))
    elif a.cmd == "user":
        print(json.dumps(reg.set_enabled(a.email, a.action == "enable").__dict__))
    elif a.cmd == "key" and a.action == "personal":
        k = reg.link_personal_key(a.email, _env(a.key_env), a.label)
        print(f"{k.label}: personal key of {k.user_id} ({k.key_hash[:12]}…)")
    elif a.cmd == "key" and a.action == "app":
        k = reg.add_app_key(_env(a.key_env), a.label, _env(a.secret_env), a.issuer)
        print(f"{k.label}: app key ({k.key_hash[:12]}…), tokens from issuer {a.issuer!r}")
    elif a.cmd == "key" and a.action == "remove":
        reg.unlink_key(_env(a.key_env))
        print("removed")
    elif a.cmd == "show":
        print(json.dumps(reg.dump(), indent=2))


if __name__ == "__main__":
    main()
