"""ebusd client: read/definitions via HTTP-JSON (8889), write via TCP (8888)."""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .model import FieldDesc, parse_definitions, parse_device_meta, parse_values


class EbusdError(Exception):
    """Error communicating with ebusd."""


class EbusdClient:
    def __init__(
        self,
        host: str,
        port: int,
        http_port: int,
        session: aiohttp.ClientSession,
    ) -> None:
        self._host = host
        self._port = port  # TCP command port (write)
        self._http_port = http_port  # HTTP-JSON (read/definitions)
        self._session = session

    # ---- HTTP-JSON (read) ---------------------------------------------------
    async def _get(self, query: str) -> dict[str, Any]:
        url = f"http://{self._host}:{self._http_port}/data{query}"
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise EbusdError(f"HTTP {url}: {err}") from err

    async def get_definitions(
        self,
    ) -> tuple[list[FieldDesc], dict[str, dict[str, str]]]:
        data = await self._get("?def&write&verbose")
        return parse_definitions(data), parse_device_meta(data)

    async def get_values(self) -> dict[tuple[str, str, str], Any]:
        return parse_values(await self._get(""))

    async def get_data(self) -> dict[str, Any]:
        """Raw /data (values + global section).

        `full` additionally provides `lastup` per message -> the basis for
        detecting ourselves which values the bus already keeps fresh.
        Not `verbose`: that only toggles units and comments
        (mainloop.cpp; `lastup` hangs off OF_ALL_ATTRS, i.e. `full`).
        """
        return await self._get("?full")

    async def refresh(self, circuit: str, message: str, max_age: int) -> None:
        """Read a message directly from the bus if the cache is older than `max_age`.

        ebusd performs this bus read in a blocking way (mainloop.cpp: readFromBus),
        so only use it for a few genuinely time-critical messages.
        """
        await self._get(f"/{circuit}/{message}?exact=1&required=1&maxage={max_age}")

    # ---- TCP (write + connection test) --------------------------------------
    async def _command(self, cmd: str) -> list[str]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), timeout=10
            )
        except (OSError, asyncio.TimeoutError) as err:
            raise EbusdError(f"TCP connect {self._host}:{self._port}: {err}") from err
        try:
            writer.write((cmd + "\n").encode())
            await writer.drain()
            lines: list[str] = []
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout=15)
                if raw == b"":
                    break
                line = raw.decode(errors="replace").rstrip("\r\n")
                if line == "":
                    break
                lines.append(line)
            return lines
        except (OSError, asyncio.TimeoutError) as err:
            raise EbusdError(f"TCP command {cmd!r}: {err}") from err
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=5)
            except (OSError, asyncio.TimeoutError):
                pass

    async def write(self, circuit: str, message: str, value: object) -> None:
        await self._command(f"write -c {circuit} {message} {value}")

    async def read(self, circuit: str, message: str) -> None:
        """Force a fresh read from the bus (updates ebusd's cache -> /data)."""
        await self._command(f"read -f -c {circuit} {message}")

    async def test(self) -> None:
        """Checks both ports (HTTP read + TCP reachable)."""
        await self._get("")  # HTTP 8889
        await self._command("info")  # TCP 8888
