"""Operacoes de alto nivel: reinicio com verificacao, status e o job das 05:00."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from . import addons, backup, dropbox, raknet, serverctl, txt
from .auth import Auth
from .config import Config
from .docker_ctl import DockerController
from .store import Store

if TYPE_CHECKING:  # so a anotacao; o bot so existe dentro do container do bot
    from aiogram import Bot

log = logging.getLogger("bds.ops")

BDS_IMAGE = "itzg/minecraft-bedrock-server:stable"


@dataclass
class AppContext:
    config: Config
    docker: DockerController
    staging: Path
    store: Store
    auth: Auth
    server: serverctl.ServerControl
    # ligado durante um restart pedido pelo proprio bot, para a vigia nao
    # duplicar o aviso que o /reiniciar ja mandou no chat.
    em_restart: bool = False
    # Task do "reinicio em 5 min" do /permitir. Vive aqui, e nao em variavel
    # solta, para o botao poder cancelar e para o shutdown do bot saber o que
    # deixar de lado. A lista dos nomes que dependem dela esta no bot.db
    # (store.liberacoes), entao um recreate do container nao perde o prazo.
    tarefa_liberacao: asyncio.Task | None = None
    # ligado entre o para() e o espera_pronto() da liberacao. Sem ele, um
    # botao apertado no meio do reinicio cancelaria a task e o container ficaria
    # parado: o stop do docker ja saiu da thread quando o cancelamento chega.
    liberacao_em_curso: bool = False

    @classmethod
    def build(cls, config: Config) -> "AppContext":
        config.staging_dir.mkdir(parents=True, exist_ok=True)
        store = Store(config.state_dir / "bot.db")
        return cls(
            config=config,
            docker=DockerController(config.bds_container, BDS_IMAGE),
            staging=config.staging_dir,
            store=store,
            auth=Auth(store, config.admin_claim_code),
            server=serverctl.ServerControl(config.data_dir, backup_keep=config.backup_keep),
        )

    def mundo(self) -> str:
        """Nome da pasta do mundo: server.properties manda, .env e so reserva."""
        return self.server.nivel_do_mundo(self.config.level_name)


async def notificar(bot: Bot, ctx: AppContext, texto: str) -> None:
    """Manda para os admins registrados.

    Mora aqui, e nao no main, porque a liberacao pendente do /permitir tambem
    precisa falar com o chat - e quando ela fala sozinha (o reinicio de 5 min
    disparando sozinho) nao existe mais nenhum handler esperando para responder.
    """
    for user in ctx.store.usuarios():
        if not user.eh_admin:
            continue
        try:
            await bot.send_message(user.user_id, texto)
            log.info("avisei %s (%d chars)", user.user_id, len(texto))
        except Exception as exc:
            log.warning("nao consegui avisar %s: %s", user.user_id, exc)


def overrides_sujo(ctx: AppContext) -> list[tuple[str, str, str]]:
    """Overrides do banco que o server.properties nao tem mais (drift).

    As chaves do compose ficam de fora: a imagem reescreve level-name e
    server-udp-ports em todo boot a partir do .env, entao reaplicar o override
    aqui so faria o reconciliador e o compose discordarem a cada 60s, com o BDS
    lendo o valor do compose no boot seguinte de qualquer jeito.
    """
    atual = ctx.server.le_props()
    return [
        (k, v, atual.get(k, ""))
        for k, v in ctx.store.overrides().items()
        if atual.get(k) != v and not serverctl.do_compose(k)
    ]


def aplica_overrides(ctx: AppContext) -> list[str]:
    reaplicados = []
    for chave, valor, _atual in overrides_sujo(ctx):
        ctx.server.set_prop(chave, valor)
        reaplicados.append(f"{chave}={valor}")
    return reaplicados


# O BDS escreve "[2026-09-28 16:41:21:830 INFO] texto" - o nivel vai DENTRO do
# colchete, e por isso some junto com o carimbo. O formato antigo trazia
# "[2026-09-28 12:00:00:123 UTC] [Server] texto", e o regex abaixo pega os dois:
# qualquer colchete inicial que comece com data. As tags [Server]/[Command] vem
# depois, em linha separada, e sao removidas a parte; [Error] e companhia
# ficam, porque ali a severidade e a informacao.
RE_CARIMBO = re.compile(r"^\s*\[\d{4}-\d{2}-\d{2}[^\]]*\]\s*")
TAGS_MUROS = ("[Server]", "[Server:Console]", "[Console]", "[Command]", "[Chat]", "[Scripting]")


def limpa_linha_console(linha: str) -> str:
    texto = RE_CARIMBO.sub("", linha).strip()
    # As tags podem vir empilhadas ("[Server] [Command] list"), entao o corte
    # e em loop: parar na primeira deixaria "[Command] list" no meio da
    # resposta, e o filtro de eco nao pegaria.
    trocou = True
    while trocou:
        trocou = False
        for tag in TAGS_MUROS:
            if texto.startswith(tag):
                texto = texto[len(tag) :].strip()
                trocou = True
                break
    return texto


def limpa_resposta(comando: str, linhas: list[str]) -> list[str]:
    """As linhas da resposta sem carimbo, sem tag de parede e sem eco do comando."""
    corpo: list[str] = []
    for bruta in linhas:
        limpa = limpa_linha_console(bruta)
        if not limpa or limpa.lower() == comando.lower():
            continue
        if corpo and limpa == corpo[-1]:
            continue
        corpo.append(limpa)
    return corpo


def resposta_console(comando: str, linhas: list[str], erro: str = "", limite: int = 3500) -> str:
    """Monta a resposta do console para o Telegram.

    Filtra o eco do proprio comando (o BDS devolve "[Command] list" no log) e
    linhas repetidas, que alguns comandos geram a cada jogador listado.
    """
    cabecalho = txt.cabecalho(txt.CONSOLE, f"console: {comando}")
    if erro:
        return "\n".join([cabecalho, "", txt.erro("O comando nao chegou no console."), "", txt.sub([erro])])

    corpo = limpa_resposta(comando, linhas)

    if not corpo:
        return "\n".join(
            [
                cabecalho,
                "",
                txt.info("O console nao devolveu nada."),
                "",
                txt.sub(
                    [
                        "Comandos como 'list', 'help', 'tps' e 'gamerule' respondem.",
                        "Os que nao falam nada (save-resume, stop) sao assim mesmo.",
                    ]
                ),
            ]
        )
    texto = "\n".join(corpo)
    if len(texto) > limite:
        texto = "... (inicio cortado)\n" + texto[-(limite - 24) :]
    return f"{cabecalho}\n\n{texto}"


# --------------------------------------------------------------- allow-list

# O console e a fonte da verdade da allow-list, e o arquivo e a reserva. Isso
# nao e preciosismo: o BDS reescreve allowlist.json quando desliga, a partir da
# lista que tem em memoria. Quem escreve no arquivo sem o servidor saber
# (allowlist reload recusado, comando nunca entregue) tem a entrada apagada no
# proximo stop - que e o "adicionei a pessoa e ela sumiu da lista depois de um
# restart".

# O que um /permitir pode ter conseguido. O bot nao diz "liberado" sem saber em
# qual destes ele caiu: a diferenca entre eles e se o jogador entra ou nao.
LIBERADO = "servidor"  # o console adicionou e o 'allowlist list' confirmou
LIBERADO_BOOT = "boot"  # so no arquivo, mas o BDS esta parado: o boot le
LIBERADO_PENDENTE = "pendente"  # no arquivo com o BDS rodando: precisa reiniciar
LIBERADO_FALHOU = "falhou"  # nem no console nem no arquivo: nao foi pra lugar nenhum

RE_SO_NUMEROS = re.compile(r"^[\d\s()\-]+$")


def allowlist_no_console(ctx: AppContext, *args: str) -> tuple[str, list[str]]:
    """Manda 'allowlist ...' no console do BDS.

    Devolve (erro, linhas limpas). Erro vazio = o comando chegou no servidor.
    As respostas que interessam sao 'list' (o que ele tem carregado) e 'add' /
    'remove'; 'on', 'off' e 'reload' sao silenciosos, como o /console avisa.
    """
    comando = "allowlist " + " ".join(args)
    erro, linhas = ctx.docker.console(comando)
    if erro:
        return erro, []
    return "", limpa_resposta(comando, linhas)


def _casa(linha: str, nome: str) -> bool:
    """Esta linha do 'allowlist list' e' a entrada do jogador?"""
    limpa = linha.strip().lstrip("*-").strip().strip('"').strip()
    alvo = nome.strip().strip('"').lower()
    if limpa.lower() == alvo:
        return True
    # o BDS pode repetir o xuid na mesma linha ("Ze (2535463291192118)"). So
    # aceita quando o nome ocupa a linha INTEIRA ate o separador e o que sobra
    # e' so numero: e o que separa "Ze: 2535..." (a lista) de "Ze: ola" (chat).
    for sep in (" - ", ":", " ("):
        if sep not in limpa:
            continue
        nome_linha, _, resto = limpa.partition(sep)
        if nome_linha.strip().lower() == alvo and RE_SO_NUMEROS.match(resto.strip()):
            return True
    return False


