"""Controle do servidor: server.properties, allowlist.json, permissions.json e kicks.

O bot e o dono do /data/server.properties. O image nao reescreve o arquivo
inteiro: o bedrock-entry.sh so joga nele as propriedades que tem variavel de
ambiente SETADA, entao o que o bot grava fica. As variaveis do compose que
brigariam com o arquivo (GAMEMODE, DIFFICULTY...) foram deixadas de fora de
proposito, para o /config do Telegram ser a unica fonte.

Duas chaves sao excecao e estao em DO_COMPOSE: LEVEL_NAME e SERVER_UDP_PORTS.
La quem manda e o compose, porque a imagem reescreve as duas em todo boot. O
bot ainda deixa o /config escrever no arquivo (para o admin ver o valor
mudando), mas nao guarda override delas: o reconciliador ficaria reescrevendo
algo que o boot seguinte desfaz.
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


def caminho_backup(caminho: Path) -> Path:
    """Nome unico de backup: carimbo de segundo + contador de 3 digitos.

    So o carimbo nao bastava: o reconciliador grava varias chaves em sequencia
    (e o /config pode mandar varias de uma vez) e tudo isso acontece no mesmo
    segundo. Sem o contador a segunda gravacao sobrescrevia o backup da
    primeira e o historico do arquivo ficava com buracos. Os 3 digitos sao
    fixos de proposito: e o que mantem a ordem cronologica quando o prune
    ordena os backups por nome.
    """
    carimbo = int(time.time())
    for tentativa in range(1000):
        alvo = caminho.with_suffix(caminho.suffix + f".{carimbo}{tentativa:03d}{BACKUP_SUFFIX}")
        if not alvo.exists():
            return alvo
    raise PropertyError(f"nao consegui achar um nome livre para o backup de {caminho.name}")


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
        cuidado="o compose publica 19132/tcp: mudar isso derruba o acesso ate voce mudar o compose tambem",
    ),
    Prop(
        "level-name",
        "Nome da pasta do mundo",
        "str",
        cuidado=(
            "o LEVEL_NAME do compose manda nesta: a imagem reescreve level-name "
            "em todo boot, entao para valer tem que mudar la tambem (e mover a "
            "pasta do mundo, que o nome so passa a valer no boot seguinte)"
        ),
    ),
    Prop("online-mode", "Autenticacao Microsoft", "bool", cuidado="desligar quebra o permissions.json (precisa de XUID)"),
    Prop(
        "server-udp-ports",
        "Faixa UDP do nethernet",
        "portas",
        cuidado=(
            "o SERVER_UDP_PORTS do .env manda nesta: a imagem reescreve em todo "
            "boot, entao o que eu gravo aqui so vale ate o proximo restart. "
            "Mude la (e no host/firewall.sh) para valer. Atras de firewall o "
            "nethernet so conecta com faixa fixa e 1:1 entre externo e interno"
        ),
    )
    # "transport" fica de fora de proposito: no BDS 1.26.52+ o nethernet e o
    # unico transporte suportado e o proprio servidor aborta a conexao se
    # transport=raknet. Expor a chave seria um jeito de derrubar o acesso de
    # todo mundo sem querer.
)

POR_CHAVE = {p.key: p for p in CATALOGO}

# Propriedades em que quem manda e o compose. Sao as duas que o entry script
# precisa ler do ambiente para funcionar (o nome da pasta do mundo e o
# endereco anunciado do nethernet), entao a imagem as escreve no
# server.properties em todo boot. Um override do bot aqui seria um
# alinhamento impossivel: o reconciliador escreveria a cada 60s e o boot
# seguinte desfaria.
DO_COMPOSE: frozenset[str] = frozenset({"level-name", "server-udp-ports"})


def do_compose(chave: str) -> bool:
    """Essa propriedade e reescrita pelo compose em todo boot?"""
    return chave in DO_COMPOSE


def comando_ao_vivo(chave: str, valor: str) -> str:
    """O comando de console que aplica a propriedade sem reiniciar ('' se nao ha).

    Vale para as duas que o BDS le ao vivo. O resto (dificuldade, distancia de
    visao, gamemode) so entra no proximo boot, e nao ha comando para isso: a
    doc do BDS so traz changesetting para allow-cheats e difficulty.
    """
    if chave == "allow-list":
        # A doc e explicita: allowlist on/off liga e desliga em runtime e "does
        # not change the value in the server.properties file". Por isso o
        # /config continua gravando o arquivo tambem - o comando sozinho
        # resolveria so ate o proximo boot.
        return "allowlist on" if valor == "true" else "allowlist off"
    return ""


def cita(nome: str) -> str:
    """Como o gamertag tem que ir num comando de console do BDS.

    A doc oficial: "If there is a white-space in the Gamertag you need to
    enclose it with double quotes". Sem aspas, 'allowlist add Example Name'
    vira duas palavras e o servidor nunca acha o jogador.
    """
    return f'"{nome}"' if any(c.isspace() for c in nome) else nome


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

    def _podar_backups(self, caminho: Path) -> None:
        """Mantem so os backup_keep mais novos de um arquivo.

        Sem isso o /data enche: toda gravacao de allow-list e de permissions
        deixa um .bak para tras e ninguem limpava.
        """
        velhos = sorted(self.data_dir.glob(caminho.name + ".*" + BACKUP_SUFFIX))
        for velho in velhos[: max(0, len(velhos) - self.backup_keep)]:
            velho.unlink(missing_ok=True)

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
        alvo = caminho_backup(self.props_path)
        shutil.copy2(self.props_path, alvo)
        self._podar_backups(self.props_path)
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
            shutil.copy2(caminho, caminho_backup(caminho))
            self._podar_backups(caminho)
        caminho.write_text(json.dumps(dados, indent=2) + "\n", encoding="utf-8")

    def allowlist(self) -> list[dict]:
        return self._lista(self.allowlist_path)

    def permissoes(self) -> list[dict]:
        return self._lista(self.permissions_path)

    def add_allowlist(self, nome: str, xuid: str | None = None, log_txt: str | None = None) -> bool:
        """True se entrou como novo.

        Caminho de reserva: o jeito normal de liberar alguem e o console
        ('allowlist add'), porque o BDS resolve o XUID sozinho e salva a lista
        no formato dele. Isto aqui so serve quando o console nao responde
        (Docker Desktop), e ai o arquivo precisa mudar na mao.

        O XUID e opcional de proposito, nao por preguiça: a doc do BDS diz que
        "if it's not set then it will be populated when someone with a matching
        name connects". Exigir o numero aqui barraria exatamente quem nunca
        conseguiu entrar, que e quem precisa da permissao - e o XUID adivinhado
        no log, quando errado, e pior do que nenhum: o BDS valida a entrada por
        ele.

        Um XUID que ja esta na entrada nunca e trocado. Quem preenche a lista e
        o proprio BDS, nao a gente; se o numero informado divergir do que o
        servidor gravou, um dos dois esta errado, e o servidor ganha a duvida.
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
                self._salva_lista(self.allowlist_path, dados)
                return False

        # o BDS escreve as tres chaves, nessa ordem, e preenche o xuid depois.
        # Gravar so name deixaria um arquivo que nao parece com o que o
        # servidor escreve de volta.
        entrada: dict[str, object] = {"ignoresPlayerLimit": False, "name": nome}
        if xuid:
            entrada["xuid"] = str(xuid)
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
        """Acha o XUID do jogador no log do BDS (ele imprime quando o cara entra).

        Serve para o /ops, que escreve no permissions.json - la o XUID e
        obrigatorio mesmo. Para a allow-list nao: o BDS resolve o proprio na
        primeira conexao, e um numero adivinhado errado trava o jogador em vez
        de liberar.
        """
        alvo = nome.lower()
        for linha in reversed(log_txt.splitlines()):
            if alvo in linha.lower():
                achado = XUID_RE.search(linha)
                if achado:
                    return achado.group(0)
        return None
