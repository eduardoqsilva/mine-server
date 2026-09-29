"""Comandos do admin: configs, chaves de acesso, jogadores, reinicio, auditoria.

Tudo neste arquivo e bloqueado para quem nao e admin: o filtro RoleAdmin no
router, e o middleware que injeta `sessao` so para quem tem papel.

Nao ha lista de permissao aqui: o servidor e' aberto (allow-list=false) e quem
entra e' qualquer um. As duas coisas de jogador que sobraram sao o /ops
(permissions.json, por XUID) e o /chutar.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from urllib.parse import quote, unquote

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import backup, ops, serverctl, txt
from .auth import AdminOnly
from .ops import AppContext

log = logging.getLogger("bds.admin")
router = Router(name="admin")
router.message.middleware(AdminOnly())
router.callback_query.middleware(AdminOnly())

AJUDA_ADMIN = "\n".join(
    [
        txt.cabecalho("🛡️", "painel do admin"),
        "",
        txt.secao("⚙️", "configuracao"),
        txt.sub(
            [
                "/config  - menu de propriedades do servidor",
                "/config <chave> <valor>  - muda na mao (ex: /config difficulty hard)",
                "/config <chave> <valor> sim  - confirma propriedade perigosa",
                "/config aplicar  - reescreve o que o bot guardou",
            ]
        ),
        txt.secao("👥", "jogadores"),
        txt.sub(
            [
                "O servidor e' aberto: qualquer um entra, sem lista.",
                "novos jogadores entram como visitor e o bot pergunta se quero dar member.",
                "/ops <gamertag> <operator|member|visitor> [xuid]  - permissoes do jogador",
                "/member <gamertag> [xuid]  - atalho para dar member ao jogador",
                "  so funciona com online-mode ligado: o permissions.json casa por xuid",
                "/chutar <gamertag> [motivo]  - expulsa agora",
                "  quem esta online: /console list",
            ]
        ),
        txt.secao(txt.CHAVES, "acesso ao bot"),
        txt.sub(
            [
                "/chave <nome>  - cria uma chave de leitura",
                "/chaves  - lista as chaves",
                "/revogar <n>  - revoga a chave n (n e o numero)",
                "/pessoas  - quem tem acesso",
            ]
        ),
        txt.secao("🔧", "operacao"),
        txt.sub(
            [
                "/reiniciar [motivo]  - reinicia o servidor",
                "/anunciar <texto>  - fala no chat do jogo",
                "/console <comando>  - comando no console do BDS, com a resposta",
                "  qualquer texto que o BDS aceite, sem restricao: gamerule, tps,",
                "  time set day, allowlist off, list, help...",
                "  eu mostro na mesma mensagem o que o servidor respondeu",
                "/backup  - salva o mundo de agora e manda o link",
                "/backups  - o que existe de backup, sem copiar nada",
                "/log [n]  - ultimas linhas do log",
                "/auditoria  - ultimas acoes registradas",
            ]
        ),
        txt.secao(txt.PACOTES, "add-ons"),
        txt.sub(
            [
                "/packs  - add-ons instalados e se valem no jogo",
                "/removerpack <nome>  - desinstalar um pack",
                "",
                "Mande o .mcaddon ou .mcpack aqui que eu mostro o conteudo",
                "e pergunto se atualiza um pack ou instala como novo.",
            ]
        ),
    ]
)

MENU_CONFIG: list[tuple[str, str, str]] = [
    ("gamemode", "Modo de jogo", "survival/creative/adventure"),
    ("difficulty", "Dificuldade", "peaceful/easy/normal/hard"),
    ("max-players", "Maximo de jogadores", "numero"),
    ("view-distance", "Distancia de visao", "numero"),
    ("server-name", "Nome do servidor", "texto (sem ';')"),
    ("allow-cheats", "Permite cheats", "true/false"),
    ("hide-online-players", "Esconde quem esta online", "true/false"),
    ("default-player-permission-level", "Permissao padrao", "visitor/member/operator"),
    ("player-idle-timeout", "Desloga apos X min", "numero"),
]


def _clip(texto: str, limite: int = 3800) -> str:
    return texto if len(texto) <= limite else texto[: limite - 40] + "\n... (cortado)"


def _tokens(bruto: str) -> list[str]:
    """Quebra o que veio depois do comando, com aspas agrupando.

    Gamertag com espaco precisa vir entre aspas, entao '/ops "Ze Do Zero"
    operator' e' o jeito de escrever o nome inteiro. Aspas desbalanceadas nao
    podem derrubar o bot no meio de um comando, entao nesse caso cai para um
    split simples.
    """
    try:
        return shlex.split(bruto)
    except ValueError:
        # aspas desbalanceadas. Tirar o caractere e melhor do que devolver
        # '"Ze' como se fosse o nome: o admin recebe 'faltou aspa' em vez de
        # uma entrada que nunca vai casar com o jogador.
        return bruto.replace('"', " ").split()


def _menu_config(props: dict[str, str]) -> InlineKeyboardMarkup:
    linhas = []
    for chave, rotulo, dica in MENU_CONFIG:
        atual = props.get(chave, "?")
        linhas.append(
            [InlineKeyboardButton(text=f"{rotulo}: {atual}", callback_data=f"cfg:{chave}")]
        )
    linhas.append(
        [
            InlineKeyboardButton(text="📋 Mais propriedades (texto)", callback_data="cfgbruta"),
            InlineKeyboardButton(text="✖️ Fechar", callback_data="cfg:none"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=linhas)


# --------------------------------------------------------------------- config


@router.message(Command("admin", "ajudaadmin"))
async def cmd_admin_help(message: Message) -> None:
    await message.answer(_clip(AJUDA_ADMIN))


@router.message(Command("config", "cfg"))
async def cmd_config(message: Message, command: CommandObject, ctx: AppContext) -> None:
    props = await asyncio.to_thread(ctx.server.le_props)
    args = (command.args or "").split()

    if len(args) >= 2:
        partes = serverctl.interpreta_config(args)
        chave, bruto, confirmado = partes
        prop = serverctl.POR_CHAVE.get(chave)
        if prop is None:
            linhas = "\n".join(txt.sub([f"{p.key} — {p.label} ({p.tipo_texto})"]) for p in serverctl.CATALOGO)
            await message.answer(
                "\n".join(
                    [
                        txt.cabecalho("⚙️", "propriedade desconhecida"),
                        "",
                        txt.erro(f"'{chave}' nao e uma propriedade conhecida."),
                        "",
                        txt.info("Estas sao as que eu conheco:"),
                        linhas,
                    ]
                )
            )
            return
        try:
            valor = serverctl.valida(prop, bruto)
        except serverctl.PropertyError as exc:
            await message.answer(txt.erro(str(exc)))
            return
        if serverctl.precisa_confirmacao(prop, confirmado):
            await message.answer(
                "\n".join(
                    [
                        txt.cabecalho("⚙️", "cuidado"),
                        "",
                        txt.aviso(prop.cuidado),
                        "",
                        txt.info("Confirme com:"),
                        txt.sub([f"/config {chave} {valor} sim"]),
                    ]
                )
            )
            return
        await asyncio.to_thread(ctx.server.set_prop, chave, valor)
        do_compose = serverctl.do_compose(chave)
        if not do_compose:
            ctx.store.set_override(chave, valor, message.from_user.id)
        ctx.store.audita(message.from_user.id, "config", f"{chave}={valor}")

        aviso = prop.reinicia
        espera = ctx.config.announce_seconds + 25
        if aviso:
            rodape = (
                "\n"
                + txt.info(
                    f"Vou reiniciar agora para o servidor pegar o valor. "
                    f"Em uns {espera}s eu confirmo se deu certo e se ele voltou."
                )
            )
        else:
            rodape = "\n" + txt.info("Foi aplicado, sem derrubar o servidor.")
        if do_compose:
            rodape += (
                "\n"
                + txt.aviso(
                    "O valor que vale no boot e o do .env: a imagem reescreve esta "
                    "propriedade em todo boot."
                )
            )
        await message.answer(txt.ok(f"{chave} = {valor} gravado.") + rodape)
        if aviso:
            relatorio = await ops.restart_server(ctx, f"config {chave}")
            await message.answer(_clip(relatorio))
        return

    if len(args) == 1 and args[0].lower() == "aplicar":
        reaplicados = await asyncio.to_thread(ops.aplica_overrides, ctx)
        if reaplicados:
            await message.answer(txt.ok("Reescrito: " + ", ".join(reaplicados)))
            await message.answer(_clip(await ops.restart_server(ctx, "aplicando config")))
            return
        await message.answer(txt.info("server.properties ja esta igual ao que eu guardei."))

    linhas = [txt.cabecalho("⚙️", "configuracao do servidor")]
    linhas.append("")
    linhas += [_linha_prop(chave, props) for chave, _rotulo, _dica in MENU_CONFIG]
    await message.answer(_clip("\n".join(linhas)), reply_markup=_menu_config(props))


def _linha_prop(chave: str, props: dict[str, str]) -> str:
    rotulo = next((r for c, r, _d in MENU_CONFIG if c == chave), chave)
    return txt.campo(rotulo, props.get(chave, "(nao definido)"))


@router.callback_query(F.data.startswith("cfg:"))
async def on_config_button(callback: CallbackQuery, ctx: AppContext) -> None:
    chave = callback.data.split(":", 1)[1]
    await callback.answer()
    if chave == "none":
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    if chave == "cfgbruta":
        linhas = "\n".join(
            txt.sub([f"{p.key} = {p.tipo_texto}"] + ([f"⚠️ {p.cuidado}"] if p.cuidado else []))
            for p in serverctl.CATALOGO
        )
        await callback.message.edit_text(
            "\n".join(
                [
                    txt.cabecalho("⚙️", "todas as propriedades"),
                    "",
                    linhas,
                    "",
                    txt.info("Use: /config chave valor"),
                ]
            )
        )
        return

    prop = serverctl.POR_CHAVE[chave]
    dica = {
        "bool": "manda sim ou nao",
        "int": "manda o numero",
        "choice": "opcoes: " + ", ".join(prop.opcoes),
        "str": "manda o texto (sem aspas)",
    }[prop.tipo]
    atual = (await asyncio.to_thread(ctx.server.le_props)).get(chave, "(nao definido)")
    linhas = [
        txt.cabecalho("⚙️", prop.label),
        "",
        txt.campo("Chave", prop.key),
        txt.campo("Atual", atual),
        "",
        txt.info(dica),
    ]
    if prop.cuidado:
        linhas += ["", txt.aviso(prop.cuidado), txt.sub(["Repita com um 'sim' no fim para confirmar."])]
    linhas += ["", txt.info(f"Mande: /config {chave} <valor>")]
    await callback.message.edit_text("\n".join(linhas))


async def _resolva_xuid(ctx: AppContext, nome: str, xuid: str | None) -> str | None:
    if xuid is not None:
        return xuid
    log_txt = ctx.docker.logs(lines=1500)
    return ctx.server.xuid_no_log(nome, log_txt)


async def _promover_jogador(ctx: AppContext, nome: str, xuid: str | None, nivel: str, *, quem: int, origem: str) -> str:
    if not ctx.server.online_mode():
        return (
            "O online-mode esta desligado, entao o permissions.json nao consegue casar o jogador "
            f"por XUID. Ative ONLINE_MODE=true no .env e reinicie o servidor para aceitar {nome} como {nivel}."
        )
    xuid_real = await _resolva_xuid(ctx, nome, xuid)
    if xuid_real is None:
        return (
            f"Nao achei o XUID de {nome} no log do servidor. "
            f"Espere o player entrar e tente de novo, ou mande {origem} com o XUID manualmente."
        )
    try:
        mudou = await asyncio.to_thread(ctx.server.set_permissao, nome, xuid_real, nivel)
    except serverctl.PropertyError as exc:
        return str(exc)
    ctx.store.audita(quem, "ops", f"{nome}={nivel} xuid={xuid_real}")
    await asyncio.to_thread(ctx.docker.exec, "send-command", "permission", "reload")
    return f"{nome} = {nivel} (xuid {xuid_real})\n" + ("Recarregado sem reiniciar." if mudou else "Ja estava assim.")


@router.callback_query(F.data.startswith("join_yes:"))
async def on_join_yes(callback: CallbackQuery, ctx: AppContext) -> None:
    _, xuid, nome_q = callback.data.split(":", 2)
    nome = unquote(nome_q)
    await callback.answer("Promovendo para member...")
    resposta = await _promover_jogador(ctx, nome, xuid, "member", quem=callback.from_user.id, origem="/member")
    await callback.message.edit_text(
        txt.ok(f"Aprovacao de {nome}.") + "\n" + txt.sub([resposta])
    )


@router.callback_query(F.data.startswith("join_no:"))
async def on_join_no(callback: CallbackQuery) -> None:
    _, _xuid, nome_q = callback.data.split(":", 2)
    nome = unquote(nome_q)
    await callback.answer("Sem promocao.")
    await callback.message.edit_text(txt.info(f"Mantive {nome} como visitor. Pode mudar depois com /member {nome}."))


# ------------------------------------------------------------------ jogadores


@router.message(Command("ops"))
async def cmd_ops(message: Message, command: CommandObject, ctx: AppContext) -> None:
    args = _tokens(command.args or "")
    if len(args) < 2:
        await message.answer(txt.info('Uso: /ops <gamertag> <operator|member|visitor> [xuid]  ("aspas" se o nome tiver espaco)'))
        return
    nome, nivel = args[0], args[1].lower()
    xuid = args[2] if len(args) > 2 else None
    resposta = await _promover_jogador(ctx, nome, xuid, nivel, quem=message.from_user.id, origem="/ops")
    if "O online-mode esta desligado" in resposta or "Nao achei o XUID" in resposta:
        await message.answer(
            "\n".join(
                [
                    txt.cabecalho("👤", "preciso do xuid"),
                    "",
                    txt.erro(resposta),
                    "",
                    txt.info("Se quiser usar permissao por XUID, ative ONLINE_MODE=true no .env."),
                    txt.sub(["docker compose up -d bds"]),
                ]
            )
        )
        return
    await message.answer(txt.ok(resposta.splitlines()[0]) + "\n" + txt.info("\n".join(resposta.splitlines()[1:])))


@router.message(Command("member"))
async def cmd_member(message: Message, command: CommandObject, ctx: AppContext) -> None:
    args = _tokens(command.args or "")
    if not args:
        await message.answer(txt.info('Uso: /member <gamertag> [xuid]  ("aspas" se o nome tiver espaco)'))
        return
    nome = args[0]
    xuid = args[1] if len(args) > 1 else None
    resposta = await _promover_jogador(ctx, nome, xuid, "member", quem=message.from_user.id, origem="/member")
    if "Nao achei o XUID" in resposta or "online-mode" in resposta.lower():
        await message.answer(
            "\n".join(
                [
                    txt.cabecalho("👤", "member"),
                    "",
                    txt.erro(resposta),
                    "",
                    txt.info("Se o jogador ja entrou, o XUID aparecera no log do servidor."),
                    txt.sub([f"/member {nome} 2535453759792258"]),
                ]
            )
        )
        return
    await message.answer(txt.ok(resposta.splitlines()[0]) + "\n" + txt.info("\n".join(resposta.splitlines()[1:])))


@router.message(Command("chutar"))
async def cmd_chutar(message: Message, command: CommandObject, ctx: AppContext) -> None:
    args = _tokens(command.args or "")
    if not args:
        await message.answer(txt.info('Uso: /chutar <gamertag> [motivo]  ("aspas" se o nome tiver espaco)'))
        return
    nome = args[0]
    motivo = " ".join(args[1:]) or "expulso pelo admin"
    saida = await asyncio.to_thread(ctx.docker.exec, "send-command", "kick", nome, motivo)
    ctx.store.audita(message.from_user.id, "kick", f"{nome} ({motivo})")
    detalhe = saida.strip()[:300] or "sem resposta (ele ja tinha saido)"
    await message.answer("\n".join([txt.ok(f"Kick de {nome}."), txt.sub([motivo, detalhe])]))


# --------------------------------------------------------------------- acesso


@router.message(Command("chave"))
async def cmd_chave(message: Message, command: CommandObject, ctx: AppContext) -> None:
    label = (command.args or "").strip() or "leitura"
    key, token = ctx.store.cria_chave("viewer", label[:40], message.from_user.id)
    ctx.store.audita(message.from_user.id, "chave.criada", f"{key.id} {label}")
    await message.answer(
        "\n".join(
            [
                txt.cabecalho(txt.CHAVES, f"chave #{key.id} criada"),
                "",
                txt.campo("Nome", f"{label} (so leitura)"),
                "",
                txt.OK + " " + token,
                "",
                txt.info(f"A pessoa usa: /entrar {token}"),
                "",
                txt.aviso("Mande essa chave pelo privado."),
                txt.sub(
                    [
                        "Ela fica gravada no bot so como hash:",
                        "esta mensagem e a unica vez que o texto aparece.",
                    ]
                ),
            ]
        )
    )


@router.message(Command("chaves"))
async def cmd_chaves(message: Message, ctx: AppContext) -> None:
    chaves = ctx.store.chaves(incluir_revogadas=True)
    if not chaves:
        await message.answer(
            "\n".join(
                [
                    txt.cabecalho(txt.CHAVES, "chaves de leitura"),
                    "",
                    txt.info("Nenhuma chave ainda. Crie com /chave <nome>"),
                ]
            )
        )
        return
    linhas = [txt.cabecalho(txt.CHAVES, "chaves de leitura"), ""]
    for k in chaves:
        if not k.ativa:
            marca = txt.ERRO
            estado = "revogada"
        elif k.bound_to:
            marca = txt.OK
            estado = f"ativa — {k.bound_name or k.bound_to}"
        else:
            marca = txt.AVISO
            estado = "ativa, ainda nao usada"
        linhas.append(f"{marca} #{k.id} {k.label}")
        linhas.append(txt.sub([f"{estado}  ·  usos: {k.used_count}"]))
    await message.answer(_clip("\n".join(linhas)))


@router.message(Command("revogar"))
async def cmd_revogar(message: Message, command: CommandObject, ctx: AppContext) -> None:
    bruto = (command.args or "").strip()
    if not bruto.isdigit():
        await message.answer(txt.info("Uso: /revogar <numero>"))
        return
    key = await asyncio.to_thread(ctx.store.revoga_chave, int(bruto))
    if key is None:
        await message.answer(txt.erro(f"Nao existe chave #{bruto}."))
        return
    ctx.store.audita(message.from_user.id, "chave.revogada", f"#{key.id} {key.label}")
    linhas = [txt.ok(f"Chave #{key.id} ({key.label}) revogada.")]
    if key.bound_to:
        try:
            removido = await asyncio.to_thread(ctx.store.remove_usuario, key.bound_to)
        except ValueError:
            removido = False
            linhas.append(txt.info("Essa chave era do admin, entao o acesso dele continua."))
        if removido:
            linhas.append(txt.info("O usuario da chave tambem perdeu o acesso."))
    await message.answer("\n".join(linhas))


@router.message(Command("pessoas"))
async def cmd_pessoas(message: Message, ctx: AppContext) -> None:
    usuarios = ctx.store.usuarios()
    linhas = [txt.cabecalho("👥", "quem tem acesso"), ""]
    if not usuarios:
        linhas.append(txt.info("Ninguem ainda. Resgate com /start <codigo de resgate>."))
    for u in usuarios:
        marca = txt.OK + " admin" if u.eh_admin else txt.INFO + " leitura"
        linhas.append(f"{marca} — {u.username or '?'} (id {u.user_id})")
        linhas.append(txt.sub([u.note or "sem nota"]))
    await message.answer(_clip("\n".join(linhas)))


@router.message(Command("auditoria"))
async def cmd_auditoria(message: Message, ctx: AppContext) -> None:
    registros = ctx.store.auditoria(25)
    linhas = [txt.cabecalho("📋", "ultimas acoes"), ""]
    if not registros:
        linhas.append(txt.info("Nada registrado ainda."))
    for registro in registros:
        linhas.append(f"{txt.INFO} {registro['action']} — id {registro['user_id']}")
        linhas.append(txt.sub([registro["detail"] or ""]))
    await message.answer(_clip("\n".join(linhas)))


# ------------------------------------------------------------------ operacao


@router.message(Command("reiniciar"))
async def cmd_reiniciar(message: Message, command: CommandObject, ctx: AppContext) -> None:
    motivo = (command.args or "").strip() or "Reinicio pedido pelo admin"
    ctx.store.audita(message.from_user.id, "reiniciar", motivo)
    aviso = await message.answer(txt.info("Reiniciando..."))
    relatorio = await ops.restart_server(ctx, motivo)
    try:
        await aviso.edit_text(_clip(relatorio))
    except Exception:
        log.warning("nao consegui editar o aviso de restart", exc_info=True)
        await message.answer(_clip(relatorio))


@router.message(Command("anunciar"))
async def cmd_anunciar(message: Message, command: CommandObject, ctx: AppContext) -> None:
    texto = (command.args or "").strip()
    if not texto:
        await message.answer(txt.info("Uso: /anunciar <texto>"))
        return
    await asyncio.to_thread(ctx.docker.say, texto)
    ctx.store.audita(message.from_user.id, "anunciar", texto[:120])
    await message.answer(txt.ok(f"Falei no jogo: {texto}"))


@router.message(Command("console", "cmd", "comando"))
async def cmd_console(message: Message, command: CommandObject, ctx: AppContext) -> None:
    """Manda um comando no console do BDS e mostra a resposta na mesma mensagem.

    Sem filtro: o texto vai inteiro para o console, do jeito que o admin
    escreveu. Serve tanto para o que o catalogo do /config nao cobre ('gamerule',
    'tps', 'time set day') quanto para o que ele cobre, porque o comando do
    console e' o caminho sem reinicio.

    A resposta vem do log do container (o send-command da imagem escreve no
    stdin do BDS e sai; o que o servidor fala sai no stdout), e volta na mesma
    mensagem - com o carimbo e o eco do proprio comando removidos.
    """
    texto = (command.args or "").strip()
    if not texto:
        await message.answer(
            "\n".join(
                [
                    txt.info("Uso: /console <comando>"),
                    "",
                    txt.info("Voce escreve o que quiser; eu mando tudo e mostro a resposta."),
                    txt.sub(
                        [
                            "/console list",
                            "/console help",
                            "/console tps",
                            "/console gamerule doDaylightCycle false",
                            "/console time set day",
                            "/console allowlist off",
                        ]
                    ),
                ]
            )
        )
        return
    estado = await asyncio.to_thread(ctx.docker.state)
    if not estado.running:
        await message.answer(
            txt.erro("O container do BDS nao esta rodando, entao nao ha console.")
        )
        return
    erro, linhas = await asyncio.to_thread(ctx.docker.console, texto)
    ctx.store.audita(message.from_user.id, "console", texto[:120])
    await message.answer(_clip(ops.resposta_console(texto, linhas, erro)))


LOG_LIMITE = 3700


@router.message(Command("backups"))
async def cmd_backups(message: Message, ctx: AppContext) -> None:
    """So informa o que existe. Nao para o servidor, nao copia nada."""
    await message.answer(_clip(ops.status_backup(ctx)))


@router.message(Command("backup"))
async def cmd_backup(message: Message, ctx: AppContext) -> None:
    """Salva o mundo e a config do estado de agora e manda o link.

    Derruba o servidor por ~1min, avisa o jogo antes e volta sozinho. A
    espera e o usuario, nao o bot: o /backup manual e o backup semanal
    chamam a MESMA funcao, entao o que voce ve aqui e o que roda sozinho
    toda semana.
    """
    estado = await asyncio.to_thread(ctx.docker.state)
    if not estado.running:
        await message.answer(
            "\n".join(
                [
                    txt.erro("O servidor ja esta fora do ar."),
                    "",
                    txt.sub(["Nada a salvar: backup e do estado de agora."]),
                ]
            )
        )
        return
    await message.answer(
        "\n".join(
            [
                txt.info("Salvando o mundo. O jogo sai do ar por instantes."),
                "",
                # sem numero: o aviso vai uns segundos, mas o container leva
                # dois a tres minutos pra subir de novo, e um "~80s" que vira
                # 200s e pior do que nao prometer nada
                txt.sub(["Aviso no jogo, copia, e o servidor volta. Depois mando o link."]),
            ]
        )
    )
    try:
        registro, relatorio = await ops.cria_backup(ctx, "backup pedido no Telegram")
    except backup.BackupError as exc:
        ctx.store.audita(message.from_user.id, "backup", f"falhou: {exc}"[:200])
        await message.answer(
            "\n".join(
                [
                    txt.erro("O backup falhou."),
                    "",
                    str(exc),
                    "",
                    txt.sub(["Se o servidor nao voltou, veja: docker compose ps"]),
                ]
            )
        )
        return
    ctx.store.audita(message.from_user.id, "backup", f"{registro.nome} ({registro.tamanho} bytes)")
    await message.answer(_clip(relatorio))


@router.message(Command("log"))
async def cmd_log(message: Message, command: CommandObject, ctx: AppContext) -> None:
    linhas = 30
    if command.args and command.args.strip().isdigit():
        linhas = max(1, min(int(command.args.strip()), 300))
    texto = await asyncio.to_thread(ctx.docker.logs, linhas)
    cabecalho = txt.cabecalho(txt.LOG, f"ultimas {linhas} linhas")
    if not texto.strip():
        await message.answer(cabecalho + "\n\n" + txt.info("Sem log disponivel."))
        return
    # Corta pelo inicio, nunca pelo fim: o _clip geral preserva o comeco, e num
    # log o que o admin quer sao as linhas mais novas. Com 30 linhas do BDS
    # passando de 3700 chars, o corte pelo fim escondia justamente o estado
    # atual do servidor.
    if len(texto) > LOG_LIMITE:
        texto = "... (inicio cortado)\n" + texto[-(LOG_LIMITE - 24) :]
    await message.answer(cabecalho + "\n\n" + texto)


@router.message(Command("removerpack"))
async def cmd_remover_pack(message: Message, command: CommandObject, ctx: AppContext) -> None:
    from . import addons

    installed = addons.installed_packs(ctx.config.data_dir)
    if not installed:
        await message.answer(txt.info("Nenhum pack instalado."))
        return

    alvo = None
    if command.args:
        termo = command.args.strip().lower()
        for pack in installed.values():
            if termo == pack.uuid or pack.uuid.startswith(termo) or termo in pack.name.lower():
                alvo = pack
                break

    if alvo is None:
        botoes = [
            [
                InlineKeyboardButton(
                    text=f"{'🧠 BP' if p.kind == addons.BEHAVIOR else '🎨 RP'} {p.name} v{p.version_str}",
                    callback_data=f"rm:{p.uuid}",
                )
            ]
            for p in sorted(installed.values(), key=lambda p: (p.kind, p.name.lower()))
        ]
        botoes.append([InlineKeyboardButton(text="❌ Cancelar", callback_data="rm:cancel")])
        await message.answer(
            "\n".join(
                [
                    txt.cabecalho(txt.PACOTES, "remover pack"),
                    "",
                    txt.aviso("A remocao reinicia o servidor."),
                    txt.info("Qual pack remover?"),
                ]
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=botoes),
        )
        return

    teclado = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"🗑️ Sim, remover {alvo.name}", callback_data=f"rm:go:{alvo.uuid}"),
                InlineKeyboardButton(text="Cancelar", callback_data="rm:cancel"),
            ]
        ]
    )
    await message.answer(
        f"{txt.aviso('Remover ' + alvo.name + ' v' + alvo.version_str + '?')}",
        reply_markup=teclado,
    )


@router.callback_query(F.data.startswith("rm:"))
async def on_remove_pack(callback: CallbackQuery, ctx: AppContext) -> None:
    from . import addons

    partes = callback.data.split(":")
    acao = partes[1] if len(partes) > 1 else ""

    if acao == "cancel":
        await callback.answer("Ok")
        await callback.message.edit_text(txt.info("Cancelado, nada foi removido."))
        return

    installed = addons.installed_packs(ctx.config.data_dir)
    pack = installed.get(partes[-1])
    if pack is None:
        await callback.answer(txt.erro("Esse pack nao esta mais instalado."), show_alert=True)
        return

    if acao != "go":
        # primeiro passo: o botao da lista mostra a confirmacao
        teclado = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"🗑️ Sim, remover {pack.name}", callback_data=f"rm:go:{pack.uuid}"
                    ),
                    InlineKeyboardButton(text="Cancelar", callback_data="rm:cancel"),
                ]
            ]
        )
        await callback.answer()
        await callback.message.edit_text(
            "\n".join(
                [
                    txt.cabecalho(txt.PACOTES, "remover pack"),
                    "",
                    txt.aviso(f"Remover {pack.name} v{pack.version_str}?"),
                    txt.sub(["Isso reinicia o servidor."]),
                ]
            ),
            reply_markup=teclado,
        )
        return

    try:
        removido = addons.remove(pack.uuid, ctx.config.data_dir, addons.world_dir(ctx.config.data_dir, ctx.mundo()))
    except addons.AddonError as exc:
        await callback.answer(txt.erro(f"Falha: {exc}"), show_alert=True)
        return

    await callback.answer("Removido")
    ctx.store.audita(callback.from_user.id, "pack.removido", f"{removido.name} v{removido.version_str}")
    await callback.message.edit_text(txt.info(f"{removido.name} removido. Reiniciando..."))
    relatorio = await ops.restart_server(ctx, "Pack removido")
    await callback.message.edit_text(_clip(f"{txt.ok(removido.name + ' removido.')}\n\n{relatorio}"))