def confirmado(linhas: list[str], nome: str) -> bool:
    """O servidor diz que tem este jogador na lista?

    Errar para "nao" e de graca: o /permitir avisa e o admin confere no /lista.
    Errar para "sim" e' o bug que este projeto estava tendo - a versao anterior
    procurava o nome com `in` em qualquer linha, e a janela de log que o
    console() devolve traz chat, entrada de jogador e o proprio eco do comando.
    Um "ola" no chat do Ze confirmava a liberacao de quem o Ze nao esta.
    """
    return any(_casa(linha, nome) for linha in linhas)


@dataclass
class EstadoLista:
    """O retrato da allow-list de tres fontes, que precisam concordar entre si.

    Antes o /lista, o /status e o /permitir montavam cada um a sua versao, e
    eles discordavam - que e como o bot acabava dizendo "liberado" olhando o
    arquivo enquanto o servidor consultava outra coisa.
    """

    erro_console: str = ""
    linhas_servidor: list[str] = field(default_factory=list)
    entradas_arquivo: list[dict] = field(default_factory=list)
    erro_arquivo: str = ""
    allow_list_arquivo: str = ""
    whitelist_antigo: bool = False
    pendentes: list[str] = field(default_factory=list)
    online_mode: bool = True

    @property
    def servidor_consultavel(self) -> bool:
        """Da para perguntar ao servidor o que ele tem?"""
        return not self.erro_console

    def entradas_presas(self) -> list[str]:
        """Quem esta na lista com xuid enquanto o servidor nao autentica ninguem.

        Sao entradas que o proprio jogador nao consegue destravar: sem conta
        Microsoft o cliente nao tem XUID, e o servidor casa a entrada por um
        numero que nunca vai bater. Some com isso e o nome volta a valer.
        """
        if self.online_mode:
            return []
        return [
            str(item.get("name", "")).strip()
            for item in self.entradas_arquivo
            if item.get("xuid") and str(item.get("name", "")).strip()
        ]

    def tem_no_servidor(self, nome: str) -> bool:
        return confirmado(self.linhas_servidor, nome)

    def so_no_arquivo(self) -> list[str]:
        """Quem esta gravado mas o servidor nao tem: nao entra no jogo.

        So quando o console respondeu. Sem ele nao da para saber o que o
        servidor tem, e afirmar "voce nao esta na lista" seria inventar.
        """
        if not self.servidor_consultavel:
            return []
        return [
            nome
            for nome in (str(item.get("name", "")).strip() for item in self.entradas_arquivo)
            if nome and not self.tem_no_servidor(nome)
        ]


def estado_real(ctx: AppContext) -> EstadoLista:
    """Le a allow-list de onde cada uma das tres fontes diz que esta.

    Faz tres leituras bloqueantes (console, arquivo, propriedades): sempre por
    asyncio.to_thread na chamada. O console e' o mais caro - leva ate 3s quando
    o comando nao responde nada.
    """
    erro_console, linhas = allowlist_no_console(ctx, "list")
    try:
        entradas = ctx.server.allowlist()
    except serverctl.PropertyError as exc:
        entradas, erro_arquivo = [], str(exc)
    else:
        erro_arquivo = ""
    return EstadoLista(
        erro_console=erro_console,
        linhas_servidor=linhas,
        entradas_arquivo=entradas,
        erro_arquivo=erro_arquivo,
        allow_list_arquivo=ctx.server.le_props().get("allow-list", ""),
        whitelist_antigo=ctx.server.tem_whitelist_json(),
        pendentes=[r["name"] for r in ctx.store.liberacoes()],
        online_mode=ctx.server.online_mode(),
    )


@dataclass
class Liberacao:
    estado: str
    aviso: str = ""


