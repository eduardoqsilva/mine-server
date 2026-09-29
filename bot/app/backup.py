"""Backup do mundo, da config e da memoria do bot.

Duas camadas porque elas valem coisas diferentes. O /data e o que o BDS le:
mundo, server.properties, allowlist, permissions e os packs que o admin
instalou. O /state/bot.db e o que o bot sabe: quem e admin, o hash das chaves
de acesso, os overrides do /config e a auditoria. Perder o primeiro tira o
servidor; perder o segundo obriga a reemitir toda chave de acesso.

O que fica de fora, de proposito:

- `bedrock_server-*`, que e 245 MB dos 411 MB do /data. O entry script
  rebaixa o BDS no boot, entao guardar isso no backup so ocupa espaco.
- `server.properties.*.bak`, que sao o historico local de gravacao. O
  arquivo atual ja vai no backup e o history nao vale 16 KB de banda.
- `content_log.txt`, que e log, e o proprio bot tem /log para ele.

O tar sai com o servidor parado (ver ops.cria_backup), porque copiar um
mundo enquanto o BDS escreve nele da um arquivo ou consistente ou inútil.
"""

from __future__ import annotations

import logging
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import dropbox

log = logging.getLogger("bds.backup")

PREFIXO = "mine-bedrock-"
PASTA_DROPBOX = "/backups"
# tudo que entra do /data, em relacao a raiz do volume
DE_DATA = (
    "worlds",
    "behavior_packs",
    "resource_packs",
    "definitions",
    "server.properties",
    "allowlist.json",
    "permissions.json",
    "valid_known_packs.json",
    "profanity_filter.wlist",
    "server-icon.png",
)
DO_STATE = ("bot.db",)


class BackupError(RuntimeError):
    pass


@dataclass
class Backup:
    caminho: Path
    nome: str
    tamanho: int
    itens: list[str]
    segundos: float
    link: str = ""
    apagados_local: list[str] | None = None
    apagados_dropbox: list[str] | None = None
    erro_dropbox: str = ""
    # quantos backups ficaram guardados no Dropbox depois da rotacao
    dropbox_total: int = 0


def nome_arquivo(quando: datetime | None = None) -> str:
    """Nome com timestamp UTC, ordenavel por nome.

    UTC e nao o horario local porque o nome precisa ordenar por tempo mesmo
    que o servidor mude de fuso ou atravesse horario de verao. A hora local
    aparece no relatorio, que e onde ela faz sentido para o usuario.
    """
    momento = quando or datetime.now(timezone.utc)
    return f"{PREFIXO}{momento.strftime('%Y%m%dT%H%M%SZ')}.tar.gz"


def _seleciona(data_dir: Path, state_dir: Path) -> list[tuple[Path, str]]:
    """Monta a lista (caminho, nome dentro do tar) do que vale guardar.

    A raiz de cada pasta e unreadida, entao o tar sai com o prefixo `data/` e
    `state/`: e o que permite restaurar com um mv em vez de um script que
    precisa saber de onde veio cada coisa.
    """
    alvos: list[tuple[Path, str]] = []
    for nome in DE_DATA:
        caminho = data_dir / nome
        if caminho.exists():
            alvos.append((caminho, f"data/{nome}"))
    for nome in DO_STATE:
        caminho = state_dir / nome
        if caminho.exists():
            alvos.append((caminho, f"state/{nome}"))
    return alvos


