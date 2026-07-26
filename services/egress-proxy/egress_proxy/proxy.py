"""Default-deny HTTP/HTTPS proxy (WSC-E2-T1/T2), implemented in pure asyncio
(stdlib) — no mitmproxy — so it can run inside a `python:3.11-slim` container
with no `pip install` (useful for the containerized network isolation test,
which cannot depend on building a custom image).

Two traffic forms are supported:
  - `CONNECT host:port` (opaque HTTPS tunnel): used for model-gateway access and
    for any HTTPS host on the allowlist. Enforcement = allowlist only (we cannot
    — and do not want to, it is out of scope for this phase — inspect/inject
    inside an opaque TLS tunnel without terminating TLS, which would require a
    trusted CA installed in the sandbox; see README).
  - Plain HTTP proxying (`GET/POST http://host/path HTTP/1.1` as the
    request-line, the classic forward-proxy form): used for the "git remote
    relay" pattern and for plaintext model-gateway calls — here we DO inject
    credentials, because the proxy terminates the connection and builds the
    outbound request itself.

Every refusal (host off the allowlist) emits `dse_audit.emit(action=
"egress_denied", ...)` — the import is optional (the proxy also runs in a "bare"
container without `dse_audit` installed, in which case it falls back to logging
on stdout; production must install `dse-audit` in the egress-proxy image).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from .allowlist import Allowlist
from .credentials import CredentialBroker

logger = logging.getLogger("egress_proxy")

try:
    from dse_audit import emit as _audit_emit
except Exception:  # pragma: no cover - "bare container" mode, dse_audit not installed
    _audit_emit = None


def _emit_audit(*, actor: str, action: str, tenant_id: str, work_item_id: str | None, details: dict) -> None:
    if _audit_emit is not None:
        try:
            _audit_emit(actor=actor, action=action, tenant_id=tenant_id, work_item_id=work_item_id, details=details)
            return
        except Exception:  # noqa: BLE001 - never let an egress denial fail because of the audit write
            logger.warning("failed to write audit_log, falling back to a local log: %s %s", action, details)
    logger.info("AUDIT (fallback local, sem Postgres) action=%s details=%s", action, details)


_ABSOLUTE_URI_RE = re.compile(r"^https?://([^/:]+)(:(\d+))?(/.*)?$")


@dataclass
class DenialLog:
    host: str
    port: int
    ts: float = field(default_factory=time.time)


class EgressProxy:
    def __init__(
        self,
        allowlist: Allowlist,
        *,
        tenant_id: str,
        work_item_id: str | None = None,
        credential_broker: CredentialBroker | None = None,
    ) -> None:
        self.allowlist = allowlist
        self.tenant_id = tenant_id
        self.work_item_id = work_item_id
        self.credential_broker = credential_broker or CredentialBroker()
        self.denials: list[DenialLog] = []
        self._server: asyncio.base_events.Server | None = None

    async def start(self, host: str = "0.0.0.0", port: int = 8806) -> asyncio.base_events.Server:
        self._server = await asyncio.start_server(self._handle_client, host, port)
        return self._server

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # -- internals ----------------------------------------------------------

    async def _read_headers(self, reader: asyncio.StreamReader) -> dict[str, str]:
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"", b"\n"):
                break
            if b":" in line:
                k, v = line.decode(errors="replace").split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return headers

    def _is_allowed(self, host: str, port: int) -> bool:
        return self.allowlist.is_allowed(host, port)

    def _deny(self, host: str, port: int) -> bytes:
        self.denials.append(DenialLog(host=host, port=port))
        _emit_audit(
            actor="system:egress-proxy",
            action="egress_denied",
            tenant_id=self.tenant_id,
            work_item_id=self.work_item_id,
            details={"host": host, "port": port},
        )
        return b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            try:
                method, target, _version = request_line.decode(errors="replace").strip().split(" ")
            except ValueError:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            headers = await self._read_headers(reader)

            if method.upper() == "CONNECT":
                await self._handle_connect(target, writer, reader)
            else:
                await self._handle_plain_http(method, target, headers, reader, writer)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:  # noqa: BLE001 - the proxy must never crash the process over 1 bad connection
            logger.exception("error handling egress connection")
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _handle_connect(
        self, target: str, writer: asyncio.StreamWriter, reader: asyncio.StreamReader
    ) -> None:
        host, _, port_str = target.partition(":")
        port = int(port_str) if port_str else 443
        if not self._is_allowed(host, port):
            writer.write(self._deny(host, port))
            await writer.drain()
            return
        try:
            remote_reader, remote_writer = await asyncio.open_connection(host, port)
        except OSError:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(pipe(reader, remote_writer), pipe(remote_reader, writer))

    async def _handle_plain_http(
        self,
        method: str,
        target: str,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        m = _ABSOLUTE_URI_RE.match(target)
        if not m:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        host = m.group(1)
        port = int(m.group(3)) if m.group(3) else 80
        path = m.group(4) or "/"

        if not self._is_allowed(host, port):
            writer.write(self._deny(host, port))
            await writer.drain()
            return

        inject = headers.pop("x-dse-inject-credential", None)
        if inject == "github":
            cred = self.credential_broker.mint(
                work_item_id=self.work_item_id or "unknown",
                repo=headers.get("x-dse-repo", "unknown/unknown"),
                branch=headers.get("x-dse-branch", "unknown"),
            )
            headers["authorization"] = f"token {cred.token}"
            headers["x-dse-credential-id"] = cred.credential_id

        length = int(headers.get("content-length", "0") or 0)
        body = await reader.readexactly(length) if length else b""

        try:
            remote_reader, remote_writer = await asyncio.open_connection(host, port)
        except OSError:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        headers["host"] = host
        headers["connection"] = "close"
        request_lines = [f"{method} {path} HTTP/1.1"]
        request_lines += [f"{k}: {v}" for k, v in headers.items()]
        remote_writer.write(("\r\n".join(request_lines) + "\r\n\r\n").encode())
        if body:
            remote_writer.write(body)
        await remote_writer.drain()

        while True:
            data = await remote_reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        remote_writer.close()
