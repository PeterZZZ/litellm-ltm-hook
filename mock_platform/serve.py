# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Run the mock platform: the plain listener, and a mutual-TLS one for LiteLLMs.

    python -m mock_platform.serve --host 127.0.0.1 --port 8100

The plain listener is what browsers use (the portal, its SSO sign-in). With
MOCK_PLATFORM_MTLS_PORT set, a second listener on that port serves the same app
over TLS and requires a client certificate signed by MOCK_PLATFORM_CLIENT_CA;
the app then answers the memory API only there, and only when the
certificate's common name is the service label of the caller's credential
(authorize.py, rule 1). Certificates: certs.py.

    MOCK_PLATFORM_MTLS_PORT     e.g. 8443; unset = no mutual TLS (as before)
    MOCK_PLATFORM_TLS_CERT      the platform's certificate (certs.py server)
    MOCK_PLATFORM_TLS_KEY       its key
    MOCK_PLATFORM_CLIENT_CA     the CA that signs LiteLLMs' certificates (ca.crt)

Both listeners share one app, so one registry, one reconcile loop. The rest of
the settings are app.py's.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import ssl
from typing import Optional

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from .authorize import CLIENT_CERT_SCOPE


class ClientCertH11(H11Protocol):
    """h11, with the verified client certificate's common name in every request's
    scope. uvicorn checks the certificate but does not pass it on to the app."""

    def connection_made(self, transport) -> None:
        super().connection_made(transport)
        name = common_name(transport.get_extra_info("peercert"))
        app = self.app

        async def with_cert(scope, receive, send):
            await app({**scope, CLIENT_CERT_SCOPE: name}, receive, send)

        self.app = with_cert


def common_name(peercert) -> Optional[str]:
    """The subject's common name in ssl's getpeercert() form; None without one."""
    for rdn in (peercert or {}).get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None


def mtls_config(app, host: str, port: int, certfile: str, keyfile: str, client_ca: str,
                **kwargs) -> uvicorn.Config:
    return uvicorn.Config(app, host=host, port=port, http=ClientCertH11, lifespan="off",
                          ssl_certfile=certfile, ssl_keyfile=keyfile, ssl_ca_certs=client_ca,
                          ssl_cert_reqs=ssl.CERT_REQUIRED, **kwargs)


async def serve(plain: uvicorn.Server, mtls: Optional[uvicorn.Server]) -> None:
    """Run both; the plain server takes the signals and stops the other with it."""
    if mtls is None:
        await plain.serve()
        return
    mtls.capture_signals = contextlib.nullcontext
    other = asyncio.create_task(mtls.serve())
    try:
        await plain.serve()
    finally:
        mtls.should_exit = True
        await other


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="mock_platform.serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8100)
    a = p.parse_args(argv)

    from .app import app
    if app is None:
        raise SystemExit("MOCK_PLATFORM_DB is required (see mock_platform/app.py)")
    plain = uvicorn.Server(uvicorn.Config(app, host=a.host, port=a.port))
    mtls = None
    port = os.environ.get("MOCK_PLATFORM_MTLS_PORT", "").strip()
    if port:
        missing = [n for n in ("MOCK_PLATFORM_TLS_CERT", "MOCK_PLATFORM_TLS_KEY", "MOCK_PLATFORM_CLIENT_CA")
                   if not os.environ.get(n, "").strip()]
        if missing:
            raise SystemExit(f"MOCK_PLATFORM_MTLS_PORT needs {', '.join(missing)}")
        config = mtls_config(app, a.host, int(port), os.environ["MOCK_PLATFORM_TLS_CERT"],
                             os.environ["MOCK_PLATFORM_TLS_KEY"], os.environ["MOCK_PLATFORM_CLIENT_CA"])
        config.load()  # a bad certificate or key stops the platform here, before anything listens
        mtls = uvicorn.Server(config)
    asyncio.run(serve(plain, mtls))


if __name__ == "__main__":
    main()
