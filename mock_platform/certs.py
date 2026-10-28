# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""A small certificate authority for mutual TLS between LiteLLM and the platform.

    python -m mock_platform.certs --dir PKI ca
    python -m mock_platform.certs --dir PKI server 127.0.0.1 [platform.internal ...]
    python -m mock_platform.certs --dir PKI sign litellm-main.csr --db registry.sqlite
    python -m mock_platform.certs --dir PKI client litellm-main

ca            ca.crt, ca.key. The platform accepts client certificates it signed;
              LiteLLM checks the platform's certificate against ca.crt.
server NAME.. platform.crt, platform.key for the platform's mutual-TLS listener;
              each NAME (a DNS name or an IP address) is one LiteLLM dials.
sign CSR      LABEL.crt for one LiteLLM deployment, from the signing request its
              operator made on the LiteLLM host, so its key never leaves there:
                openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
                  -keyout LABEL.key -out LABEL.csr -subj "/CN=LABEL"
              LABEL, the request's common name, must be a service in the registry
              (`mock_platform.admin service add LABEL`): the platform accepts the
              certificate only with that service's credential. Only the name and
              the public key are taken from the request; the CA sets the rest.
client LABEL  LABEL.crt and LABEL.key made here, key and all: quicker for a test
              stack, but the key then has to be copied to the LiteLLM host.

Keys are written mode 0600 and nothing is overwritten: issuing again means
removing the old files first. There is no revocation list; removing the
service from the registry is what stops a leaked certificate.
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import os
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_DAYS = 3650
LEAF_DAYS = 365


def make_ca(pki: Path, name: str = "MemMachine mock platform CA") -> Path:
    """ca.crt and ca.key in `pki`; returns ca.crt."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (
        _builder(subject, subject, key.public_key(), CA_DAYS)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .sign(key, hashes.SHA256())
    )
    _write(pki, "ca", cert, key)
    return pki / "ca.crt"


def issue_server(pki: Path, names: list[str]) -> Path:
    """platform.crt and platform.key, valid for every name in `names`."""
    sans = [_san(n) for n in names]
    return _issue(pki, "platform", names[0], ExtendedKeyUsageOID.SERVER_AUTH,
                  x509.SubjectAlternativeName(sans))


def issue_client(pki: Path, label: str) -> Path:
    """LABEL.crt and LABEL.key: the certificate of the LiteLLM registered as LABEL."""
    _check_label(label)
    return _issue(pki, label, label, ExtendedKeyUsageOID.CLIENT_AUTH)


def sign_client(pki: Path, csr_pem: bytes, services: list[str], out: Path) -> Path:
    """LABEL.crt in `out` for a LiteLLM's signing request, LABEL being its common
    name; refused unless LABEL is one of `services` (the registry's labels)."""
    csr = x509.load_pem_x509_csr(csr_pem)
    if not csr.is_signature_valid:
        raise ValueError("the request's signature does not match its key")
    names = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(names) != 1:
        raise ValueError("the request must name exactly one common name: the service label")
    label = names[0].value
    _check_label(label)
    if label not in services:
        raise ValueError(f"{label!r} is not a service in the registry "
                         f"(mock_platform.admin service add {label})")
    key = csr.public_key()
    if not (isinstance(key, ec.EllipticCurvePublicKey) and key.curve.key_size >= 256
            or isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048):
        raise ValueError("the request's key must be EC P-256 or larger, or RSA 2048 or larger")
    return _issue(pki, label, label, ExtendedKeyUsageOID.CLIENT_AUTH, public_key=key, out=out)


def _check_label(label: str) -> None:
    if not label or "/" in label or label.startswith("."):
        raise ValueError(f"not a usable service label: {label!r}")


def _issue(pki: Path, stem: str, common_name: str, usage, san=None, public_key=None,
           out: Optional[Path] = None) -> Path:
    """Sign a certificate for `public_key`, or for a new key written beside it."""
    ca_cert = x509.load_pem_x509_certificate((pki / "ca.crt").read_bytes())
    ca_key = serialization.load_pem_private_key((pki / "ca.key").read_bytes(), password=None)
    key = None if public_key else ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    builder = (
        _builder(subject, ca_cert.subject, public_key or key.public_key(), LEAF_DAYS)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
    )
    if san is not None:
        builder = builder.add_extension(san, critical=False)
    out = out or pki
    _write(out, stem, builder.sign(ca_key, hashes.SHA256()), key)
    return out / f"{stem}.crt"


def _builder(subject, issuer, public_key, days: int) -> x509.CertificateBuilder:
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
    )


def _san(name: str):
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        return x509.DNSName(name)


def _write(pki: Path, stem: str, cert: x509.Certificate, key=None) -> None:
    """stem.crt, and stem.key when the key was made here."""
    pki.mkdir(parents=True, exist_ok=True)
    files = [(pki / f"{stem}.crt", cert.public_bytes(serialization.Encoding.PEM), 0o644)]
    if key is not None:
        files.insert(0, (pki / f"{stem}.key", key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), 0o600))
    for path, _, _ in files:
        if path.exists():
            raise FileExistsError(0, "exists", str(path))
    for path, data, mode in files:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="mock_platform.certs")
    p.add_argument("--dir", required=True, type=Path, help="where the CA and certificates live")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ca", help="create the CA")
    s = sub.add_parser("server", help="the platform's certificate")
    s.add_argument("names", nargs="+", help="DNS names or IP addresses LiteLLM uses to reach the platform")
    g = sub.add_parser("sign", help="one LiteLLM deployment's certificate, from its signing request")
    g.add_argument("csr", type=Path, help="the request (PEM); its common name is the service label")
    g.add_argument("--db", required=True, help="the platform's registry: the label must be a service there")
    g.add_argument("--out", type=Path, help="where LABEL.crt goes (default: beside the request)")
    c = sub.add_parser("client", help="one LiteLLM deployment's certificate and key, both made here")
    c.add_argument("label", help="its service label in the registry")
    a = p.parse_args(argv)
    try:
        if a.cmd == "ca":
            print(make_ca(a.dir))
        elif a.cmd == "server":
            print(issue_server(a.dir, a.names))
        elif a.cmd == "sign":
            from .registry import Registry
            services = Registry(a.db).services()
            print(sign_client(a.dir, a.csr.read_bytes(), services, a.out or a.csr.parent))
        else:
            print(issue_client(a.dir, a.label))
    except FileExistsError as exc:
        raise SystemExit(f"{exc.filename} already exists; remove it to issue again") from None
    except ValueError as exc:
        raise SystemExit(f"refused: {exc}") from None


if __name__ == "__main__":
    main()
