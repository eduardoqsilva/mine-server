"""Acoes no container do BDS: restart, comando de console, logs, versao, pull da imagem."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

import docker
from docker.errors import APIError, DockerException, NotFound

log = logging.getLogger("bds.docker")

_VERSION_PATTERNS = (
    re.compile(r"Selected Bedrock server version: (\S+)"),
    re.compile(r"Version:\s*(\d[\w.\-+]*)"),
    re.compile(r"Starting Bedrock (?:Dedicated )?Server v?(\d[\w.\-+]*)"),
)


@dataclass
class ContainerState:
    running: bool
    status: str
    started_at: str
    restart_count: int
    image: str


class DockerController:
    def __init__(self, container_name: str, image: str) -> None:
        self.container_name = container_name
        self.image = image
        self._client = docker.from_env()

    def _container(self):
        return self._client.containers.get(self.container_name)

    def state(self) -> ContainerState:
        try:
            c = self._container().attrs
        except NotFound:
            return ContainerState(False, "nao encontrado", "", 0, self.image)
        st = c.get("State", {})
        return ContainerState(
            running=bool(st.get("Running")),
            status=st.get("Status", "?"),
            started_at=st.get("StartedAt", ""),
            restart_count=int(c.get("RestartCount", 0)),
            image=c.get("Config", {}).get("Image", self.image),
        )

    def logs(self, lines: int = 200, since: float | None = None) -> str:
        try:
            # since vai como int: o docker-py exige datetime ou numero positivo,
            # e recusava a string com InvalidArgument. Esse erro vem de
            # DockerException (nao de APIError), entao o except abaixo e amplo
            # de proposito: log indisponivel nao pode derrubar o /reiniciar.
            raw = self._container().logs(tail=lines, since=int(since) if since else None)
        except DockerException as exc:
            log.warning("logs indisponiveis: %s", exc)
            return ""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return str(raw)

    def exec(self, *args: str) -> str:
        """Roda um binario no container (send-command, etc)."""
        res = self._container().exec_run(list(args), demux=False)
        out = res.output or b""
        return out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)

    def say(self, text: str) -> None:
        try:
            self.exec("send-command", "say", text)
        except (NotFound, APIError) as exc:
            log.warning("send-command falhou: %s", exc)

    def restart(self, timeout: int = 100) -> None:
        # timeout < stop_grace_period (120s) para sobrar margem: o announce do
        # STOP_SERVER_ANNOUNCE_DELAY roda em 20s e o "stop" salva o mundo.
        self._container().restart(timeout=timeout)
        log.info("container reiniciado")

    def pull_image(self) -> None:
        """Baixa a imagem nova sem recriar o container (vale no proximo up -d)."""
        try:
            for _ in self._client.api.pull(self.image, stream=True, decode=True):
                pass
            log.info("imagem %s atualizada", self.image)
        except (DockerException, APIError) as exc:
            log.warning("pull da imagem falhou: %s", exc)

    def server_version(self, since: float | None = None) -> str:
        text = self.logs(lines=400, since=since)
        for pattern in _VERSION_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1).rstrip(".")
        return "?"

    def _epoch_do_boot(self) -> float:
        """Inicio do container atual em epoch, para cortar o log no --since."""
        raw = self._container().attrs.get("State", {}).get("StartedAt", "")
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    def pronto(self) -> tuple[bool, str]:
        """Se o BDS imprimiu "Server started." neste boot, e o alerta de transporte.

        Sempre recortado no inicio do container: o log guarda o historico, e um
        "Server started." do boot anterior (ou o erro de transporte de um boot
        velho) nao diz nada sobre o servidor de agora.

        Sinal confere em qualquer transporte. O ping RakNet nao da: no
        nethernet (unico transporte do BDS 1.26.52+) a 19132 nem e bindada,
        entao esperar o pong queima o BOOT_TIMEOUT inteiro a cada restart.
        """
        text = self.logs(lines=400, since=self._epoch_do_boot() or None)
        if "TRANSPORT TYPE ERROR" in text:
            return False, (
                "o BDS recusou o transporte configurado: nesta versao o nethernet e o unico "
                "aceito. Confira a propriedade 'transport' no server.properties"
            )
        return "Server started." in text, ""
