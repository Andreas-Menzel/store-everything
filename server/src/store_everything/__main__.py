"""Process entrypoint: `python -m store_everything`."""

from __future__ import annotations

import uvicorn

from store_everything.config import load_settings
from store_everything.log import configure_logging


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)

    uvicorn.run(
        "store_everything.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        # `X-Forwarded-*` is honoured only from explicitly configured proxy addresses;
        # a spoofed client IP would poison rate limiting and audit records (ADR-0009).
        proxy_headers=settings.trust_proxy_headers,
        forwarded_allow_ips=settings.forwarded_allow_ips if settings.trust_proxy_headers else None,
        # Logging is configured above; uvicorn must not install its own handlers.
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
