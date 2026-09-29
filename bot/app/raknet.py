"""Ping RakNet (unconnected ping) para saber se o BDS esta de pe e quantos jogadores tem.

Usado em dois lugares:
  - checagem de saude depois de cada restart (inclusive o diario das 05:00);
  - /status, para mostrar jogadores online sem depender de log.

Duas situacoes que o ping denuncia:
  - pong com o payload "MCPE;..."  -> tudo certo, jogadores veem o nome/versao;
  - pong de 33 bytes sem payload   -> bug do Mojang MCPE-239705, o cliente da
    timeout. Nao tem contorno no compose: o ENABLE_LAN_VISIBILITY esta "false"
    de proposito. Se aparecer, e bug do Mojang mesmo - avise.

Sobre o nethernet: no BDS 1.26.52+ ele e o unico transporte suportado
(o servidor aborta a conexao se transport=raknet) e responde ao unconnected
ping do RakNet com um prefixo de 17 bytes antes do magic. Por isso o parser
procura o MAGIC em vez de assumir que ele comeca no offset 1.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import time
from dataclasses import dataclass

MAGIC = bytes((0x00, 0xFF, 0xFF, 0x00, 0xFE, 0xFE, 0xFE, 0xFE, 0xFD, 0xFD, 0xFD, 0xFD, 0x12, 0x34, 0x56, 0x78))
UNCONNECTED_PING = 0x01
UNCONNECTED_PONG = 0x1C
BROKEN_PONG_SIZE = 33


@dataclass
class PingResult:
    ok: bool
    motd_ok: bool
    pong_bytes: int
    error: str = ""
    game_name: str = ""
    protocol: str = ""
    version: str = ""
    players: int | None = None
    max_players: int | None = None
    motd: str = ""

    def resumo(self) -> str:
        if not self.ok:
            return f"sem resposta ({self.error or 'timeout'})"
        if not self.motd_ok:
            return (
                f"PONG DEFEITUOSO ({self.pong_bytes} bytes, sem MCPE;...) - "
                "clientes nao conectam, bug do Mojang"
            )
        jogadores = f"{self.players}/{self.max_players}" if self.players is not None else "?"
        return f"{self.game_name} {self.version}  ·  {jogadores} jogador(es) online"


def _offset_do_magic(data: bytes) -> int:
    """Offset do MAGIC no pong, ou -1 se nao houver um framing valido.

    No RakNet puro o MAGIC comeca no offset 1. No nethernet (unico transporte
    do BDS 1.26.52+) o BDS prepende 17 bytes antes dele. Em vez de travar num
    offset so, procuramos o MAGIC e so aceitamos a posicao em que o payload
    declarado fecha exatamente o resto do pacote - e o que garante que um
    MAGIC achado por acaso dentro do payload nao passe.
    """
    inicio = 1
    while True:
        achado = data.find(MAGIC, inicio)
        if achado < 0:
            return -1
        if achado + 2 + len(MAGIC) <= len(data):
            (length,) = struct.unpack_from(">H", data, achado + len(MAGIC))
            if len(data) - (achado + len(MAGIC) + 2) == length:
                return achado
        inicio = achado + 1


def _parse_pong(data: bytes) -> PingResult:
    if len(data) < 1 + len(MAGIC) + 2:
        return PingResult(False, False, len(data), "pong truncado")
    if data[0] != UNCONNECTED_PONG:
        return PingResult(False, False, len(data), f"id inesperado 0x{data[0]:02x}")

    magic = _offset_do_magic(data)
    if magic < 0:
        return PingResult(False, False, len(data), "magic divergente (resposta nao e do raknet)")

    start = magic + len(MAGIC)
    (length,) = struct.unpack_from(">H", data, start)
    payload = data[start + 2 :]
    if len(payload) < length:
        return PingResult(False, False, len(data), f"payload curto: {len(payload)} de {length}")
    payload = payload[:length]

    if not payload.startswith(b"MCPE;"):
        return PingResult(
            True,
            False,
            len(data),
            (
                f"pong de {len(data)} bytes sem MCPE; - bug do Mojang (itzg#649)"
                if len(data) == BROKEN_PONG_SIZE
                else f"payload sem MCPE; ({payload[:24]!r})"
            ),
            "",
            "",
            "",
            None,
            None,
            "",
        )

    parts = payload.decode("utf-8", "replace").split(";")
    players = maxplayers = None
    if len(parts) > 4 and parts[4].isdigit():
        players = int(parts[4])
    if len(parts) > 5 and parts[5].isdigit():
        maxplayers = int(parts[5])
    return PingResult(
        ok=True,
        motd_ok=True,
        pong_bytes=len(data),
        game_name=parts[1] if len(parts) > 1 else "",
        protocol=parts[2] if len(parts) > 2 else "",
        version=parts[3] if len(parts) > 3 else "",
        players=players,
        max_players=maxplayers,
        motd=parts[7] if len(parts) > 7 else "",
    )


def ping(host: str, port: int = 19132, timeout: float = 3.0) -> PingResult:
    now = struct.pack(">Q", int(time.time() * 1000))
    client_id = struct.pack(">Q", 0x0123456789ABCDEF)
    request = bytes((UNCONNECTED_PING,)) + now + MAGIC + client_id

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(request, (host, port))
        data, _ = sock.recvfrom(2048)
    except socket.timeout:
        return PingResult(False, False, 0, "timeout")
    except OSError as exc:
        return PingResult(False, False, 0, str(exc))
    finally:
        sock.close()
    return _parse_pong(data)


async def ping_async(host: str, port: int = 19132, timeout: float = 3.0) -> PingResult:
    return await asyncio.to_thread(ping, host, port, timeout)
