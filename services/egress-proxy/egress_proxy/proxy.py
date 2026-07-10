"""Proxy HTTP/HTTPS default-deny (WSC-E2-T1/T2), implementado em asyncio puro
(stdlib) — sem mitmproxy — para poder rodar dentro de um container
`python:3.11-slim` sem `pip install` (útil para o teste de isolamento de rede
containerizado, que não pode depender de build de imagem custom).

Duas formas de tráfego suportadas:
  - `CONNECT host:port` (túnel HTTPS opaco): usado para acesso ao
    model-gateway e para qualquer host HTTPS na allowlist. Enforcement =
    allowlist apenas (não conseguimos — nem queremos, é fora de escopo desta
    fase — inspecionar/injetar dentro de um túnel TLS opaco sem terminar TLS,
    o que exigiria um CA confiável instalado no sandbox; ver README).
  - Proxy HTTP simples (`GET/POST http://host/path HTTP/1.1` como
    request-line, forma clássica de forward-proxy): usado para o padrão de
    "git remote relay" e chamadas ao model-gateway em texto plano — aqui SIM
    injetamos credenciais, porque o proxy termina a conexão e monta a
    requisição de saída ele mesmo.

Toda recusa (host fora da allowlist) gera `dse_audit.emit(action=
"egress_denied", ...)` — import é opcional (o proxy roda também num
container "pelado" sem `dse_audit` instalado, caso em que cai para log em
stdout; produção deve instalar `dse-audit` na imagem do egress-proxy).
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
except Exception:  # pragma: no cover - modo "container pelado" sem dse_audit instalado
    _audit_emit = None


def _emit_audit(*, actor: str, action: str, tenant_id: str, work_item_id: str | None, details: dict) -> None:
    if _audit_emit is not None:
        try:
            _audit_emit(actor=actor, action=action, tenant_id=tenant_id, work_item_id=work_item_id, details=details)
            return
        except Exception:  # noqa: BLE001 - nunca deixa a recusa de egress falhar por causa do audit
            logger.warning("falha ao gravar audit_log, caindo para log local: %s %s", action, details)
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
        except Exception:  # noqa: BLE001 - proxy nunca deve crashar o processo por 1 conexão ruim
            logger.exception("erro tratando conexão de egress")
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
