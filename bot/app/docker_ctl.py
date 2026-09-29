"""Acoes no container do BDS: restart, comando de console, logs, versao, pull da imagem."""

from __future__ import annotations

import logging
import re
import time
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


def novas_linhas(antes: list[str], agora: list[str]) -> list[str]:
    """Linhas que apareceram entre as duas leituras do log.

    Nao da para contar: `docker logs --tail=N` devolve sempre N linhas assim
    que o log passa de N, entao num servidor com horas de uptime len() nunca
    cresce e a contagem daria zero sempre. A ancora e o conteudo: procura a
    maior sobreposicao em que o fim de "antes" e o comeco de "agora" batem, e
    o que sobra depois dela e o novo.

    Sem sobreposicao devolve vazio: e o caso do log ter rotacionado no meio da
    espera, e inventar linha seria pior do que dizer que nao veio nada.
    """
    if not antes:
        return agora
    for k in range(min(len(antes), len(agora)), 0, -1):
        if antes[-k:] == agora[:k]:
            return agora[k:]
    return []


_delta = novas_linhas


def _motivo_recusa(saida: str) -> str:
    """Traduz a saida do send-command em algo util para o admin.

    O script imprime duas mensagens parecidas e a causa nao e a mesma:
    "unable to find" e o find ter rodado sem achar o BDS, "failed to search"
    e o proprio find ter falhado - que e o que acontece quando o
    /proc/<pid>/exe nao resolve, limitacao do Docker Desktop no Windows sem
    conserto do lado do bot. No Linux de verdade o caminho funciona.
    """
    if "failed to search" in saida or "Permission denied" in saida:
        return (
            "nao consegui ler /proc/<pid>/exe dentro do container. "
            "Isso e o Docker Desktop no Windows, onde o /proc e virtual: "
            "o /console so funciona num Linux de verdade (a VPS)"
        )
    return "o processo do BDS nao apareceu. O servidor subiu? (docker compose logs bds)"


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

    def console(self, comando: str, espera: float = 3.0, teto: int = 25) -> tuple[str, list[str]]:
        """Manda um comando no console do BDS e devolve o que ele respondeu.

        O send-command da imagem nao e um cliente de console: ele escreve o
        comando no stdin do processo (/proc/<pid>/fd/0) e sai. A resposta nao
        volta por la - ela sai no stdout do container. Por isso a leitura vem
        do log, e para nao devolver o log inteiro e preciso saber quantas
        linhas existiam antes do comando.

        A espera e adaptativa: volta assim que a resposta parar de crescer, em
        vez de dormir os 3s inteiros sempre. Comando que nao responde nada
        (save-resume, stop) espera o tempo todo e devolve lista vazia, que o
        chamador mostra como "sem resposta".

        Devolve (erro, linhas novas do log): erro vazio = comando entregue.
        """
        antes = self.logs(lines=500).splitlines()
        try:
            saida = self.exec("send-command", comando)
        except (NotFound, APIError, DockerException) as exc:
            log.warning("send-command falhou: %s", exc)
            return f"falei com o container e recebi: {exc}", []
        if "ERROR" in saida:
            # O send-command avisa e sai sem escrever nada. Duas causas
            # conhecidas: o BDS ainda nao subiu, ou o /proc/<pid>/exe nao
            # resolve - que e o caso do Docker Desktop no Windows, onde o
            # /proc e virtual e o readlink do exe do processo da Permission
            # denied. nativa (VPS/Linux) o caminho funciona.
            log.warning("send-command recusou %r: %s", comando, saida.strip())
            return "o send-command recusou: " + _motivo_recusa(saida), []

        linhas: list[str] = []
        ultima = time.monotonic()
        fim = ultima + espera
        while time.monotonic() < fim:
            time.sleep(0.35)
            linhas_novas = novas_linhas(antes, self.logs(lines=500).splitlines())
            if len(linhas_novas) > len(linhas):
                linhas = linhas_novas
                ultima = time.monotonic()
            elif time.monotonic() - ultima >= 0.8 and linhas:
                break
        if not linhas:
            log.info("console %r nao gerou resposta em %.1fs", comando, espera)
        return "", linhas[-teto:]

    def restart(self, timeout: int = 100) -> None:
        # timeout < stop_grace_period (120s) para sobrar margem: o announce do
        # STOP_SERVER_ANNOUNCE_DELAY roda em 20s e o "stop" salva o mundo.
        self._container().restart(timeout=timeout)
        log.info("container reiniciado")

    def para(self, timeout: int = 110) -> None:
        """Desliga o container preservando o mundo.

        timeout < stop_grace_period (120s) de proposito: o "stop" do entry
        script roda em 20s e o resto da margem cobre o caso do SIGKILL, onde
        a morte so acontece depois de o tempo estourar.

        Diferente de restart() porque o backup precisa de uma janela parada
        de verdade, e nao de um stop seguido de start que outro restart
        poderia interromper no meio do tar.
        """
        self._container().stop(timeout=timeout)
        log.info("container parado")

    def liga(self) -> None:
        self._container().start()
        log.info("container ligado")

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