async def poe_na_lista(ctx: AppContext, nome: str, xuid: str | None = None) -> Liberacao:
    """Coloca o jogador na allow-list. Devolve em que estado ela ficou.

    A ordem e' add -> reload -> list, e o meio dela e' o que faz a diferenca.
    A doc do BDS descreve o `add` como mexer no *arquivo*, e so o `reload` como
    o comando que "makes the server reload the allowlist from the file" - sem
    ele a edicao fica no disco esperando um boot. Antes desta ordem, todo
    `/permitir` que caia no caminho de arquivo acabava em LIBERADO_PENDENTE e
    derrubava o servidor 5 minutos depois para um reload que cabe em um comando.

    O console vem primeiro porque o `allowlist add` e' o caminho que sobrevive:
    o BDS grava a lista no formato dele. O arquivo so e' tocado quando o console
    nao entrega o nome, e nesse caso um reload resolve - o boot, nao.

    Devolve sempre um estado, nunca um "ok" vago: a diferenca entre LIBERADO e
    LIBERADO_PENDENTE e exatamente a diferenca entre o jogador entrar agora e o
    jogador ficar na porta.
    """
    erro, _linhas = await asyncio.to_thread(allowlist_no_console, ctx, "add", serverctl.cita(nome))
    if not erro:
        # O reload vai sempre depois do add, mesmo quando o console ja pareceu
        # aceitar. E' de graca (o comando e' silencioso) e cobre o caso em que o
        # add so gravou o arquivo, que e' o unico jeito de o bot dizer
        # "liberado" sem restart.
        await asyncio.to_thread(allowlist_no_console, ctx, "reload")
        _e, listadas = await asyncio.to_thread(allowlist_no_console, ctx, "list")
        if confirmado(listadas, nome):
            return Liberacao(LIBERADO)
        # O console aceitou e a lista nao mostra o nome. A causa mais comum nao
        # e' console mudo: e' entrada antiga no arquivo com XUID, porque o BDS
        # valida por ele e nunca re-resolve o nome. Tirar o campo resolve.
        if await asyncio.to_thread(ctx.server.corrige_xuid, nome):
            await asyncio.to_thread(allowlist_no_console, ctx, "reload")
            _e, listadas = await asyncio.to_thread(allowlist_no_console, ctx, "list")
            if confirmado(listadas, nome):
                return Liberacao(
                    LIBERADO,
                    "A entrada estava presa num XUID velho e o servidor nao ia liberar ninguem com "
                    "ele. Tirei o campo e ele resolveu o nome de novo.",
                )
        return await _so_no_arquivo(
            ctx, nome, xuid, erro, "o console aceitou o comando, mas 'allowlist list' nao devolveu o nome"
        )

    return await _so_no_arquivo(ctx, nome, xuid, erro, "o console nao respondeu")


async def cura_offline(ctx: AppContext) -> list[str]:
    """Deixa a lista usavel sem conta Microsoft. Devolve quem foi corrigido.

    So age com `online-mode` desligado, e mesmo assim so tira xuid: com
    autenticacao o campo e' preenchido pelo servidor e o nome continua valendo.
    Sem essa cura, um allowlist.json herdado de quando o servidor exigia conta
    fica com o dono trancado fora do proprio servidor, sem erro nenhum - que e
    o pior tipo de bug, o que so aparece quando alguem tenta entrar.
    """
    if await asyncio.to_thread(ctx.server.online_mode):
        return []
    mudados = await asyncio.to_thread(ctx.server.purga_xuids)
    if mudados:
        await asyncio.to_thread(allowlist_no_console, ctx, "reload")
    return mudados


async def _so_no_arquivo(
    ctx: AppContext, nome: str, xuid: str | None, erro: str, explicacao: str
) -> Liberacao:
    """O console nao serviu: grava no arquivo, manda o reload e confere.

    Este era o caminho que agendava o reinicio sempre, e a ideia era razoavel
    enquanto se acreditava que so o boot lia o arquivo. A doc do BDS diz o
    contrario: "After you've modified the file you need to run the command
    allowlist reload to make sure that the server knows about your new change".
    Entao aqui tambem vale a sequencia arquivo -> reload -> list, e o
    LIBERADO_PENDENTE (que derruba o servidor) sobra so para o caso em que o
    proprio reload nao pega - que e' o console mudo de verdade, e nao um
    detalhe de formatacao.

    O que sobra do agendamento e o motivo de o timer continuar existindo: se o
    console nao responde nem para receber o reload, nao ha caminho curto, e o
    boot le o arquivo com certeza.
    """
    try:
        await asyncio.to_thread(ctx.server.add_allowlist, nome, xuid)
    except serverctl.PropertyError as exc:
        # allowlist.json ilegivel. Nao adianta agendar reinicio: sem arquivo
        # nao ha o que o boot ler, e o bot nao pode marcar pendencia que ele
        # mesmo nao vai conseguir cumprir.
        return Liberacao(LIBERADO_FALHOU, f"Nao consegui gravar no allowlist.json: {exc}")

    # O reload vai sempre, e nao so quando o console errou: mesmo tendo saido
    # limpo do `add`, o que o servidor tem em memoria pode ser a lista velha.
    erro_reload, _l = await asyncio.to_thread(allowlist_no_console, ctx, "reload")
    if not erro_reload:
        _e, listadas = await asyncio.to_thread(allowlist_no_console, ctx, "list")
        if confirmado(listadas, nome):
            return Liberacao(
                LIBERADO,
                "O console nao respondeu bem, entao gravei no allowlist.json e mandei o "
                "'allowlist reload': o servidor ja esta com a lista nova, sem precisar reiniciar.",
            )

    try:
        rodando = ctx.docker.state().running
    except Exception:  # noqa: BLE001 - o aviso nao pode falhar por causa do docker
        rodando = True
    if not rodando:
        return Liberacao(
            LIBERADO_BOOT,
            f"Como o servidor esta parado, gravei no allowlist.json e o proximo boot ja le isso ({explicacao}: {erro}).",
        )
    return Liberacao(
        LIBERADO_PENDENTE,
        f"Gravei no allowlist.json, mas nem o reload pegou ({explicacao}: {erro or erro_reload}) e o "
        "servidor esta rodando: enquanto isso a entrada nao vale, porque ele consulta a lista que tem "
        "em memoria. Vou reiniciar em instantes para o boot ler o arquivo.",
    )


async def tira_da_lista(ctx: AppContext, nome: str) -> tuple[bool, str]:
    """Tira o jogador da allow-list. Devolve (saiu, o que o admin precisa saber)."""
    erro, _linhas = await asyncio.to_thread(allowlist_no_console, ctx, "remove", serverctl.cita(nome))
    # Sai da fila de pendencia: nao faz sentido o bot reiniciar o servidor
    # daqui a 5 min para liberar quem o admin acabou de tirar.
    ctx.store.apaga_liberacao(nome)
    if not erro:
        return True, ""
    removido = await asyncio.to_thread(ctx.server.remove_allowlist, nome)
    return removido, (
        f"Nao consegui falar com o console do BDS: {erro}. "
        + (
            "Tirei do allowlist.json, e o proximo boot ja le isso."
            if not ctx.docker.state().running
            else "Tirei do allowlist.json, mas o servidor esta rodando: o proximo stop reescreve "
            "o arquivo e o jogador volta a estar na lista. O jeito que segura e o console."
        )
    )


async def liga_a_lista(ctx: AppContext, by: int) -> str:
    """Liga a allow-list no arquivo e no servidor. Devolve um aviso, se houver.

    Sem isso o /permitir nao libera ninguem: a propriedade e lida so no boot, e
    enquanto ela estiver desligada a lista nao bloqueia nem libera ninguem.

    O 'allowlist on' vai sempre, mesmo com o arquivo ja em true, e nao so quando
    o valor muda: a propriedade e' lida no boot, mas o BDS reescreve
    allow-list no shutdown a partir do estado de runtime. Depois de um stop
    dessas o arquivo pode estar true e o servidor ter ficado ligado desde um
    boot em que estava false - e ai o bot acha que a lista esta no ar sem
    estar.
    """
    props = await asyncio.to_thread(ctx.server.le_props)
    ja_no_arquivo = props.get("allow-list") == "true"
    if not ja_no_arquivo:
        await asyncio.to_thread(ctx.server.set_prop, "allow-list", "true")
        ctx.store.set_override("allow-list", "true", by)
    erro, _linhas = await asyncio.to_thread(allowlist_no_console, ctx, "on")
    if erro:
        if ja_no_arquivo:
            return (
                f"allow-list=true no arquivo, mas o console nao aceitou o 'allowlist on' ({erro}). "
                "Enquanto o console mudo so o boot liga a lista."
            )
        return f"Gravei allow-list=true, mas o console nao aceitou o 'allowlist on' ({erro})."
    if not ja_no_arquivo:
        return "A lista estava desligada; liguei agora e tambem gravei allow-list=true, entao o proximo boot ja vem ligada."
    return ""


