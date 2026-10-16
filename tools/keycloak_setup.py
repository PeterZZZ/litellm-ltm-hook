# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Create the mock SSO realm in a local Keycloak: one sign-in for Open WebUI and the platform.

Idempotent: run it again and it updates what exists.

    KEYCLOAK_URL=http://127.0.0.1:8180 KEYCLOAK_ADMIN=... KEYCLOAK_ADMIN_PASSWORD=... \
    ACCOUNTS_FILE=/path/accounts.txt SECRETS_OUT=/path/keycloak-clients.env \
    python3 tools/keycloak_setup.py

What it makes, in realm "corp":
  - a user-profile attribute employee_id that only admins can edit;
  - client "open-webui" (Open WebUI's OIDC login) and client "memmachine-platform"
    (the portal's login, plus a service account that may read users, for the
    platform's employee-ID lookups); both get an employee_id claim in their tokens;
  - the users in USERS, with the passwords from ACCOUNTS_FILE ("email password" lines).

Client secrets are generated once and written to SECRETS_OUT (mode 600); an existing
file is reused so reruns keep the same secrets.
"""
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

KC = os.environ.get("KEYCLOAK_URL", "http://127.0.0.1:8180").rstrip("/")
REALM = os.environ.get("KEYCLOAK_REALM", "corp")
OWUI_URLS = os.environ.get("OWUI_URLS", "http://127.0.0.1:8087,http://localhost:8087").split(",")
PORTAL_URLS = os.environ.get("PORTAL_URLS", "http://127.0.0.1:8100,http://localhost:8100").split(",")

# email -> (first name, last name, employee ID)
USERS = {
    "admin@corp.example": ("Platform", "Admin", "E10000"),
    "alice@corp.example": ("Alice", "Kim", "E12345"),
    "bob@corp.example": ("Bob", "Lee", "E67890"),
    "carol@corp.example": ("Carol", "Park", "E24680"),
}


def call(method, path, body=None, token=None, form=None):
    data, headers = None, {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{KC}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300], e.headers


def admin_token():
    s, body, _ = call("POST", "/realms/master/protocol/openid-connect/token", form={
        "grant_type": "password", "client_id": "admin-cli",
        "username": os.environ["KEYCLOAK_ADMIN"], "password": os.environ["KEYCLOAK_ADMIN_PASSWORD"]})
    if s != 200:
        sys.exit(f"admin login failed: {s} {body}")
    return body["access_token"]


def ensure_realm(t):
    s, _, _ = call("GET", f"/admin/realms/{REALM}", token=t)
    if s == 404:
        s, body, _ = call("POST", "/admin/realms", {"realm": REALM, "enabled": True,
                                                    "displayName": "Corp SSO (mock)",
                                                    "loginWithEmailAllowed": True}, t)
        assert s == 201, (s, body)
        print(f"realm {REALM}: created")
    else:
        print(f"realm {REALM}: exists")


def ensure_profile_attribute(t):
    _, profile, _ = call("GET", f"/admin/realms/{REALM}/users/profile", token=t)
    attrs = [a for a in profile["attributes"] if a["name"] != "employee_id"]
    attrs.append({
        "name": "employee_id", "displayName": "Employee ID",
        "permissions": {"view": ["admin", "user"], "edit": ["admin"]},
        "validations": {"pattern": {"pattern": "^E[0-9]{5}$", "error-message": "E followed by 5 digits"}},
        "multivalued": False,
    })
    profile["attributes"] = attrs
    s, body, _ = call("PUT", f"/admin/realms/{REALM}/users/profile", profile, t)
    assert s == 200, (s, body)
    print("user profile: employee_id (admin-only edit)")


MAPPER = {
    "name": "employee_id", "protocol": "openid-connect",
    "protocolMapper": "oidc-usermodel-attribute-mapper",
    "config": {"user.attribute": "employee_id", "claim.name": "employee_id",
               "jsonType.label": "String", "id.token.claim": "true",
               "access.token.claim": "true", "userinfo.token.claim": "true"},
}


def ensure_client(t, client_id, secret, base_urls, callback, service_account=False):
    rep = {
        "clientId": client_id, "enabled": True, "protocol": "openid-connect",
        "publicClient": False, "secret": secret, "standardFlowEnabled": True,
        "directAccessGrantsEnabled": False, "serviceAccountsEnabled": service_account,
        "redirectUris": [u.rstrip("/") + callback for u in base_urls],
        "webOrigins": [u.rstrip("/") for u in base_urls],
        "attributes": {"post.logout.redirect.uris": "##".join(u.rstrip("/") + "/*" for u in base_urls)},
        "protocolMappers": [MAPPER],
    }
    _, found, _ = call("GET", f"/admin/realms/{REALM}/clients?clientId={client_id}", token=t)
    if found:
        cid = found[0]["id"]
        rep.pop("protocolMappers")
        s, body, _ = call("PUT", f"/admin/realms/{REALM}/clients/{cid}", {**found[0], **rep}, t)
        assert s == 204, (s, body)
        _, mappers, _ = call("GET", f"/admin/realms/{REALM}/clients/{cid}/protocol-mappers/models", token=t)
        if not any(m["name"] == "employee_id" for m in mappers):
            call("POST", f"/admin/realms/{REALM}/clients/{cid}/protocol-mappers/models", MAPPER, t)
        print(f"client {client_id}: updated")
    else:
        s, body, _ = call("POST", f"/admin/realms/{REALM}/clients", rep, t)
        assert s == 201, (s, body)
        _, found, _ = call("GET", f"/admin/realms/{REALM}/clients?clientId={client_id}", token=t)
        cid = found[0]["id"]
        print(f"client {client_id}: created")
    return cid


def grant_view_users(t, client_uuid):
    _, sa, _ = call("GET", f"/admin/realms/{REALM}/clients/{client_uuid}/service-account-user", token=t)
    _, rm, _ = call("GET", f"/admin/realms/{REALM}/clients?clientId=realm-management", token=t)
    rm_id = rm[0]["id"]
    _, role, _ = call("GET", f"/admin/realms/{REALM}/clients/{rm_id}/roles/view-users", token=t)
    s, body, _ = call("POST", f"/admin/realms/{REALM}/users/{sa['id']}/role-mappings/clients/{rm_id}", [role], t)
    assert s == 204, (s, body)
    print("memmachine-platform service account: realm-management/view-users")


def ensure_user(t, email, first, last, employee_id, password):
    rep = {"username": email, "email": email, "emailVerified": True, "enabled": True,
           "firstName": first, "lastName": last, "attributes": {"employee_id": [employee_id]}}
    _, found, _ = call("GET", f"/admin/realms/{REALM}/users?email={urllib.parse.quote(email)}&exact=true", token=t)
    if found:
        uid = found[0]["id"]
        s, body, _ = call("PUT", f"/admin/realms/{REALM}/users/{uid}", rep, t)
        assert s == 204, (s, body)
    else:
        s, body, headers = call("POST", f"/admin/realms/{REALM}/users", rep, t)
        assert s == 201, (s, body)
        uid = headers["Location"].rsplit("/", 1)[1]
    s, body, _ = call("PUT", f"/admin/realms/{REALM}/users/{uid}/reset-password",
                      {"type": "password", "value": password, "temporary": False}, t)
    assert s == 204, (s, body)
    print(f"user {email}: {employee_id}")


def load_secrets(path):
    have = {}
    if os.path.exists(path):
        have = dict(line.strip().split("=", 1) for line in open(path) if "=" in line)
    for name in ("OWUI_OIDC_CLIENT_SECRET", "PLATFORM_OIDC_CLIENT_SECRET"):
        have.setdefault(name, secrets.token_urlsafe(32))
    old = os.umask(0o077)
    try:
        with open(path, "w") as f:
            f.writelines(f"{k}={v}\n" for k, v in have.items())
    finally:
        os.umask(old)
    return have


def main():
    passwords = dict(line.split() for line in open(os.environ["ACCOUNTS_FILE"]) if "@" in line)
    sec = load_secrets(os.environ["SECRETS_OUT"])
    t = admin_token()
    ensure_realm(t)
    ensure_profile_attribute(t)
    ensure_client(t, "open-webui", sec["OWUI_OIDC_CLIENT_SECRET"], OWUI_URLS, "/oauth/oidc/callback")
    pid = ensure_client(t, "memmachine-platform", sec["PLATFORM_OIDC_CLIENT_SECRET"], PORTAL_URLS,
                        "/portal/auth/callback", service_account=True)
    grant_view_users(t, pid)
    for email, (first, last, emp) in USERS.items():
        ensure_user(t, email, first, last, emp, passwords[email])


if __name__ == "__main__":
    main()
