"""Round-robin egress proxy pool for publisher browser sessions.

The pool maps a multi-port ISP proxy plan (one static IP per port, for
example ``isp.oxylabs.io:8001`` … ``:8010``) onto Playwright-style proxy
settings. The proxy password is never stored in config or artifacts; it is
read from the ``INSTSCI_PROXY_PASSWORD`` environment variable at runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

PROXY_PASSWORD_ENV = "INSTSCI_PROXY_PASSWORD"


def parse_ports(spec: str) -> list[int]:
    """Parse a port spec like ``"8001-8010"`` or ``"8001,8003"`` into a list."""
    text = str(spec or "").strip()
    if not text:
        return []
    ports: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, _, end_text = part.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ValueError(f"invalid proxy port range: {part!r}") from exc
            if end < start:
                raise ValueError(f"invalid proxy port range: {part!r}")
            ports.extend(range(start, end + 1))
        else:
            try:
                ports.append(int(part))
            except ValueError as exc:
                raise ValueError(f"invalid proxy port: {part!r}") from exc
    return ports


@dataclass
class ProxyPool:
    """Cycle through fixed-IP proxy ports, one Playwright dict at a time."""

    host: str
    ports: list[int]
    username: str
    password: str
    _index: int = field(default=0, repr=False)

    @classmethod
    def from_config(cls, config: Any, *, start_offset: int = 0) -> "ProxyPool | None":
        """Build a pool from config, or None when the pool is not configured.

        ``start_offset`` shifts which port the pool hands out first, so parallel
        brokers each begin on a different egress IP.
        """
        ports = parse_ports(getattr(config, "proxy_pool_ports", ""))
        if not ports:
            return None
        password = os.environ.get(PROXY_PASSWORD_ENV, "")
        if not password:
            raise RuntimeError(
                f"proxy pool is configured but {PROXY_PASSWORD_ENV} is not set"
            )
        return cls(
            host=str(getattr(config, "proxy_pool_host", "") or "").strip(),
            ports=ports,
            username=str(getattr(config, "proxy_pool_username", "") or "").strip(),
            password=password,
            _index=start_offset % len(ports),
        )

    def next_proxy(self) -> dict[str, str]:
        """Return the next proxy as Playwright settings, round-robin."""
        port = self.ports[self._index % len(self.ports)]
        self._index += 1
        return {
            "server": f"http://{self.host}:{port}",
            "username": self.username,
            "password": self.password,
        }


def rotate_context(downloader: Any, context: Any, pool: "ProxyPool") -> Any:
    """Close the current browser context and relaunch it on the next proxy.

    The persistent profile directory is untouched, so cookies and SSO state
    survive the relaunch; only the egress IP changes.
    """
    try:
        context.close()
    except Exception:
        pass
    return downloader._launch_context(proxy=pool.next_proxy())


class RotationCounter:
    """Signal a rotation every ``every`` downloads; ``0`` disables rotation."""

    def __init__(self, every: int):
        self.every = max(0, int(every))
        self._count = 0

    def tick(self) -> bool:
        if not self.every:
            return False
        self._count += 1
        if self._count >= self.every:
            self._count = 0
            return True
        return False