def empacota(data_dir: Path, state_dir: Path, destino: Path) -> tuple[int, list[str]]:
    """Cria o .tar.gz em `destino`. Devolve (tamanho em bytes, itens incluidos).

    gzip nivel 1 e proposital: os packs ja vem zipados (nao comprimem mais) e
    o mundo e o que comprime, entao nivel alto seria CPU sobrando para
    ganhar quase nada. Streaming direto no arquivo, sem staging: o tar do
    mundo pode passar de um gigabyte e nao vale a pena ter dois no disco ao
    mesmo tempo.

    Ver _confere_mundo para o caso do mundo sair travado.
    """
    alvos = _seleciona(data_dir, state_dir)
    if not alvos:
        raise BackupError("nao achei nada para empacotar: /data e /state estao vazios?")
    destino.parent.mkdir(parents=True, exist_ok=True)
    # tarfile decide o compressor pelo NOME, e ele so sabe str.endswith: um
    # Path estoura aqui. No container seria Linux e nao apareceria, entao
    # assim o teste roda igual na sua maquina.
    with tarfile.open(str(destino), "w|gz", compresslevel=1) as tar:
        for caminho, dentro in alvos:
            if dentro == "data/worlds":
                _confere_mundo(caminho)
            tar.add(caminho, arcname=dentro, recursive=True)
    return destino.stat().st_size, [dentro for _, dentro in alvos]


def _confere_mundo(caminho: Path, tentativas: int = 3, espera: float = 1.5) -> None:
    """Espera todo arquivo do mundo poder ser lido, e so entao grava no tar.

    O mundo e LevelDB: o BDS mantem alguns .ldb abertos e, logo apos o
    container parar, o SO pode ainda nao ter liberado o descritor. Ler pela
    metade nao avisa - o resultado e um .tar.gz que abre sem erro e so falha
    na hora de restaurar, que e o pior lugar para descobrir.

    A conferida vem ANTES do tar.add de proposito. O `w|gz` e um stream: nao
    da para voltar atras e apagar o que ja foi gravado, entao repetir o
    add dentro do mesmo arquivo deixaria os membros pela metade duplicados no
    tar. Abrir cada arquivo primeiro custa uma passada a mais no mundo e
    garante que o tar so recebe o que da para ler inteiro.
    """
    for tentativa in range(tentativas):
        try:
            for arquivo in sorted(caminho.rglob("*")):
                if arquivo.is_file():
                    with arquivo.open("rb"):
                        pass
            return
        except (PermissionError, OSError) as exc:
            if tentativa == tentativas - 1:
                raise BackupError(
                    f"nao consegui ler {caminho.name} do mundo apos {tentativas} tentativas: {exc}"
                ) from exc
            log.warning("mundo ainda travado (%s), tentando de novo em %.1fs", exc, espera)
            time.sleep(espera)


def roda_local(backups_dir: Path, manter: int) -> list[str]:
    """Deixa so os `manter` backups locais mais novos. Devolve os nomes apagados."""
    pasta = backups_dir / "atuais"
    if not pasta.is_dir():
        return []
    meus = sorted((p for p in pasta.iterdir() if p.is_file() and p.name.startswith(PREFIXO)), key=lambda p: p.name)
    apagados = []
    for velho in meus[: max(0, len(meus) - manter)]:
        velho.unlink(missing_ok=True)
        apagados.append(velho.name)
    if apagados:
        log.info("backup: apaguei %d arquivo(s) local(is) antigo(s)", len(apagados))
    return apagados


def sobe(dbx: dropbox.Dropbox, arquivo: Path, manter: int) -> tuple[str, str, list[str], int]:
    """Sobe para o Dropbox, gira os antigos. Devolve (link, erro, apagados, sobraram).

    O `erro` vem separado do `link` em vez de levantar excecao porque um
    backup local bom com o Dropbox fora do ar ainda e um backup: o usuario
    precisa saber que o arquivo existe, mesmo sem link para clicar.
    """
    destino = f"{PASTA_DROPBOX}/{arquivo.name}"
    apagados: list[str] = []
    try:
        dbx.envia(arquivo, destino)
        apagados, _bytes_livres, sobraram = dbx.roda(PASTA_DROPBOX, PREFIXO, manter)
        return dbx.link(destino), "", apagados, sobraram
    except dropbox.DropboxError as exc:
        log.warning("Dropbox falhou no backup: %s", exc)
        return "", str(exc), apagados, 0
    except OSError as exc:
        log.warning("erro de disco ou de rede no Dropbox: %s", exc)
        return "", str(exc), apagados, 0