# -------------------------------------------------- liberacao com reinicio

def _prazo_texto(segundos: int, tz: ZoneInfo) -> str:
    """'em 5 min (14:37)': o relativo e o horario, porque a mensagem fica na tela.

    O horario e' do fuso do servidor, e nao UTC: as duas informacoes batem com
    o relogio do admin, e o prazo e' sempre o do primeiro pedido - que e o que
    `restante()` calcula, e nao `segundos` de novo.
    """
    relativo = f"em {max(1, round(segundos / 60))} min" if segundos >= 60 else "daqui a pouco"
    return f"{relativo} ({datetime.now(tz).strftime('%H:%M')})"


def restante(ctx: AppContext) -> int:
    """Quantos segundos faltam para o prazo, medido da primeira marcacao.

    E' da mais antiga, e nao da ultima: um segundo /permitir nao estica o prazo
    do primeiro, senao o admin perderia o horario que ele leu na tela.
    """
    pendentes = ctx.store.liberacoes()
    if not pendentes:
        return 0
    mais_velho = min(r["at"] for r in pendentes)
    return max(0, int(mais_velho + ctx.config.allowlist_grace_seconds - time.time()))


async def libera_reiniciando(ctx: AppContext, nomes: list[str], by: int | None = None) -> str:
    """Faz a liberacao valer de verdade: para, regrava o arquivo, sobe.

    A ordem nao e' detalhe, e' o mecanismo inteiro:

    1. `para()` so volta quando o container morreu, e o BDS reescreve o
       allowlist.json no shutdown a partir da lista que tem em memoria. Entao o
       arquivo ja foi reescrito quando a escrita nova acontece, e a entrada
       sobrevive em vez de ser apagada. Escrever antes do stop seria o jeito
       certo de perder a liberacao - era o que o aviso antigo ensinava.
    2. allow-list=true e reafirmado pelo mesmo motivo: o mesmo stop grava o
       valor de runtime, e ele pode ter virado false.
    3. `liga()` e espera_pronto: o boot le o arquivo, a lista entra na memoria
       do servidor, e de la em diante quem mantem a lista e' o proprio BDS.

    A trava de reinicio e a mesma do /reiniciar e do backup: dois stops no
    mesmo minuto se atropelam, e o primeiro reporta "o servidor nao subiu" sem
    ele ter subido ainda. O em_restart liga para a vigia nao mandar "o servidor
    caiu" durante uma parada que o proprio bot pediu.
    """
    nomes = [n for n in dict.fromkeys(nomes) if n.strip()]
    if not nomes:
        return txt.info("Nao ha liberacao pendente.")
    async with _restart_lock(ctx):
        try:
            ctx.em_restart = True
            return await _libera_pendente(ctx, nomes, by)
        except Exception as exc:
            # NUNCA levanta: quem chamou ja mandou "reiniciando..." e uma
            # excecao subindo deixaria o admin olhando para o silencio. E o
            # log.exception (e nao o raise) que faz o Codigo terminar com um
            # relatorio em vez de um traceback no Telegram.
            log.exception("libera_reiniciando levantou")
            return _falhou(
                f"liberar {', '.join(nomes)}",
                f"Falha inesperada durante a liberacao: {exc}",
                "Veja: docker compose logs bds",
            )
        finally:
            ctx.em_restart = False


async def _libera_pendente(ctx: AppContext, nomes: list[str], by: int | None) -> str:
    cfg = ctx.config
    quem = ", ".join(nomes)

    if cfg.announce_seconds > 0:
        await asyncio.to_thread(
            ctx.docker.say, f"[bot] liberando {quem} - reiniciando em {cfg.announce_seconds}s"
        )
        await asyncio.sleep(cfg.announce_seconds)

    ctx.liberacao_em_curso = True
    try:
        # O para() fica dentro do try de proposito. Se o bot levar um SIGTERM ou
        # for cancelado bem no meio (o shutdown cancela as tasks), o `liga` do
        # finally roda do mesmo jeito: perder a liberacao e' aceitavel, deixar o
        # BDS parado nao e - o boot e' o que faz o jogo voltar, e ele nao
        # depende do processo do bot.
        await asyncio.to_thread(ctx.docker.para)

        problema = ""
        try:
            await asyncio.to_thread(ctx.server.set_prop, "allow-list", "true")
        except serverctl.PropertyError as exc:
            problema = f"O server.properties nao pode ser lido com o servidor parado: {exc}"
        else:
            ctx.store.set_override("allow-list", "true", by or 0)
        if not problema:
            for nome in nomes:
                await asyncio.to_thread(ctx.server.add_allowlist, nome)
                ctx.store.audita(by, "permitir", f"{nome} (liberado no reinicio)")
            # So agora a pendencia sai: enquanto a escrita nao aconteceu, ela
            # continua marcada e o proximo /permitir tenta de novo.
            ctx.store.limpa_liberacoes()
    finally:
        # Em qualquer caminho, inclusive numa escrita que explodiu no meio: o
        # servidor parado por causa do bot e' pior do que a liberacao perdida.
        try:
            await asyncio.to_thread(ctx.docker.liga)
        finally:
            ctx.liberacao_em_curso = False

    if problema:
        return _falhou(
            f"liberar {quem}",
            problema,
            "Subi o servidor de volta. A liberacao continua pendente: mande /permitir de novo.",
        )

    pronto, alerta, _ping = await espera_pronto(ctx)
    if alerta or not pronto:
        # A lista esta no arquivo e o proximo boot le: a liberacao vale, mas o
        # bot nao pode dizer que o servidor esta de pe.
        return _relatorio(
            f"liberar {quem}",
            "liberacao gravada, servidor nao voltou",
            [
                txt.aviso(alerta or f"o BDS nao imprimiu 'Server started.' em {cfg.boot_timeout}s"),
                "",
                txt.campo("Arquivo", f"{quem} esta no allowlist.json e entra no proximo boot"),
                txt.campo("Diagnostico", "docker compose logs bds"),
            ],
            icone=txt.AVISO,
        )

    corpo = [
        f"{txt.OK} {quem} entrou na lista de verdade.",
        "",
        txt.campo("Como", "parei o servidor, gravei no allowlist.json com ele parado e subi de novo"),
        txt.campo("Lista", "o BDS leu o arquivo no boot, entao quem mantem a lista agora e ele"),
    ]
    if ctx.server.tem_whitelist_json():
        corpo += [
            "",
            txt.aviso(
                "Continua um whitelist.json no /data: ele tem preferencia sobre o allowlist.json "
                "e pode continuar mandando. Apague esse arquivo."
            ),
        ]
    return _relatorio(f"liberar {quem}", "liberacao salva", corpo)


