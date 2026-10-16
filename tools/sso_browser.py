# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""A minimal headless browser for the mock Keycloak login, for scripted checks.

    opener, url = sso_login("http://127.0.0.1:8087/oauth/oidc/login", email, password)

Follows the app's redirect to Keycloak, fills in the login form, and follows the
redirects back to the app. Returns the opener (holding the app's cookies) and the
final URL. Raises if Keycloak shows its login page again (wrong password).
"""
import html
import http.cookiejar
import re
import urllib.parse
import urllib.request


class _LocalhostJar(http.cookiejar.CookieJar):
    """Keycloak marks its cookies Secure; browsers still send those to http://localhost."""

    def add_cookie_header(self, request):
        for c in self:
            c.secure = False
        super().add_cookie_header(request)


def _opener():
    jar = _LocalhostJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def sso_login(start_url, email, password, timeout=60):
    opener, jar = _opener()
    with opener.open(start_url, timeout=timeout) as r:
        page, at = r.read().decode(), r.geturl()
    m = re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', page)
    if not m:
        raise RuntimeError(f"no Keycloak login form at {at}")
    action = html.unescape(m.group(1))
    body = urllib.parse.urlencode({"username": email, "password": password, "credentialId": ""}).encode()
    with opener.open(urllib.request.Request(action, data=body), timeout=timeout) as r:
        page, at = r.read().decode(errors="replace"), r.geturl()
    if 'id="kc-form-login"' in page:
        raise RuntimeError(f"Keycloak refused the login for {email}")
    opener.jar = jar
    return opener, at


def cookie(opener, name):
    return next((c.value for c in opener.jar if c.name == name), None)
