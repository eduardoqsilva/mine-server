"""Comandos do admin: configs, chaves de acesso, lista, negados, reinicio, auditoria.

Tudo neste arquivo e bloqueado para quem nao e admin: o filtro RoleAdmin no
router, e o middleware que injeta `sessao` so para quem tem papel.
"""

from __future__ import annotations

import asyncio
import logging
import shlex

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
                "/lista  - quem pode entrar (o que o servidor tem, o que so esta no arquivo)",
                "/permitir <gamertag>  - adiciona na lista, sem precisar de xuid",
                "  se o console nao responder, eu mando o reload; se nem isso pegar, marco como",
                "  pendente e reinicio sozinho",
                "/removerjogador <gamertag>  - tira da lista",
                "/negar <gamertag> [motivo]  - tira da lista e expulsa se estiver online",
                "/permitido <gamertag>  - volta a liberar",
                "/ops <gamertag> <operator|member|visitor> [xuid]  - permissoes do jogador",
                "  so funciona com online-mode ligado: o permissions.json casa por xuid",
                "/chutar <gamertag> [motivo]  - expulsa agora",
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
    ("allow-list", "Lista ligada", "true/false"),
    ("allow-cheats", "Permite cheats", "true/false"),
    ("hide-online-players", "Esconde quem esta online", "true/false"),
    ("default-player-permission-level", "Permissao padrao", "visitor/member/operator"),
    ("player-idle-timeout", "Desloga apos X min", "numero"),
]


def _clip(texto: str, limite: int = 3800) -> str:
    return texto if len(texto) <= limite else texto[: limite - 40] + "\n... (cortado)"


def _tokens(bruto: str) -> list[str]:
    """Quebra o que veio depois do comando, com aspas agrupando.

    A doc do BDS exige aspas em gamertag com espaco, entao o mesmo vale aqui: o
    nome e o primeiro token, e '/permitir "Ze Do Zero"' e' o jeito de escrever
    o nome inteiro. Aspas desbalanceadas nao podem derrubar o bot no meio de um
    comando, entao nesse caso cai para um split simples.
    """
    try:
        return shlex.split(bruto)
    except ValueError:
        # aspas desbalanceadas. Tirar o caractere e melhor do que devolver
        # '"Ze' como se fosse o nome: o admin recebe 'faltou aspa' em vez de
        # uma entrada que nunca vai casar com o jogador.
        return bruto.replace('"', " ").split()


def _nome_e_xuid(bruto: str) -> tuple[str, str | None, str]:
    """(gamertag, xuid, sobra) do que veio depois de um /permitir.

    O xuid e opcional, entao sobra so sobra quando sobrou alguma coisa que nao
    e numero: e o sinal de que o nome veio sem aspas ('/permitir Ze Do Zero'
    seria lido como 'Ze' com sobra 'Do Zero'). O chamador avisa em vez de
    liberar o jogador errado em silencio.
    """
    partes = _tokens(bruto)
    if not partes:
        return "", None, ""
    nome = partes[0]
    resto = partes[1:]
    if resto and resto[-1].isdigit():
        return nome, resto[-1], " ".join(resto[:-1])
    return nome, None, " ".join(resto)


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

        # allow-list tem comando de console, entao nao precisa derrubar o
        # servidor. So que o comando nao mexe no server.properties (a doc do BDS
        # e explicita), e por isso o set_prop acima continua de pe.
        ao_vivo = serverctl.comando_ao_vivo(chave, valor)
        aviso = prop.reinicia
        fora_do_console = ""
        if ao_vivo:
            erro, _linhas = await asyncio.to_thread(ctx.docker.console, ao_vivo)
            if not erro:
                aviso = False
            else:
                fora_do_console = (
                    "\n"
                    + txt.aviso("O console nao aceitou o comando, entao vou reiniciar:")
                    + "\n"
                    + txt.sub([erro])
                )

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
        await message.answer(txt.ok(f"{chave} = {valor} gravado.") + fora_do_console + rodape)
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


# ------------------------------------------------------------------ jogadores