async def agenda_liberacao(ctx: AppContext, nomes: list[str], by: int, bot: Bot) -> str:
    """Anota a liberacao pendente e arma o 'eu reinicio em N minutos'.

    O prazo e' do primeiro, nao do ultimo: um segundo /permitir no mesmo estado
    entra na fila do reinicio que ja estava marcado, senao um jogador novo
    empurraria a liberacao dos outros e o admin perderia o prazo que ele leu na
    tela. Os nomes ficam no bot.db, entao um recreate do bot no meio da espera
    nao apaga a promessa - a pendencia volta no boot seguinte.
    """
    for nome in nomes:
        ctx.store.marca_liberacao(nome, by)
    segundos = ctx.config.allowlist_grace_seconds
    ja_rodando = ctx.tarefa_liberacao is not None and not ctx.tarefa_liberacao.done()
    if segundos <= 0:
        return txt.info("So pelo botao: eu nao reinicio sozinho neste servidor.")
    if ja_rodando:
        # Nao estica o prazo: um segundo /permitir entra na fila do reinicio que
        # ja estava marcado, e o horario que aparece e o do primeiro.
        return (
            f"Ja esta marcado: eu reinicio {_prazo_texto(restante(ctx), ctx.config.tz)} assim mesmo. "
            "Nao precisa fazer mais nada."
        )
    ctx.tarefa_liberacao = asyncio.create_task(_espera_liberacao(ctx, bot, segundos))
    return (
        f"Nao preciso da sua confirmacao: {_prazo_texto(segundos, ctx.config.tz)} eu reinicio assim "
        "mesmo. O botao so antecipa."
    )


def cancela_agenda(ctx: AppContext) -> bool:

    """Corta o prazo pendente (o botao do admin). Devolve True se havia um.

    Sem isso o botao e o timer iam derrubar o servidor duas vezes: o timer
    acorda, nao acha mais pendencia (o botao ja limpou) e sai com um "nada a
    reiniciar" - ou, pior, acorda no meio do restart do botao e se atropela no
    _restart_lock esperando a vez.
    """
    tarefa = ctx.tarefa_liberacao
    if ctx.liberacao_em_curso or tarefa is None or tarefa.done():
        return False
    tarefa.cancel()
    return True


async def _espera_liberacao(ctx: AppContext, bot: Bot, segundos: int) -> None:
    try:
        try:
            await asyncio.sleep(segundos)
        except asyncio.CancelledError:
            log.info("prazo cancelado: o admin antecipou pelo botao")
            return
        relatorio = await dispara_pendente(ctx)
        await notificar(bot, ctx, relatorio)
    except Exception:
        # Uma task nao pode morrer em silencio: o admin precisa do relatorio
        # (ou do log) do que o timer dele fez.
        log.exception("falha na liberacao automatica")
    finally:
        ctx.tarefa_liberacao = None


async def dispara_pendente(ctx: AppContext) -> str:
    """Reinicia agora por causa do prazo (ou do botao). Devolve o relatorio."""
    if ctx.liberacao_em_curso:
        return txt.info("A liberacao ja esta em andamento: eu te aviso quando o servidor voltar.")
    pendentes = ctx.store.liberacoes()
    if not pendentes:
        return txt.info("Nao ha liberacao pendente: nada a reiniciar.")
    nomes = [r["name"] for r in pendentes]
    by = next((r["by"] for r in pendentes if r["by"]), None)

    # Antes de derrubar o servidor, pergunta. Se o console voltou e ja tem os
    # nomes, o problema era outro (uma rede, um restart) e o downtime seria de
    # graça.
    erro, listadas = await asyncio.to_thread(allowlist_no_console, ctx, "list")
    if not erro and all(confirmado(listadas, nome) for nome in nomes):
        ctx.store.limpa_liberacoes()
        return txt.ok(
            "O console voltou e o servidor ja tem " + ", ".join(nomes) + ": cancelei o reinicio."
        )
    return await libera_reiniciando(ctx, nomes, by)


# O menor prazo que um rearme aceita. Sem ele, um bot que subiu depois do prazo
# estourado derrubaria o BDS no meio do boot, porque o /permitir e o boot do
# servidor costumam acontecer juntos (compose up do bot logo apos o do BDS).
ESPERA_MINIMA_REARME = 30


def rearma_pendencia(ctx: AppContext, bot: Bot) -> None:
    """Devolve a promessa do /permitir depois que o container do bot renasceu.

    A task nao sobrevive a um recreate, mas o bot.db atravessa o downtime - e e
    por isso que a pendencia mora la e nao em memoria. Sem este rearme, um
    /permitir feito dois minutos antes do recreate nunca reiniciava: o prazo
    sumia junto com o container e o jogador ficava na porta sem ninguem
    prometendo nada.

    O prazo continua correndo pelo `at` original (o mais antigo), e nao recomeca
    no boot: a promessa foi "daqui a 5 minutos", e quem leu aquilo no Telegram
    conta o tempo do relogio dele.
    """
    pendentes = ctx.store.liberacoes()
    if not pendentes or ctx.config.allowlist_grace_seconds <= 0:
        return
    if ctx.tarefa_liberacao is not None and not ctx.tarefa_liberacao.done():
        return
    faltam = restante(ctx)
    if faltam < ESPERA_MINIMA_REARME:
        log.warning(
            "prazo da liberacao de %s vencido ou a menos de %ds: vai reiniciar assim que der",
            ", ".join(r["name"] for r in pendentes),
            ESPERA_MINIMA_REARME,
        )
        faltam = ESPERA_MINIMA_REARME
    ctx.tarefa_liberacao = asyncio.create_task(_espera_liberacao(ctx, bot, faltam))
    log.info("liberacao rearmada: %s em %ds", ", ".join(r["name"] for r in pendentes), faltam)


def _fmt_uptime(started_at: str) -> str:
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    delta = datetime.now(timezone.utc) - started.astimezone(timezone.utc)
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    minutes = rem // 60
    return f"{hours}h{minutes:02d}min" if hours else f"{minutes}min"


