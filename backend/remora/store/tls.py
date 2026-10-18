"""The TLS context for store traffic: the system's trust store, with certifi's bundle added only
when the system store holds no CA certificate (a minimal container, a broken ca-certificates)."""

from __future__ import annotations

import logging
import ssl

log = logging.getLogger("remora.store")


def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # certificates in a hashed capath are loaded lazily and don't count here, so an empty count
    # can also mean "capath only": adding certifi then only adds roots, it removes none
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        try:
            import certifi
        except ImportError:  # pragma: no cover - certifi ships with Remora's dependencies
            log.warning("the system trust store looks empty and certifi is missing")
        else:
            ctx.load_verify_locations(cafile=certifi.where())
    return ctx
