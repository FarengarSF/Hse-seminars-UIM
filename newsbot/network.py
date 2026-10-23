"""Verified TLS, including Python.org installations without a bundled CA file."""
import os
from pathlib import Path
import ssl


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    defaults = ssl.get_default_verify_paths()
    if not defaults.cafile and not defaults.capath and not os.environ.get("SSL_CERT_FILE"):
        for candidate in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt",
                          "/etc/pki/tls/certs/ca-bundle.crt"):
            if Path(candidate).is_file():
                context.load_verify_locations(cafile=candidate)
                break
    return context