def _teclado_liberacao() -> InlineKeyboardMarkup:
    """O botao que antecipa o reinicio. O prazo roda sozinho de qualquer jeito."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏩ Reiniciar agora e liberar", callback_data="lib:agora"),
                InlineKeyboardButton(text="Cancelar", callback_data="lib:cancelar"),
            ]
        ]
    )


@router.message(Command("lista"))
async def cmd_lista(message: Message, ctx: AppContext) -> None:
    # As tres fontes (console, arquivo e propriedade) sao lidas de uma vez e
    # mostradas lado a lado. Enquanto cada tela montava a sua versao, elas
    # discordavam - e o bot acabava descrevendo o arquivo como se fosse a lista.
    estado = await asyncio.to_thread(ops.estado_real, ctx)
    negados = ctx.store.negados()
    pendentes = [r["name"] for r in estado.pendentes]

    linhas = [txt.cabecalho(txt.JOGADORES, "lista de acesso"), ""]
    # O console e' o que vale AGORA. O arquivo conta outra historia: o BDS le
    # ele no boot e o reescreve no stop, entao ele descreve o passado.
    linhas.append(txt.secao(txt.CONSOLE, "o que o servidor tem agora"))
    if estado.erro_console:
        linhas += [
            "",
            txt.aviso("nao consegui ler do console: " + estado.erro_console),
            txt.sub(["o resto desta tela e' so o arquivo; sem o console eu nao sei o que vale agora"]),
        ]
    elif not estado.linhas_servidor:
        linhas.append(txt.sub(["(o console nao devolveu nada)"]))
    else:
        linhas += [txt.sub([l]) for l in estado.linhas_servidor]

    # A divergencia e' o bug antigo, escrito: gente no arquivo que o servidor

    # nao tem. Ela nao entra no jogo, e um "restart" resolveria apagando a prova.
    so_arquivo = estado.so_no_arquivo()
    if so_arquivo:
        linhas += [
            "",
            txt.secao(txt.AVISO, "no arquivo, mas NAO no servidor", len(so_arquivo)),
            txt.sub(["esta gente NAO entra: o BDS consulta a lista que tem em memoria"]),
        ]
        linhas += [txt.sub([f"• {nome}"]) for nome in so_arquivo]
        linhas.append(
            txt.sub(["um 'allowlist reload' resolve sem derrubar o servidor: /permitir de novo faz isso"])
        )
    if estado.whitelist_antigo:
        linhas += [
            "",
            txt.aviso(
                "existe um /data/whitelist.json: ele tem preferencia sobre o allowlist.json "
                "enquanto existir. Apague-o, senao ele continua mandando na lista."
            ),
        ]

    # A propriedade decide o proximo boot, e e a unica coisa que o boot le.
    ligado = estado.allow_list_arquivo == "true"
    linhas += [
        "",
        txt.secao("⚙️", "no arquivo (vale no proximo boot)"),
        txt.sub(
            [
                f"allow-list = {estado.allow_list_arquivo or '(nao definido)'}",
                "lista LIGADA" if ligado else "lista DESLIGADA: qualquer um pode entrar",
            ]
        ),
    ]

    linhas.append(txt.secao("📄", "no allowlist.json", len(estado.entradas_arquivo)))
    if estado.erro_arquivo:
        linhas.append(txt.erro(estado.erro_arquivo))
    elif estado.entradas_arquivo:
        for item in estado.entradas_arquivo:
            xuid = item.get("xuid")
            preso = xuid and not estado.online_mode
            if preso:
                rotulo = "  (PRESO: xuid com online-mode desligado - nunca vai casar)"
            elif xuid:
                rotulo = f"  (xuid {xuid})"
            else:
                rotulo = "  (so o nome)"
            linhas.append(txt.sub([f"{item.get('name', '?')}{rotulo}"]))
    else:
        linhas.append(txt.sub(["(ninguem ainda)"]))

    presas = estado.entradas_presas()
    if presas:
        linhas += [
            "",
            txt.aviso(
                "online-mode esta DESLIGADO, e estas entradas ainda tem xuid: "
                + ", ".join(presas)
                + ". Sem autenticacao o cliente nao tem XUID, entao o servidor compara um numero que "
                "nunca vai bater e o jogador fica de fora mesmo estando na lista. O bot tira o campo no "
                "boot; para mandar agora, /permitir de novo."
            ),
        ]

    if pendentes:
        linhas.append(txt.secao("⏳", "liberacoes pendentes", len(pendentes)))
        linhas.append(txt.sub(["ainda nao valem; eu reinicio sozinho para fazer valer"]))
        for nome in pendentes:
            linhas.append(txt.sub([f"• {nome}"]))
    if negados:
        linhas.append(txt.secao("🚫", "negados", len(negados)))
        linhas.append(txt.sub(["Nao voltam a lista sem /permitido <gamertag>"]))
        for d in negados:
            motivo = f" — {d['reason']}" if d.get("reason") else ""
            linhas.append(txt.sub([f"{d['name']}{motivo}"]))
    await message.answer(
        _clip("\n".join(linhas)),
        reply_markup=_teclado_liberacao() if pendentes else None,
    )


@router.message(Command("permitir"))
async def cmd_permitir(message: Message, command: CommandObject, ctx: AppContext) -> None:
    nome, xuid, sobra = _nome_e_xuid(command.args or "")
    if not nome:
        await message.answer(txt.info('Uso: /permitir <gamertag>  ("aspas" se o nome tiver espaco)'))
        return
    if sobra:
        # o nome quase certeza veio sem aspas; liberar 'Ze' quando o
        # jogador se chama 'Ze Do Zero' nao da erro nenhum, so nao entra
        await message.answer(
            "\n".join(
                [
                    txt.erro(f"'{sobra}' nao faz parte do gamertag."),
                    txt.info("Se o nome tem espaco, escreva entre aspas:"),
                    txt.sub([f'/permitir "{nome} {sobra}"']),
                ]
            )
        )
        return
    if ctx.store.esta_negado(nome):
        await message.answer(
            txt.erro(f"{nome} esta na lista de negados. Use /permitido {nome} primeiro.")
        )
        return
    # O xuid e opcional e so um atalho. A doc do BDS: "you don't need to
    # specify a XUID here, it will be resolved the first time the player
    # connects". Exigir o numero barraria justamente quem nunca conseguiu
    # entrar, e o numero adivinhado no log, quando errado, trava o jogador em
    # vez de liberar.
    ctx.store.audita(message.from_user.id, "permitir", nome)

    sem_xbl = not ctx.server.online_mode()
    liberacao = await ops.poe_na_lista(ctx, nome, None if sem_xbl else xuid)
    lista = await ops.liga_a_lista(ctx, message.from_user.id)

    corpo = [txt.cabecalho("👥", "liberar jogador"), "", txt.campo("Nome", nome), ""]
    if sem_xbl:
        aviso_offline = (
            "online-mode desligado: o jogador entra sem conta Microsoft e a lista guarda "
            "so o gamertag, que e' o que o servidor consegue casar."
        )
        corpo.append(txt.sub([aviso_offline]))
        corpo.append("")
    teclado = None
    if liberacao.estado == ops.LIBERADO:
        corpo.append(txt.ok(f"{nome} esta na lista que o servidor tem carregada. Pode entrar."))
        if liberacao.aviso:
            corpo += ["", txt.info(liberacao.aviso)]
        corpo += ["", txt.info("Foi sem reiniciar: vale para o servidor que esta rodando.")]
    elif liberacao.estado == ops.LIBERADO_BOOT:
        corpo.append(
            txt.ok(f"{nome} entrou no allowlist.json e o servidor esta parado, entao o proximo boot le isso.")
        )
        if liberacao.aviso:
            corpo += ["", txt.info(liberacao.aviso)]
    elif liberacao.estado == ops.LIBERADO_FALHOU:
        corpo += [
            txt.erro(liberacao.aviso),
            "",
            txt.sub(["veja o arquivo: /data/allowlist.json, e /lista para o diagnostico"]),
        ]
    else:
        # So no arquivo com o BDS rodando: ainda nao vale. Aqui o bot assume o
        # reinicio - o botao e' o atalho, nao a permissao que falta.
        corpo.append(txt.aviso(liberacao.aviso))
        corpo += ["", await ops.agenda_liberacao(ctx, [nome], message.from_user.id, message.bot)]
        teclado = _teclado_liberacao()
    if lista:
        corpo += ["", txt.info(lista)]
    await message.answer(_clip("\n".join(corpo)), reply_markup=teclado)


@router.callback_query(F.data.startswith("lib:"))
async def on_liberacao(callback: CallbackQuery, ctx: AppContext) -> None:
    """Botao do /permitir: antecipa o reinicio, ou cancela o prazo.

    O prazo nao e' uma permissao que o admin precise dar - ela ja foi dada no
    /permitir. O botao existe porque 5 minutos de porta fechada sao 5 minutos de
    reclamacao, e porque 'eu espero o timer' e' melhor do que o admin nao saber
    se o bot vai cumprir o que prometeu.
    """
    pendentes = [r["name"] for r in ctx.store.liberacoes()]
    if not pendentes:
        await callback.answer("Nada pendente: essa liberacao ja foi concluida.", show_alert=True)
        return

    if callback.data == "lib:cancelar":
        if ctx.liberacao_em_curso:
            # O timer ja disparou e o servidor ja esta no chao. Dizer "cancelado"
            # aqui seria o oposto de honesto: o mundo esta parado, o release vai
            # terminar sozinho.
            await callback.answer("O reinicio ja comecou, nao da para cancelar.", show_alert=True)
            return
        ctx.store.limpa_liberacoes()
        ops.cancela_agenda(ctx)
        await callback.answer("Cancelei.")
        await callback.message.edit_text(
            txt.aviso("Liberacao cancelada. As entradas ficaram no allowlist.json, sem efeito.")
        )
        return

    ops.cancela_agenda(ctx)
    await callback.answer("Reiniciando...")
    quem = ", ".join(pendentes)
    await callback.message.edit_text(txt.info(f"Reiniciando para liberar {quem}..."))
    relatorio = await ops.dispara_pendente(ctx)
    try:
        await callback.message.edit_text(_clip(relatorio))
    except Exception:  # noqa: BLE001 - a mensagem pode ser velha demais para editar
        await callback.message.answer(_clip(relatorio))


@router.message(Command("removerjogador"))
async def cmd_remover_jogador(message: Message, command: CommandObject, ctx: AppContext) -> None:
    nome = " ".join(_tokens(command.args or ""))
    if not nome:
        await message.answer(txt.info('Uso: /removerjogador <gamertag>  ("aspas" se o nome tiver espaco)'))
        return
    saiu, aviso = await ops.tira_da_lista(ctx, nome)
    ctx.store.audita(message.from_user.id, "removerjogador", nome)
    if aviso:
        cabecalho = txt.ok(f"{nome} saiu da lista.") if saiu else txt.ok(f"{nome} nao estava na lista.")
        await message.answer("\n".join([cabecalho, txt.aviso(aviso)]))
        return
    await message.answer(
        txt.ok(f"{nome} saiu da lista.")
        if saiu
        else txt.info(f"{nome} nao estava na lista.")
    )


@router.message(Command("negar"))
async def cmd_negar(message: Message, command: CommandObject, ctx: AppContext) -> None:
    args = _tokens(command.args or "")
    if not args:
        await message.answer(txt.info('Uso: /negar <gamertag> [motivo]  ("aspas" se o nome tiver espaco)'))
        return
    nome = args[0]
    motivo = " ".join(args[1:]) or None
    _saiu, aviso = await ops.tira_da_lista(ctx, nome)
    ctx.store.nega(nome, motivo, message.from_user.id)
    ctx.store.audita(message.from_user.id, "negar", f"{nome} ({motivo or 'sem motivo'})")
    kicked = await asyncio.to_thread(ctx.docker.exec, "send-command", "kick", nome, motivo or "fora do servidor")
    # o Bedrock nao tem denylist: quem nao esta na allowlist nao entra.
    props = await asyncio.to_thread(ctx.server.le_props)
    linhas = [
        txt.cabecalho("🚫", "jogador negado"),
        "",
        txt.ok(f"{nome}: saiu da lista, kick enviado."),
    ]
    linhas.append(
        txt.sub([f"Resposta do servidor: {kicked.strip()[:200]}"])
        if kicked.strip()
        else txt.sub(["Ele nao estava online."])
    )
    if aviso:
        linhas += ["", txt.aviso(aviso)]
    if props.get("allow-list") != "true":
        linhas += [
            "",
            txt.aviso(
                "A allow-list esta DESLIGADA, entao ele ainda consegue entrar. "
                "Ligue com: /config allow-list true"
            ),
        ]
    await message.answer("\n".join(linhas))


@router.message(Command("permitido"))
async def cmd_permitido(message: Message, command: CommandObject, ctx: AppContext) -> None:
    nome = " ".join(_tokens(command.args or ""))
    if not nome:
        await message.answer(txt.info('Uso: /permitido <gamertag>  ("aspas" se o nome tiver espaco)'))
        return
    ctx.store.permite(nome)
    ctx.store.audita(message.from_user.id, "permitido", nome)
    await message.answer(
        txt.ok(f"{nome} nao esta mais Negado.")
        + "\n"
        + txt.info(f"Use /permitir {nome} para ele voltar a entrar na lista.")
    )


@router.message(Command("ops"))
async def cmd_ops(message: Message, command: CommandObject, ctx: AppContext) -> None:
    args = _tokens(command.args or "")
    if len(args) < 2:
        await message.answer(txt.info('Uso: /ops <gamertag> <operator|member|visitor> [xuid]  ("aspas" se o nome tiver espaco)'))
        return
    nome, nivel = args[0], args[1].lower()
    xuid = args[2] if len(args) > 2 else None
    # Sem autenticacao nao existe xuid, e o permissions.json e' uma lista de
    # xuid. Melhor recusar dizendo por que do que aceitar e nao surtir efeito.
    if not ctx.server.online_mode():
        motivo_ops = (
            'A doc do BDS e' " explicita: online-mode precisa estar ligado para o "
            "permissions.json funcionar, porque o xuid exige verificacao online da conta."
        )
        await message.answer(
            "\n".join(
                [
                    txt.cabecalho("👤", "/ops precisa de autenticacao"),
                    "",
                    txt.erro("O online-mode esta desligado, entao nao existe xuid para casar."),
                    "",
                    txt.sub([motivo_ops]),
                    "",
                    txt.info("Se quiser op, ponha no .env:"),
                    txt.sub(["ONLINE_MODE=true"]),
                    txt.info("e recrie o container, porque a propriedade vive no bds:"),
                    txt.sub(["docker compose up -d bds"]),
                    "",
                    txt.info("A allow-list funciona offline. So o /ops que nao."),
                ]
            )
        )
        return
    if xuid is None:
        log_txt = ctx.docker.logs(lines=1500)
        xuid = ctx.server.xuid_no_log(nome, log_txt)
        if xuid is None:
            await message.answer(
                "\n".join(
                    [
                        txt.cabecalho("👤", "preciso do xuid"),
                        "",
                        txt.erro(f"O permissions.json funciona por XUID, e nao achei o de {nome}."),
                        "",
                        txt.info("Pegue no log do servidor:"),
                        txt.sub([f"docker compose logs bds | grep {nome}"]),
                        txt.info("Ou em mcprofile.io. Depois mande:"),
                        txt.sub([f"/ops {nome} {nivel} 2535453759792258"]),
                    ]
                )
            )
            return
    try:
        mudou = await asyncio.to_thread(ctx.server.set_permissao, nome, xuid, nivel)
    except serverctl.PropertyError as exc:
        await message.answer(txt.erro(str(exc)))
        return
    ctx.store.audita(message.from_user.id, "ops", f"{nome}={nivel} xuid={xuid}")
    await asyncio.to_thread(ctx.docker.exec, "send-command", "permission", "reload")
    await message.answer(
        txt.ok(f"{nome} = {nivel} (xuid {xuid})")
        + "\n"
        + txt.info("Recarregado sem reiniciar." if mudou else "Ja estava assim.")
    )


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

    Escapa de rotas que ainda nao existem no bot: qualquer palavra que o BDS
    aceite ('gamerule', 'tps', 'time set day', 'whitelist on') vai direto. E a
    unica forma de mexer em algo que o catalogo do /config nao cobre.
    """
    texto = (command.args or "").strip()
    if not texto:
        await message.answer(
            "\n".join(
                [
                    txt.info("Uso: /console <comando>"),
                    "",
                    txt.sub(
                        [
                            "/console list",
                            "/console help",
                            "/console tps",
                            "/console gamerule doDaylightCycle false",
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