async def status_lines(ctx: AppContext) -> list[str]:
    cfg = ctx.config
    state = await asyncio.to_thread(ctx.docker.state)
    version = await asyncio.to_thread(ctx.docker.server_version)
    ping = await raknet.ping_async(cfg.bds_host, cfg.bds_port)
    props = await asyncio.to_thread(ctx.server.le_props)
    transporte = props.get("transport", "?")

    if ping.motd_ok:
        icone_ping = txt.OK
        linha_ping = ping.resumo()
    else:
        # No nethernet a 19132 nem e bindada, entao o ping sempre da timeout e
        # isso NAO e sinal de servidor quebrado. O que vale e o log.
        pronto, alerta = await asyncio.to_thread(ctx.docker.pronto)
        if alerta:
            icone_ping = txt.ERRO
            linha_ping = alerta
        elif pronto:
            icone_ping = txt.OK
            linha_ping = f"no ar (servidor nao responde a ping no transporte {transporte})"
        else:
            icone_ping = txt.ERRO
            linha_ping = "sem 'Server started.' no log - servidor nao subiu"

    body = [
        txt.campo("Versao do BDS", version),
        txt.campo("Container", f"{'rodando' if state.running else state.status} ha {_fmt_uptime(state.started_at)}"),
        txt.campo("Reinicios", state.restart_count),
    ]
    linhas_regras = [
        txt.campo("Modo", f"{props.get('gamemode', '?')} / {props.get('difficulty', '?')}"),
        txt.campo("Lugares", props.get("max-players", "?")),
        txt.campo("Visao", props.get("view-distance", "?")),
        txt.campo(
            "Lista de acesso",
            f"{'ligada' if props.get('allow-list') == 'true' else 'desligada'} no arquivo",
        ),
    ]
    # A propriedade so diz o que o proximo boot vai ler. Quem manda AGORA e' o
    # runtime, entao o /status pergunta o console em vez de repetir o arquivo e
    # chamar isso de verdade.
    erro_lista, no_servidor = await asyncio.to_thread(allowlist_no_console, ctx, "list")
    if erro_lista:
        linhas_regras.append(txt.aviso(f"console mudo, nao da para confirmar: {erro_lista}"))
    else:
        linhas_regras.append(txt.campo("No servidor", f"{len(no_servidor)} pessoa(s) na lista carregada"))
    pendentes = [r["name"] for r in ctx.store.liberacoes()]
    if pendentes:
        linhas_regras.append(
            txt.aviso(
                f"{len(pendentes)} liberacao(oes) nao valem ainda: {', '.join(pendentes)} "
                f"(eu reinicio em {ctx.config.allowlist_grace_seconds}s)"
            )
        )
    body.append(txt.secao("⚙️", "regras do jogo") + "\n" + txt.sub(linhas_regras))
    if ping.ok and not ping.motd_ok:
        body.append(
            txt.aviso(
                "Pong sem MCPE;... - bug conhecido do Mojang (itzg#649), que o "
                "cliente entende como timeout. Nao ha contorno no compose."
            )
        )

    installed: dict[str, addons.InstalledPack] = {}
    try:
        world = addons.world_dir(cfg.data_dir, ctx.mundo())
        installed = addons.installed_packs(cfg.data_dir)
        ativos = addons.packs_do_mundo(world)
        seus = [p for p in installed.values() if not p.interno]
        em_uso = sum(1 for p in seus if p.uuid in ativos.get(p.kind, {}))
        body.append(f"{txt.MUNDO} {txt.campo('Mundo', world.name)}")
        body.append(
            f"{txt.PACOTES} {txt.campo('Seus add-ons', f'{len(seus)} (em uso: {em_uso})')}"
        )
    except addons.AddonError as exc:
        body.append(f"{txt.MUNDO} {txt.campo('Mundo', exc)}")

    logs_path = cfg.data_dir / "content_log.txt"
    if logs_path.is_file():
        age = time.time() - logs_path.stat().st_mtime
        if age > 600 and installed:
            body.append(
                txt.aviso(
                    f"Ultimo erro de pack: ver {logs_path.name} "
                    f"(nao muda ha {int(age / 60)}min)"
                )
            )
    return [txt.cabecalho(txt.SERVIDOR, "status do servidor"), f"{icone_ping} {linha_ping}", *body]


async def espera_pronto(ctx: AppContext) -> tuple[bool, str, raknet.PingResult]:
    """Espera o BDS anunciar "Server started." no log do boot atual.

    O criterio e o log, nao o ping: no nethernet (unico transporte do BDS
    1.26.52+) a porta 19132 nem e bindada, entao o unconnected ping nunca
    responde e esperar por ele gastava o BOOT_TIMEOUT inteiro a cada restart.
    O ping continua sendo feito no fim, so para dizer quantos jogadores tem.
    """
    cfg = ctx.config
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.boot_timeout
    pronto = False
    alerta = ""
    while True:
        pronto, alerta = await asyncio.to_thread(ctx.docker.pronto)
        if pronto or alerta or loop.time() >= deadline:
            break
        await asyncio.sleep(2.0)
    ping = await raknet.ping_async(cfg.bds_host, cfg.bds_port)
    return pronto, alerta, ping


def _relatorio(reason: str, cabecalho: str, corpo: list[str], icone: str = "♻️") -> str:
    cabecalhos = [txt.cabecalho(icone, cabecalho)]
    if reason:
        cabecalhos += ["", txt.campo("Motivo", reason)]
    return "\n".join([*cabecalhos, "", *corpo])


def _falhou(reason: str, erro: str, dica: str = "") -> str:
    corpo = [txt.erro(erro), "", txt.erro("O servidor NAO voltou a ficar no ar.")]
    if dica:
        corpo += ["", txt.sub([dica])]
    return _relatorio(reason, "nao deu certo", corpo, icone="❌")


async def restart_server(ctx: AppContext, reason: str) -> str:
    """Reinicia o container, reaplica a versao estavel e confirma se voltou.

    NUNCA levanta excecao: quem chama esta funcao ja mandou "reiniciando..." no
    Telegram, e uma excecao subindo daqui deixaria o admin olhando para o
    silencio, sem nenhuma confirmacao. Qualquer problema vira um relatorio
    com o veredito na propria mensagem.
    """
    async with _restart_lock(ctx):
        try:
            ctx.em_restart = True
            return await _reinicia(ctx, reason)
        except Exception as exc:
            log.exception("restart_server levantou")
            return _falhou(
                reason,
                f"Falha inesperada durante o restart: {exc}",
                "Veja: docker compose logs bds",
            )
        finally:
            ctx.em_restart = False


_locks: dict[int, asyncio.Lock] = {}


def _restart_lock(ctx: AppContext) -> asyncio.Lock:
    """Serializa os motivos de reinicio.

    O agendador das 05:00, o backup semanal e um /reiniciar do admin podem
    cair no mesmo minuto. Sem esta trava, dois reinicios se atropelam: o
    segundo stop chega enquanto o primeiro ainda espera o boot, e o relatorio
    do primeiro volta dizendo que o servidor nao subiu, o que e mentira - ele
    so nao subiu ainda. A chave e o id do ctx, que dura o processo todo.
    """
    trava = _locks.get(id(ctx))
    if trava is None:
        trava = _locks[id(ctx)] = asyncio.Lock()
    return trava


