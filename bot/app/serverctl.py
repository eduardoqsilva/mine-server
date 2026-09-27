"""Controle do servidor: server.properties, allowlist.json, permissions.json e kicks.

O bot e o dono do /data/server.properties. As variaveis de ambiente do compose
que brigariam com o arquivo (GAMEMODE, DIFFICULTY...) foram removidas de la
justamente por isso: se as duas fontes existirem, o container sobrescreve o
arquivo no boot e a mudanca do Telegram se perde a cada reinicio.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("bds.server")

XUID_RE = re.compile(r"\b\d{15,20}\b")

BACKUP_SUFFIX = ".bak"


class PropertyError(ValueError):
    pass


@dataclass(frozen=True)
class Prop:
    key: str
    label: str
    tipo: str  # str | int | bool | choice
    reinicia: bool = True
    opcoes: tuple[str, ...] = ()
    cuidado: str = ""

    @property
    def tipo_texto(self) -> str:
        if self.tipo == "bool":
            return "true/false"
        if self.tipo == "choice":
            return " | ".join(self.opcoes)
        return self.tipo


CATALOGO: tuple[Prop, ...] = (
    Prop("server-name", "Nome do servidor", "str", cuidado='nao pode ter ";"'),
    Prop("gamemode", "Modo de jogo", "choice", opcoes=("survival", "creative", "adventure", "default")),
    Prop("force-gamemode", "Forca o modo de jogo", "bool"),
    Prop("difficulty", "Dificuldade", "choice", opcoes=("peaceful", "easy", "normal", "hard")),
    Prop("max-players", "Maximo de jogadores", "int", cuidado="jogadores conectados saem no restart"),
    Prop("view-distance", "Distancia de visao", "int"),
    Prop("tick-distance", "Distancia de tick", "int"),
    Prop("level-seed", "Semente do mundo", "str", cuidado="mudar depois apaga o mundo gerado"),
    Prop("allow-cheats", "Permite cheats", "bool", cuidado="vale so com ops de verdade na mao"),
    Prop("default-player-permission-level", "Permissao padrao", "choice", opcoes=("visitor", "member", "operator")),
    Prop("hide-online-players", "Esconde quem esta online", "bool"),
    Prop("texturepacksrequired", "Exige resource pack", "bool"),
    Prop("enforce-secure-profile", "Exige perfil seguro", "bool"),
    Prop("server-authoritative-movement", "Movimento autoritativo", "bool"),
    Prop("player-idle-timeout", "Desloga apos X min parado", "int"),
    Prop("allow-list", "Lista ligada (so quem esta na lista entra)", "bool"),
    Prop(
        "server-port",
        "Porta do jogo",
        "int",
        cuidado="o nginx so encaminha a 19132: mudar isso derruba o acesso ate voce mudar o nginx tambem",
    ),
    Prop("level-name", "Nome da pasta do mundo", "str", cuidado="so vale no primeiro boot; depois e preciso mover a pasta"),
    Prop("online-mode", "Autenticacao Microsoft", "bool", cuidado="desligar quebra o permissions.json (precisa de XUID)"),
    Prop(
        "server-udp-ports",
        "Faixa UDP do nethernet",
        "portas",
        cuidado=(
            "atras de nginx/firewall o nethernet so conecta com faixa fixa: "
            "o externo tem que bater com a porta publicada no proxy. "
            "Vazio volta a usar as portas efemeras e so funciona em LAN"
        ),
    )
    # "transport" fica de fora de proposito: no BDS 1.26.52+ o nethernet e o
    # unico transporte suportado e o proprio servidor aborta a conexao se
    # transport=raknet. Expor a chave seria um jeito de derrubar o acesso de
    # todo mundo sem querer.
)

POR_CHAVE = {p.key: p for p in CATALOGO}


def interpreta_config(args: list[str]) -> tuple[str, str, bool] | None:
    """Quebra "/config chave valor [sim]" em (chave, valor, confirmado).

    O "sim" final confirma propriedade perigosa (prop.cuidado). Sem ele o valor
    e devolvido inteiro, para nao perder texto com espacos ("Meu Server Legal").
    Devolve None quando nao ha chave nem valor.
    """
    if len(args) < 2:
        return None
    confirmado = len(args) > 2 and args[-1].lower() == "sim"
    bruto = " ".join(args[1:-1] if confirmado else args[1:])
    return args[0].lower(), bruto, confirmado


def precisa_confirmacao(prop: Prop, confirmado: bool) -> bool:
    return bool(prop.cuidado) and not confirmado


def _faixa_valida(bruto: str) -> bool:
    """Confere "N" ou "N-M" com 1..65535 e inicio <= fim."""
    pedacos = bruto.split("-")
    if len(pedacos) > 2:
        return False
    numeros = []
    for pedaco in pedacos:
        if not pedaco.isdigit():
            return False
        numero = int(pedaco)
        if not 1 <= numero <= 65535:
            return False
        numeros.append(numero)
    return numeros[0] <= numeros[-1]


# Formas aceitas pelo BDS: [host:]porta[:porta[:porta-inicio-porta-fim]], onde
# cada parte e "N" ou "N-M". O host e opcional e so usado como texto de
# iluminacao do ICE.
RE_PORTAS = re.compile(
    r"^(?:(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<v4>(?:[0-9]{1,3}\.){3}[0-9]{1,3})):)?"
    r"(?P<a>[0-9]{1,5}(?:-[0-9]{1,5})?)"
    r"(?::(?P<b>[0-9]{1,5}(?:-[0-9]{1,5})?))?$"
)


def _valida_portas(chave: str, valor: str) -> str:
    """Valida server-udp-ports, a propriedade mais facil de errar do catalogo.

    O BDS nao reclama de valor invalido: ele sobe assim mesmo e o gameplay fica
    preso em portas que o cliente nunca alcanca. Falhar aqui e melhor.
    """
    achado = RE_PORTAS.match(valor)
    if not achado:
        raise PropertyError(
            f"'{chave}' invalido: {valor!r}. Use 'N', 'N-M' ou "
            "'externo:interno', como 19133-19172:19133-19172"
        )
    for grupo in ("a", "b"):
        parte = achado.group(grupo)
        if parte is not None and not _faixa_valida(parte):
            raise PropertyError(f"'{chave}' tem porta fora de 1-65535 ou invertida em {parte!r}")
    return valor


def valida(prop: Prop, bruto: str) -> str:
    valor = bruto.strip()
    if prop.tipo == "bool":
        baixo = valor.lower()
        if baixo in ("true", "sim", "s", "1", "on", "ligado"):
            return "true"
        if baixo in ("false", "nao", "n", "0", "off", "desligado"):
            return "false"
        raise PropertyError(f"'{prop.key}' e true/false, veio {bruto!r}")
    if prop.tipo == "int":
        try:
            return str(int(valor))
        except ValueError:
            raise PropertyError(f"'{prop.key}' e um numero inteiro, veio {bruto!r}") from None
    if prop.tipo == "choice":
        canonico = valor.lower()
        for opcao in prop.opcoes:
            if canonico == opcao.lower():
                return opcao
        raise PropertyError(f"'{prop.key}' aceita: {', '.join(prop.opcoes)}")
    if prop.tipo == "portas":
        return _valida_portas(prop.key, valor)
    if prop.key == "server-name" and ";" in valor:
        raise PropertyError("server-name nao pode conter ';' (o Bedrock separa o ping por ';')")
    return valor


class ServerControl:
    def __init__(self, data_dir: Path, backup_keep: int = 5) -> None:
        self.data_dir = data_dir
        self.backup_keep = backup_keep
        self.props_path = data_dir / "server.properties"
        self.allowlist_path = data_dir / "allowlist.json"
        self.permissions_path = data_dir / "permissions.json"

    # ---------------------------------------------------------- server.properties

    def existe_props(self) -> bool:
        return self.props_path.is_file()

    def le_props(self) -> dict[str, str]:
        if not self.props_path.is_file():
            return {}
        out: dict[str, str] = {}
        for linha in self.props_path.read_text(encoding="utf-8", errors="replace").splitlines():
            limpa = linha.strip()
            if not limpa or limpa.startswith("#") or "=" not in limpa:
                continue
            chave, _, valor = limpa.partition("=")
            out[chave.strip()] = valor.strip()
        return out

    def _backup_props(self) -> Path | None:
        if not self.props_path.is_file():
            return None
        alvo = self.props_path.with_suffix(self.props_path.suffix + f".{int(time.time())}{BACKUP_SUFFIX}")
        shutil.copy2(self.props_path, alvo)
        velhos = sorted(self.data_dir.glob("server.properties.*.bak"))
        for velho in velhos[: max(0, len(velhos) - self.backup_keep)]:
            velho.unlink(missing_ok=True)
        return alvo

    def set_prop(self, chave: str, valor: str) -> tuple[str, str]:
        """Escreve uma propriedade preservando os comentarios do arquivo."""
        if not self.props_path.is_file():
            raise PropertyError("server.properties ainda nao existe (o BDS ainda nao subiu?)")
        linhas = self.props_path.read_text(encoding="utf-8", errors="replace").splitlines()
        alvo = f"{chave}="
        novo: list[str] = []
        substituido = False
        for linha in linhas:
            limpa = linha.strip()
            if (not limpa.startswith("#")) and limpa.startswith(alvo):
                novo.append(f"{chave}={valor}")
                substituido = True
            else:
                novo.append(linha)
        if not substituido:
            novo.append(f"{chave}={valor}")
        self._backup_props()
        self.props_path.write_text("\n".join(novo) + "\n", encoding="utf-8")
        return chave, valor

    def aplica(self, mudancas: dict[str, str]) -> list[str]:
        aplicadas = []
        for chave, valor in mudancas.items():
            self.set_prop(chave, valor)
            aplicadas.append(f"{chave}={valor}")
        return aplicadas

    def nivel_do_mundo(self, padrao: str = "world") -> str:
        """O nome da pasta do mundo: o arquivo manda, nao o .env."""
        return self.le_props().get("level-name", "").strip() or padrao

    # -------------------------------------------------------------- allow/deny

    def _lista(self, caminho: Path) -> list[dict]:
        if not caminho.is_file():
            return []
        try:
            dados = json.loads(caminho.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise PropertyError(f"{caminho.name} esta corrompido ({exc}); arrume na mao antes de continuar") from None
        if not isinstance(dados, list):
            raise PropertyError(f"{caminho.name} devia ser uma lista JSON")
        return dados

    def _salva_lista(self, caminho: Path, dados: list[dict]) -> None:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        if caminho.is_file():
            shutil.copy2(caminho, caminho.with_suffix(caminho.suffix + f".{int(time.time())}{BACKUP_SUFFIX}"))
        caminho.write_text(json.dumps(dados, indent=2) + "\n", encoding="utf-8")

    def allowlist(self) -> list[dict]:
        return self._lista(self.allowlist_path)

    def permissoes(self) -> list[dict]:
        return self._lista(self.permissions_path)

    def add_allowlist(self, nome: str, xuid: str | None = None, log_txt: str | None = None) -> bool:
        """True se entrou como novo.

        O Bedrock usa o XUID para validar a entrada em allow-list. Quando o
        comando vem apenas com o nickname, tentamos resolver o XUID no log do
        servidor antes de gravar a entrada; se o registro ja existe sem XUID,
        tambem atualizamos para o valor correto.
        """
        dados = self.allowlist()
        alvo = nome.lower()
        if xuid is None and log_txt is not None:
            xuid = self.xuid_no_log(nome, log_txt)

        for item in dados:
            nome_atual = str(item.get("name", "")).lower()
            xuid_atual = str(item.get("xuid", ""))
            if nome_atual == alvo or (xuid and xuid_atual == xuid):
                if xuid and not xuid_atual:
                    item["xuid"] = str(xuid)
                if not item.get("name"):
                    item["name"] = nome
                if xuid and xuid_atual and xuid_atual != xuid:
                    item["xuid"] = str(xuid)
                self._salva_lista(self.allowlist_path, dados)
                return False

        entrada: dict[str, str] = {"name": nome}
        if xuid:
            entrada = {"xuid": str(xuid), "name": nome}
        dados.append(entrada)
        self._salva_lista(self.allowlist_path, dados)
        return True

    def remove_allowlist(self, nome: str) -> bool:
        dados = self.allowlist()
        alvo = nome.lower()
        novo = [i for i in dados if str(i.get("name", "")).lower() != alvo]
        if len(novo) == len(dados):
            return False
        self._salva_lista(self.allowlist_path, novo)
        return True

    def set_permissao(self, nome: str, xuid: str, nivel: str) -> bool:
        if nivel not in ("visitor", "member", "operator"):
            raise PropertyError("permissao tem que ser visitor, member ou operator")
        dados = self.permissoes()
        for item in dados:
            if str(item.get("xuid", "")) == str(xuid):
                if item.get("permission") == nivel:
                    return False
                item["permission"] = nivel
                self._salva_lista(self.permissions_path, dados)
                return True
        dados.append({"xuid": str(xuid), "permission": nivel})
        self._salva_lista(self.permissions_path, dados)
        return True

    def clear_permissao(self, nome: str, xuid: str) -> bool:
        dados = self.permissoes()
        novo = [i for i in dados if str(i.get("xuid", "")) != str(xuid)]
        if len(novo) == len(dados):
            return False
        self._salva_lista(self.permissions_path, novo)
        return True

    def xuid_no_log(self, nome: str, log_txt: str) -> str | None:
        """Acha o XUID do jogador no log do BDS (ele imprime quando o cara entra)."""
        alvo = nome.lower()
        for linha in reversed(log_txt.splitlines()):
            if alvo in linha.lower():
                achado = XUID_RE.search(linha)
                if achado:
                    return achado.group(0)
        return None
