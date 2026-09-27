"""Handlers abertos a admin e leitor: /start, /entrar, /status, /packs e add-on.

Quem so pode ler entra aqui sem passar pelo painel do admin (admin.py). O que
mexe no servidor - instalar pack - exige admin e e barrado na propria handler
com uma mensagem clara.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    Document,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import addons, ops, txt
from .auth import AuthError, Session
from .ops import AppContext

log = logging.getLogger("bds.handlers")
router = Router(name="comum")

PENDING_TTL = 3600

HELP = "\n".join(
    [
        txt.cabecalho("📖", "servidor revolucao"),
        "",
        txt.secao("👀", "leitura", "qualquer pessoa com chave"),
        txt.sub(
            [
                "/status  - como esta o servidor",
                "/packs  - add-ons instalados e se valem no jogo",
                "/ajuda  - esta mensagem",
            ]
        ),
        txt.secao("🛡️", "so admin"),
        txt.sub(["/admin  - tudo que da para fazer"]),
        "",
        txt.info("Add-ons: mande o .mcaddon ou .mcpack e eu mostro o que tem dentro,"),
        txt.sub(["pergunto se quer atualizar um pack ou instalar como novo."]),
    ]
)

BOT_COMMANDS = [
    {"command": "status", "description": "como esta o servidor"},
    {"command": "packs", "description": "add-ons instalados"},
    {"command": "entrar", "description": "usar uma chave de leitura"},
    {"command": "ajuda", "description": "como usar"},
    {"command": "admin", "description": "painel do admin"},
]


@dataclass
class Pending:
    user_id: int
    created: float
    workdir: Path
    archive: Path
    label: str
    packs: list[addons.Pack] = field(default_factory=list)


_pending: dict[str, Pending] = {}


def _encerra(pending: Pending | None) -> None:
    """Some com os arquivos do upload, dando certo ou errado."""
    if pending:
        shutil.rmtree(pending.workdir, ignore_errors=True)


def _cleanup(token: str) -> None:
    _encerra(_pending.pop(token, None))


def drop_expired() -> None:
    agora = time.time()
    for token in [t for t, p in _pending.items() if agora - p.created > PENDING_TTL]:
        _cleanup(token)


def _clip(text: str, limit: int = 3800) -> str:
    return text if len(text) <= limit else text[: limit - 40] + "\n... (cortado)"


def _kind_tag(kind: str) -> str:
    """Rotulo curto do tipo do pack, com o mesmo icone da listagem."""
    return "🧠 BP" if kind == addons.BEHAVIOR else "🎨 RP"


# ------------------------------------------------------------------ comandos


@router.message(CommandStart())
@router.message(Command("ajuda", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("entrar"))
async def cmd_entrar(message: Message, command: CommandObject, ctx: AppContext) -> None:
    args = (command.args or "").split()
    if not args:
        await message.answer(
            "\n".join(
                [
                    txt.cabecalho("🔑", "usar chave de leitura"),
                    "",
                    txt.campo("Uso", "/entrar CHAVE"),
                    "",
                    txt.info("A chave e o texto que o dono do servidor te mandou"),
                    txt.sub(["(ex.: A1B2-C3D4-E5F6) - ela so da leitura de status."]),
                ]
            )
        )
        return
    travado = ctx.auth.segundos_bloqueio(message.from_user.id)
    if travado:
        await message.answer(txt.erro(f"Muitas tentativas. Tente de novo em {int(travado)}s."))
        return
    try:
        sessao, key, primeira = ctx.auth.resgata_chave(message.from_user.id, message.from_user.username, args[0])
    except AuthError as exc:
        ctx.auth.registra_tentativa(message.from_user.id, ok=False)
        await message.answer(txt.erro(str(exc)))
        return
    ctx.auth.registra_tentativa(message.from_user.id, ok=True)
    titulo = "chave vinculada" if primeira else "chave reconhecida"
    await message.answer(
        "\n".join(
            [
                txt.cabecalho("🔑", titulo),
                "",
                txt.ok(f"{key.label} — {message.from_user.username or 'sua conta'}"),
                "",
                txt.info("Daqui para frente voce ve o status: /status"),
                txt.aviso("Mudou de conta do Telegram? Peca uma chave nova ao admin."),
            ]
        )
    )
    ctx.store.audita(sessao.user_id, "acesso", f"chave {key.id} ({key.label})")


@router.message(Command("status"))
async def cmd_status(message: Message, ctx: AppContext) -> None:
    texto = _clip("\n".join(await ops.status_lines(ctx)), 1000)
    icone = ctx.config.icon_file
    if icone.is_file():
        await message.answer_photo(photo=FSInputFile(icone), caption=texto)
        return
    await message.answer(texto)


@router.message(Command("packs", "addons"))
async def cmd_packs(message: Message, ctx: AppContext) -> None:
    await message.answer(_clip(ops.lista_packs(ctx)))


# ------------------------------------------------------------------- add-on


@router.message(F.document)
async def on_document(message: Message, ctx: AppContext, sessao: Session) -> None:
    if not sessao.eh_admin:
        await message.answer(txt.erro("Envio de add-on e so para admin. Leitura nao instala nada."))
        return

    drop_expired()
    doc: Document = message.document
    nome = doc.file_name or "upload.zip"
    limite = ctx.config.max_upload_bytes

    if doc.file_size and doc.file_size > limite:
        await message.answer(
            "\n".join(
                [
                    txt.cabecalho("📦", "arquivo grande demais"),
                    "",
                    txt.erro(
                        f"O Telegram so me entrega ate {limite // (1024 * 1024)} MB e esse tem "
                        f"{doc.file_size / (1024 * 1024):.1f} MB."
                    ),
                    "",
                    txt.info("Se for um pack de texturas grande, mande separado o behavior pack"),
                    txt.sub(["e o resource pack, ou suba por SFTP direto em"]),
                    txt.sub([f"{ctx.config.data_dir}/behavior_packs."]),
                ]
            )
        )
        return

    token = secrets.token_hex(5)
    workdir = ctx.staging / token
    archive = workdir / nome
    aviso = await message.answer(txt.info("Baixando e abrindo o arquivo..."))

    try:
        workdir.mkdir(parents=True, exist_ok=True)
        with archive.open("wb") as destino:
            await message.bot.download(doc, destination=destino)
    except Exception as exc:  # rede do Telegram / disco
        log.exception("falha no download")
        shutil.rmtree(workdir, ignore_errors=True)
        await aviso.edit_text(txt.erro(f"Falha ao baixar o arquivo: {exc}"))
        return

    extraido = workdir / "x"
    try:
        addons.extract_archive(archive, extraido)
        packs = addons.detect_packs(extraido)
    except addons.AddonError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        await aviso.edit_text(txt.erro(str(exc)))
        return
    except Exception as exc:
        log.exception("falha ao abrir o arquivo")
        shutil.rmtree(workdir, ignore_errors=True)
        await aviso.edit_text(txt.erro(f"nao consegui abrir o arquivo: {exc}"))
        return

    installed = addons.installed_packs(ctx.config.data_dir)
    _pending[token] = Pending(
        user_id=message.from_user.id,
        created=time.time(),
        workdir=workdir,
        archive=archive,
        label=nome,
        packs=packs,
    )

    server_version = ctx.docker.server_version()
    linhas = [
        txt.cabecalho("📦", "conteudo do arquivo"),
        "",
        txt.campo("Arquivo", f"{nome} — {len(packs)} pack(s) dentro"),
    ]
    for pack in packs:
        tag = _kind_tag(pack.kind)
        if pack.uuid in installed:
            velho = installed[pack.uuid].version_str
            estado = f"v{velho} -> v{pack.version_str}  (atualizar)"
        else:
            estado = f"v{pack.version_str}  (novo)"
        linhas += ["", f"[{tag}] {pack.name}", txt.sub([f"{estado}  ·  uuid {pack.uuid[:8]}"])]
    avisos = [addons.engine_warning(pack.min_engine, server_version) for pack in packs]
    avisos = [a for a in avisos if a]
    if avisos:
        linhas += ["", *[txt.aviso(a) for a in avisos]]
    linhas += ["", txt.aviso("O que eu faco com esse arquivo?")]

    botoes = []
    updates = [p for p in packs if p.uuid in installed]
    for pack in updates:
        velho = installed[pack.uuid].version_str
        botoes.append(
            [
                InlineKeyboardButton(
                    text=f"⬆️ Atualizar {pack.name} (v{velho} -> v{pack.version_str})",
                    callback_data=f"ad:upd:{token}:{pack.uuid[:8]}",
                )
            ]
        )
    if len(updates) > 1:
        botoes.append([InlineKeyboardButton(text="⬆️ Atualizar todos", callback_data=f"ad:updall:{token}")])
    novos = [p for p in packs if p.uuid not in installed]
    if novos:
        rotulo = "Instalar como novo" if len(novos) > 1 else f"Instalar {novos[0].name} como novo"
        botoes.append([InlineKeyboardButton(text=f"➕ {rotulo}", callback_data=f"ad:new:{token}")])
    botoes.append([InlineKeyboardButton(text="❌ Cancelar", callback_data=f"ad:cancel:{token}")])

    await aviso.edit_text(
        _clip("\n".join(linhas)),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=botoes),
    )


@router.callback_query(F.data.startswith("ad:"))
async def on_addon_action(callback: CallbackQuery, ctx: AppContext, sessao: Session) -> None:
    if not sessao.eh_admin:
        await callback.answer(txt.erro("Isso e so para admin."), show_alert=True)
        return

    partes = callback.data.split(":")
    if len(partes) < 3:
        await callback.answer()
        return
    acao, token = partes[1], partes[2]
    pending = _pending.get(token)

    if pending is None or pending.user_id != callback.from_user.id:
        await callback.answer(txt.erro("Esse upload expirou, mande o arquivo de novo."), show_alert=True)
        return

    if acao == "cancel":
        _cleanup(token)
        await callback.answer("Cancelado")
        await callback.message.edit_text(txt.aviso("Cancelado, nada foi instalado."))
        return

    if acao == "new":
        alvos = list(pending.packs)
    elif acao == "updall":
        installed = addons.installed_packs(ctx.config.data_dir)
        alvos = [p for p in pending.packs if p.uuid in installed]
    elif acao == "upd":
        if len(partes) < 4:
            await callback.answer()
            return
        achado = next((p for p in pending.packs if p.uuid.startswith(partes[3])), None)
        alvos = [achado] if achado else []
    else:
        alvos = []

    if not alvos:
        await callback.answer(txt.erro("Nenhum pack valido nessa opcao."), show_alert=True)
        return

    # O token morre aqui (o clique seguinte cai no "upload expirou"), mas os
    # arquivos so podem sumir DEPOIS da instalacao: addons.apply copia de
    # pack.root, que mora dentro dessa mesma pasta. Apagar antes quebrava
    # todo add-on com "erro de disco".
    _pending.pop(token, None)
    await callback.answer("Aplicando...")
    await callback.message.edit_text(txt.info("Aplicando e reiniciando o servidor..."))
    modo = "new" if acao == "new" else "update"
    asyncio.create_task(_rodar(callback.message, ctx, [(pack, modo) for pack in alvos], pending))


async def _rodar(
    message: Message,
    ctx: AppContext,
    itens: list[tuple[addons.Pack, str]],
    pending: Pending | None = None,
) -> None:
    try:
        relatorio = await ops.install_packs(ctx, itens)
    except Exception as exc:  # nunca deixar a task morrer em silencio
        log.exception("falha na instalacao")
        relatorio = txt.erro(f"erro inesperado: {exc}")
    try:
        await message.edit_text(_clip(relatorio))
    except Exception:
        log.exception("nao consegui editar a mensagem")
    finally:
        _encerra(pending)