async def _reinicia(ctx: AppContext, reason: str) -> str:
    cfg = ctx.config
    before = await asyncio.to_thread(ctx.docker.server_version)

    if cfg.announce_seconds > 0:
        await asyncio.to_thread(ctx.docker.say, f"[bot] {reason} - reiniciando em {cfg.announce_seconds}s")
        await asyncio.sleep(cfg.announce_seconds)

    since = time.time()
    try:
        await asyncio.to_thread(ctx.docker.restart)
    except Exception as exc:  # docker errors de rede/socket
        log.exception("restart falhou")
        return _falhou(reason, f"Falha ao reiniciar o container: {exc}")

    state = await asyncio.to_thread(ctx.docker.state)
    if not state.running:
        return _falhou(
            reason,
            "O container subiu e caiu de novo.",
            "Veja: docker compose logs bds",
        )

    pronto, alerta, ping = await espera_pronto(ctx)
    after = await asyncio.to_thread(ctx.docker.server_version, since)

    if alerta:
        return _falhou(reason, alerta, "Confira a propriedade 'transport' no server.properties")
    if not pronto:
        return _falhou(
            reason,
            f"o BDS nao imprimiu 'Server started.' em {cfg.boot_timeout}s",
            "Veja: docker compose logs bds",
        )

    reaplicados = await asyncio.to_thread(aplica_overrides, ctx)
    if reaplicados:
        log.warning("override reescrito pelo boot do BDS: %s (so vale no proximo restart)", reaplicados)
    asyncio.create_task(asyncio.to_thread(ctx.docker.pull_image))

    corpo = [
        f"{txt.OK} Deu certo: a mudanca entrou e o servidor voltou.",
        "",
        txt.campo("Versao do BDS", f"{before} -> {after}"),
    ]
    if ping.motd_ok:
        corpo += ["", txt.sub([txt.campo("Jogadores", ping.resumo())])]
    else:
        corpo += [
            "",
            txt.info(
                "Contagem de jogadores indisponivel: no transporte nethernet o BDS "
                "nao responde ao ping na 19132. Isso e normal nesta versao."
            ),
        ]
    if reaplicados:
        corpo += [
            "",
            txt.aviso(
                "O boot do container reescreveu " + ", ".join(reaplicados) + ". "
                "Eu ja voltei a aplicar no arquivo, mas o servidor so usa no proximo restart."
            ),
        ]
    return _relatorio(reason, "reiniciado", corpo)


async def cria_backup(ctx: AppContext, motivo: str) -> tuple[backup.Backup, str]:
    """Faz o backup do estado atual e devolve (backup, relatorio).

    A ordem e parada -> copia -> liga, e nao pode ser outra: o BDS escreve no
    mundo o tempo todo, e um tar tirado com o servidor no ar pega arquivos no
    meio de uma gravacao. Um backup que falha ao ser extraido e pior do que
    nenhum, porque voce so descobre na hora de restaurar.

    O container volta a subir num finally, mesmo se o empacotamento explodir:
    ficar com o servidor fora do ar por causa de um .tar.gz seria o pior
    resultado possivel desta funcao. O em_restart tambem volta no finally, e
    nao so no caminho feliz: se ele ficasse ligado depois de uma falha, a
    vigia silenciava toda queda de verdade dali em diante.
    """
    cfg = ctx.config
    async with _restart_lock(ctx):
        # em_restart fica ligado ate o container voltar: e o que segura a
        # vigia, que veria o container parar sem ser o /reiniciar e mandaria
        # um "o servidor caiu" no meio de um backup que esta indo bem.
        ctx.em_restart = True
        inicio = time.monotonic()
        arquivo = cfg.backups_dir / "atuais" / backup.nome_arquivo()
        try:
            try:
                if cfg.announce_seconds > 0:
                    await asyncio.to_thread(ctx.docker.say, f"[bot] {motivo} - salvando o mundo em {cfg.announce_seconds}s")
                    await asyncio.sleep(cfg.announce_seconds)

                await asyncio.to_thread(ctx.docker.para)
                # o mundo so esta integro depois que o BDS larga o arquivo: o
                # entry script so manda o "stop", e o processo precisa de alguns
                # segundos para fechar o mundo e sair.
                await asyncio.sleep(2.0)

                tamanho, itens = await asyncio.to_thread(
                    backup.empacota, cfg.data_dir, cfg.state_dir, arquivo
                )
            except Exception as exc:
                log.exception("backup falhou antes de terminar o empacotamento")
                # se este religa falhar, sobe o erro dele: servidor parado e o
                # problema maior que o empacotamento, e o log.exception acima
                # ja deixou o original registrado
                await _religa(ctx)
                raise backup.BackupError(f"nao consegui empacotar: {exc}") from exc

            # subir para o Dropbox e demorado (o .tar.gz passa de 100 MB) e nao
            # precisa do servidor parado: o mundo ja esta no disco. Por isso o
            # container volta antes, e o upload roda com o jogo no ar.
            await _religa(ctx)
        finally:
            # so depois de confirmado que o servidor voltou: enquanto ele esta
            # subindo, a queda e esperada e nao e para avisar o usuario
            ctx.em_restart = False

        segundos = time.monotonic() - inicio

        apagados_local = await asyncio.to_thread(
            backup.roda_local, cfg.backups_dir, cfg.backup_local_keep
        )
        registro = backup.Backup(
            caminho=arquivo,
            nome=arquivo.name,
            tamanho=tamanho,
            itens=itens,
            segundos=segundos,
            apagados_local=apagados_local,
        )

        dbx = dropbox.Dropbox(
            token=cfg.dropbox.token,
            refresh_token=cfg.dropbox.refresh_token,
            app_key=cfg.dropbox.app_key,
            app_secret=cfg.dropbox.app_secret,
        )
        if not dbx.configurado():
            registro.erro_dropbox = "sem credencial do Dropbox no .env"
            return registro, _relatorio_backup(registro, motivo)

        link, erro, apagados, sobraram = await asyncio.to_thread(
            backup.sobe, dbx, arquivo, cfg.backup_dropbox_keep
        )
        registro.link = link
        registro.erro_dropbox = erro
        registro.apagados_dropbox = apagados
        registro.dropbox_total = sobraram
        return registro, _relatorio_backup(registro, motivo)


async def _religa(ctx: AppContext) -> None:
    """Sobe o container e espera ficar pronto, sem levantar excecao.

    Chamar duas vezes (uma no except e outra depois do empacotamento) e
    proposital: o except ja devolveu o erro, entao a segunda volta faz o
    container subir do estado parado em que o primeiro deixou.
    """
    if not (await asyncio.to_thread(ctx.docker.state)).running:
        try:
            await asyncio.to_thread(ctx.docker.liga)
        except Exception as exc:
            log.exception("nao consegui religar o container")
            raise backup.BackupError(f"parei o servidor para o backup e nao consegui religar: {exc}") from exc
        pronto, alerta, _ = await espera_pronto(ctx)
        if not pronto:
            log.error("servidor nao voltou depois do backup: %s", alerta or "sem 'Server started.'")
            raise backup.BackupError("o servidor nao voltou depois do backup")


def _relatorio_backup(registro: backup.Backup, motivo: str) -> str:
    mb = registro.tamanho / 1048576
    corpo = [
        f"{txt.OK} Mundo e config salvos no estado de agora.",
        "",
        txt.campo("Arquivo", registro.nome),
        txt.campo("Tamanho", f"{mb:.1f} MB"),
        txt.campo("Servidor fora do ar", f"{registro.segundos:.0f}s"),
    ]
    if registro.link:
        corpo += ["", txt.campo("Link", registro.link)]
    if registro.erro_dropbox:
        corpo += [
            "",
            txt.aviso("Ficou so na VPS, o Dropbox nao aceitou: " + registro.erro_dropbox),
            txt.sub([f"Copie por SSH: {registro.caminho}"]),
        ]
    if registro.apagados_dropbox:
        rotacao = (
            f"No Dropbox apaguei {len(registro.apagados_dropbox)} antigo(s), "
            f"ficaram {registro.dropbox_total}."
        )
        corpo += ["", txt.sub([rotacao])]
    if registro.apagados_local:
        corpo += ["", txt.sub([f"Na VPS apaguei {len(registro.apagados_local)} antigo(s)."])]
    return _relatorio(motivo, "backup pronto", corpo, icone="💾")


