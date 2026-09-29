"""Operacoes de alto nivel: reinicio com verificacao, status e o job das 05:00."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import addons, backup, dropbox, raknet, serverctl, txt
from .auth import Auth
from .config import Config
from .docker_ctl import DockerController
from .store import Store

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

# O console e a fonte da verdade da allow-list, e o arquivo e o reserva. Isso
# nao e preciosismo: o BDS reescreve allowlist.json quando desliga, a partir da
# lista que tem em memoria. Quem escreve no arquivo sem o servidor saber
# (allowlist reload recusado, comando nunca entregue) tem a entrada apagada no
# proximo stop - que e o "adicionei a pessoa e ela sumiu da lista depois de um
# restart".
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


def tem_nome(linhas: list[str], nome: str) -> bool:
    """O nome aparece na resposta do console?"""
    alvo = nome.lower()
    return any(alvo in linha.lower() for linha in linhas)


def _aviso_arquivo(ctx: AppContext, erro: str, gravou: bool, o_que: str) -> str:
    """O que dizer quando so deu para mexer no arquivo, e nao no console.

    O detalhe que muda tudo: se o BDS esta PARADO, o proximo boot le o arquivo
    e a entrada entra na memoria dele - ai funciona, e o /reiniciar resolve. Se
    o BDS esta RODANDO e o console nao responde, o proximo stop faz o servidor
    reescrever o arquivo a partir da lista velha e a edicao se perde; mandar
    reiniciar seria exatamente o jeito de perder a entrada. Nesse caso o
    caminho e' o console.
    """
    if not gravou:
        return f"Nao consegui falar com o console do BDS: {erro}. Ele nem estava no arquivo."
    try:
        rodando = ctx.docker.state().running
    except Exception:  # noqa: BLE001 - o aviso nao pode falhar por causa do docker
        rodando = True
    if not rodando:
        return (
            f"Nao consegui falar com o console do BDS: {erro}. Como o servidor esta parado, "
            f"{o_que} no allowlist.json e o proximo boot ja le isso."
        )
    return (
        f"Nao consegui falar com o console do BDS: {erro}. {o_que.capitalize()} no allowlist.json, "
        "mas o servidor esta rodando e ele reescreve esse arquivo quando desliga: um restart agora "
        "apagaria a entrada. O jeito que segura e pelo console - o /permitir de novo, quando ele "
        "responder."
    )


async def poe_na_lista(ctx: AppContext, nome: str, xuid: str | None = None) -> tuple[bool, str]:
    """Coloca o jogador na allow-list. Devolve (entrou, o que o admin precisa saber).

    O console vem primeiro porque o 'allowlist add' e' o caminho que sobrevive:
    o BDS resolve o XUID, grava o arquivo no formato dele e ja passa a valer
    para o servidor que esta rodando. O arquivo so e' tocado quando o console
    nao responde, e nesse caso a entrada so vale no proximo boot.
    """
    erro, _linhas = await asyncio.to_thread(allowlist_no_console, ctx, "add", serverctl.cita(nome))
    if not erro:
        _erro, listadas = await asyncio.to_thread(allowlist_no_console, ctx, "list")
        if tem_nome(listadas, nome):
            return True, ""
        # o comando foi entregue e o servidor nao devolveu o nome: nao da para
        # dizer que liberou. Fala isso em vez de mentir com um "pronto".
        return True, (
            "Mandei 'allowlist add' mas o console nao devolveu o nome em "
            "'allowlist list'. Se ela nao entrar, mande /lista: ele mostra o "
            "que o servidor tem carregado."
        )

    novo = await asyncio.to_thread(ctx.server.add_allowlist, nome, xuid)
    await asyncio.to_thread(allowlist_no_console, ctx, "reload")
    return novo, _aviso_arquivo(ctx, erro, novo, "gravei")


async def tira_da_lista(ctx: AppContext, nome: str) -> tuple[bool, str]:
    """Tira o jogador da allow-list. Devolve (saiu, o que o admin precisa saber)."""
    erro, _linhas = await asyncio.to_thread(allowlist_no_console, ctx, "remove", serverctl.cita(nome))
    if not erro:
        return True, ""
    removido = await asyncio.to_thread(ctx.server.remove_allowlist, nome)
    return removido, _aviso_arquivo(ctx, erro, removido, "tirei")


async def liga_a_lista(ctx: AppContext, by: int) -> str:
    """Liga a allow-list no arquivo e no servidor. Devolve um aviso, se houver.

    Sem isso o /permitir nao libera ninguem: a propriedade e lida so no boot, e
    enquanto ela estiver desligada a lista nao bloqueia nem libera ninguem.
    """
    props = await asyncio.to_thread(ctx.server.le_props)
    if props.get("allow-list") == "true":
        return ""
    await asyncio.to_thread(ctx.server.set_prop, "allow-list", "true")
    ctx.store.set_override("allow-list", "true", by)
    erro, _linhas = await asyncio.to_thread(allowlist_no_console, ctx, "on")
    if erro:
        return f"Gravei allow-list=true, mas o console nao aceitou o 'allowlist on' ({erro})."
    return "A lista estava desligada; liguei agora e tambem gravei allow-list=true, entao o proximo boot ja vem ligada."


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
            "ligada" if props.get("allow-list") == "true" else "desligada",
        ),
    ]
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