def status_backup(ctx: AppContext) -> str:
    """O que existe agora, sem fazer nada."""
    cfg = ctx.config
    pasta = cfg.backups_dir / "atuais"
    meus = sorted((p for p in pasta.iterdir() if p.is_file()), key=lambda p: p.name) if pasta.is_dir() else []
    corpo = [
        txt.campo("Na VPS", f"{len(meus)} de {cfg.backup_local_keep}"),
        txt.campo("Pasta", str(pasta)),
        txt.campo("Dropbox", cfg.dropbox.resumo()),
        txt.campo("Automatico", f"{DIAS_SEMANA[cfg.backup_day % 7]} {cfg.backup_hour:02d}:{cfg.backup_minute:02d} ({cfg.tz})"),
    ]
    if meus:
        ultimo = meus[-1]
        corpo += [
            "",
            txt.campo("Ultimo", f"{ultimo.name} ({ultimo.stat().st_size / 1048576:.1f} MB)"),
        ]
    return _relatorio("", "backup", corpo, icone="💾")


DIAS_SEMANA = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")


def lista_packs(ctx: AppContext) -> str:
    """Lista os add-ons instalados, dizendo quais o mundo realmente ativa.

    Tres grupos porque eles respondem a perguntas diferentes:
      - Em uso: o mundo referencia o pack, entao ele esta valendo no jogo.
      - Add-ons: instalados e nao referenciados. Nao tem efeito ate o bot
        religar no mundo, e vale dizer isso com nome.
      - Internos do BDS: vanilla, chemistry, editor e as libraries que o proprio
        servidor traz e carrega sozinho. Sao dezenas de pastas; entra uma linha
        de resumo para nao enterrar o que o admin instalou.
    """
    cfg = ctx.config
    installed = addons.installed_packs(cfg.data_dir)
    if not installed:
        return "\n".join(
            [
                txt.cabecalho(txt.PACOTES, "add-ons instalados"),
                "",
                txt.info("Nenhum add-on instalado ainda."),
                "",
                txt.DIVISOR,
                txt.info("Mande um .mcaddon ou .mcpack que eu mostro o que tem dentro."),
                txt.aviso("O envio de add-on e so para admin."),
            ]
        )

    try:
        world = addons.world_dir(cfg.data_dir, ctx.mundo())
        ativos = addons.packs_do_mundo(world)
        nome_mundo = world.name
    except addons.AddonError as exc:
        ativos, nome_mundo = {}, "?"
        log.warning("nao consegui ler os packs do mundo: %s", exc)

    do_usuario = [p for p in installed.values() if not p.interno]
    internos = [p for p in installed.values() if p.interno]
    em_uso = [p for p in do_usuario if p.uuid in ativos.get(p.kind, {})]
    soltos = [p for p in do_usuario if p.uuid not in ativos.get(p.kind, {})]

    cabecalho_ = [
        txt.cabecalho(txt.PACOTES, "add-ons instalados"),
        "",
        txt.campo("Mundo", nome_mundo),
        txt.campo("Seus add-ons", f"{len(do_usuario)} ({len(em_uso)} em uso)"),
    ]

    def bloco(icone: str, titulo: str, packs: list[addons.InstalledPack], ativo: bool) -> list[str]:
        if not packs:
            return []
        linhas = [txt.secao(icone, titulo, len(packs))]
        for pack in packs:
            marca = txt.OK if ativo else txt.AVISO
            linhas += [
                "",
                f"{marca} {pack.name}",
                txt.sub([f"v{pack.version_str}  ·  uuid {pack.uuid[:8]}  ·  {pack.folder.name}"]),
            ]
        return linhas

    linhas = cabecalho_
    linhas += bloco("✅", "em uso no mundo", em_uso, True)
    linhas += bloco("⚠️", "instalados, mas sem efeito", soltos, False)
    if not do_usuario:
        linhas += ["", txt.info("Nenhum add-on seu ainda. Os packs abaixo sao do proprio BDS.")]

    notas = []
    if soltos:
        nomes = ", ".join(p.name for p in soltos[:6]) + ("..." if len(soltos) > 6 else "")
        notas.append(txt.aviso(f"{len(soltos)} sem efeito no jogo: {nomes}"))
        notas.append(txt.info("Mande o .mcaddon de novo: eu reinstalo e religo no mundo."))
    elif do_usuario:
        notas.append(txt.ok(f"Todos os {len(do_usuario)} add-ons seus estao em uso."))
    if internos:
        familias = sorted({p.folder.name.split("_")[0] for p in internos})
        notas.append(
            txt.info(
                f"{len(internos)} packs internos do BDS ("
                + ", ".join(familias[:5])
                + ("..." if len(familias) > 5 else "")
                + ") — carregados pelo proprio servidor, fora da conta."
            )
        )
    notas.append(txt.info("Instalar ou atualizar: mande o arquivo .mcaddon aqui no bot."))
    return "\n".join(linhas + ["", txt.DIVISOR, *notas])


async def install_packs(ctx: AppContext, items: list[tuple[addons.Pack, str]]) -> str:
    """Aplica (pack, modo) no mundo e reinicia uma unica vez no fim."""
    cfg = ctx.config
    try:
        world = addons.world_dir(cfg.data_dir, ctx.mundo())
    except addons.AddonError as exc:
        return str(exc)

    linhas: list[str] = []
    for pack, mode in items:
        try:
            result = addons.apply(pack, cfg.data_dir, world, mode, backup_keep=cfg.backup_keep)
        except addons.AddonError as exc:
            linhas.append(txt.erro(f"{pack.name}: {exc}"))
            continue
        except OSError as exc:
            log.exception("falha aplicando %s", pack.name)
            linhas.append(txt.erro(f"{pack.name}: erro de disco ({exc})"))
            continue
        new_version = ".".join(str(n) for n in result.new_version)
        de = f"v{'.'.join(str(n) for n in result.old_version)} -> " if result.old_version else ""
        caminho = f"{addons.PACK_DIRS[result.kind]}/{result.folder}"
        linhas.append(txt.ok(f"{result.name} {de}v{new_version}"))
        linhas.append(txt.sub([caminho, f"mundo: {result.world_entry}"]))

    if any(linha.startswith(txt.ERRO) for linha in linhas):
        return "\n".join([txt.cabecalho(txt.PACOTES, "add-on"), "", *linhas])

    report = await restart_server(ctx, "Add-on aplicado")
    return "\n".join([txt.cabecalho(txt.PACOTES, "add-on"), "", *linhas, "", report])
