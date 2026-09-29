"""Entrada do bot: dispatcher, portao de acesso, agendador das 05:00 e limpeza.

Seguranca do container: quem tem o docker.sock na mao comanda o servidor. Por
isso o acesso passa por duas camadas, sempre nesta ordem:

1. AuthMiddleware (update-outer) - resolve o papel e cria o acesso na hora:
   admin pelo codigo de resgate, leitor pela chave. Quem nao tem papel nenhum so
   recebe a dica de como entrar.
2. AdminOnly (router admin.py) - barra tudo que nao for admin.

O resgate e pelo codigo do .env, nao pelo primeiro usuario a falar com o bot.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.enums import UpdateType
from aiogram.types import BotCommandScopeDefault

from . import admin, handlers, ops, txt
from .auth import AuthMiddleware
from .config import Config, ConfigError
from .ops import AppContext

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("bds.main")


async def notificar(bot: Bot, ctx: AppContext, texto: str) -> None:
    """Manda para os admins registrados."""
    alvos = [u.user_id for u in ctx.store.usuarios() if u.eh_admin]
    for user_id in alvos:
        try:
            await bot.send_message(user_id, texto)
            log.info("avisei %s (%d chars)", user_id, len(texto))
        except Exception as exc:
            log.warning("nao consegui avisar %s: %s", user_id, exc)


async def reconciliador(ctx: AppContext, bot: Bot) -> None:
    """Reaplica as props que o boot sobrescreveu. Corrige drift e atualizacoes."""
    while True:
        await asyncio.sleep(60)
        try:
            if ctx.docker.state().running:
                mudancas = await asyncio.to_thread(ops.aplica_overrides, ctx)
                if mudancas:
                    await asyncio.to_thread(ctx.docker.exec, "send-command", "allowlist", "reload")
                    log.info("reconciliado: %s", ", ".join(mudancas))
                    await notificar(
                        bot, ctx,
                        txt.cabecalho("⚙️", "ajustei a config")
                        + "\n\n"
                        + txt.info("O boot do container sobrescreveu: " + ", ".join(mudancas)),
                    )
        except Exception:
            log.exception("falha ao reconciliar config")


async def agendador(ctx: AppContext, bot: Bot) -> None:
    cfg = ctx.config
    while True:
        agora = datetime.now(cfg.tz)
        alvo = agora.replace(hour=cfg.update_hour, minute=cfg.update_minute, second=0, microsecond=0)
        if alvo <= agora:
            alvo += timedelta(days=1)
        horas = (alvo - agora).total_seconds() / 3600
        log.info("auto-update: proximo restart em %s (%.1fh)", alvo.isoformat(timespec="minutes"), horas)
        await asyncio.sleep((alvo - agora).total_seconds())

        log.info("auto-update: reiniciando para pegar a versao estavel mais nova")
        try:
            relatorio = await ops.restart_server(
                ctx, f"Atualizacao automatica das {cfg.update_hour:02d}:{cfg.update_minute:02d}"
            )
            await notificar(
                bot, ctx, txt.cabecalho("🔄", "atualizacao automatica") + "\n\n" + relatorio
            )
        except Exception as exc:
            log.exception("auto-update falhou")
            await notificar(bot, ctx, txt.erro(f"Auto-update falhou: {exc}"))


async def agendador_backup(ctx: AppContext, bot: Bot) -> None:
    """Backup semanal, uma vez por semana no dia e hora configurados.

    Mesmo desenho do agendador das 05:00: calcula o proximo dia da semana,
    dorme e repete. Repete em vez de contar 7 dias porque assim um boot do
    bot nao empurra o backup, e o horario continua sendo o que o admin pediu.

    O erro nao derruba a task: um Dropbox fora do ar as 04:00 nao pode
    deixar o servidor sem backup nas semanas seguintes.
    """
    cfg = ctx.config
    while True:
        agora = datetime.now(cfg.tz)
        # timedelta(days=...) resolve a virada de semana e de ano sozinha.
        dias = (cfg.backup_day - agora.weekday()) % 7
        alvo = (agora + timedelta(days=dias)).replace(
            hour=cfg.backup_hour, minute=cfg.backup_minute, second=0, microsecond=0
        )
        if alvo <= agora:
            alvo += timedelta(days=7)
        horas = (alvo - agora).total_seconds() / 3600
        log.info("backup: proximo em %s (%.1fh)", alvo.isoformat(timespec="minutes"), horas)
        await asyncio.sleep((alvo - agora).total_seconds())

        if not (await asyncio.to_thread(ctx.docker.state)).running:
            log.warning("backup pulado: o servidor esta fora do ar")
            await notificar(
                bot, ctx, txt.aviso("Backup semanal pulado: o servidor ja estava parado.")
            )
            continue
        try:
            _, relatorio = await ops.cria_backup(ctx, "backup semanal automatico")
            await notificar(bot, ctx, relatorio)
        except Exception as exc:
            log.exception("backup semanal falhou")
            await notificar(bot, ctx, txt.erro(f"Backup semanal falhou: {exc}"))


async def vigia(ctx: AppContext, bot: Bot) -> None:
    """Avisa quando o BDS muda de estado sem ser o bot a mandar.

    Cobre a mudanca aplicada por fora do Telegram - editando o
    server.properties na mao, um docker exec, um deploy na VPS. O bot nao
    sabe que voce mexeu, mas ve o container mudar de StartedAt, entao avisa
    do mesmo jeito que avisa do restart que ele mesmo mandou: o que era, se
    deu certo e se o servidor voltou.
    """
    try:
        estado = await asyncio.to_thread(ctx.docker.state)
    except Exception as exc:
        log.warning("vigia nao leu o estado inicial: %s", exc)
        return

    visto = estado.started_at
    caido = not estado.running
    if caido:
        await notificar(
            bot, ctx,
            txt.cabecalho("🛑", "o servidor esta parado")
            + "\n\n"
            + txt.erro("O container do BDS nao esta rodando, e eu acabei de subir.")
            + "\n\n"
            + txt.info("Veja: docker compose logs bds"),
        )

    while True:
        await asyncio.sleep(30)
        try:
            estado = await asyncio.to_thread(ctx.docker.state)
        except Exception as exc:
            log.warning("vigia nao leu o estado: %s", exc)
            continue

        if not estado.running:
            if not caido:
                caido = True
                log.warning("vigia: container do BDS parou")
                await notificar(
                    bot, ctx,
                    txt.cabecalho("🛑", "o servidor caiu")
                    + "\n\n"
                    + txt.erro("O container do BDS parou de rodar e eu nao mandei parar.")
                    + "\n\n"
                    + txt.info("Veja: docker compose logs bds"),
                )
            continue

        if caido:
            caido = False
            motivo = "O servidor voltou a subir"
        elif estado.started_at and estado.started_at != visto:
            motivo = "O servidor reiniciou"
        else:
            continue
        visto = estado.started_at

        # O restart que o proprio bot mandou ja tem relatorio no chat dele.
        if ctx.em_restart:
            log.info("vigia: reinicio detectado durante restart do proprio bot")
            continue

        log.info("vigia: %s, verificando se ficou no ar", motivo)
        pronto, alerta, _ping = await ops.espera_pronto(ctx)
        if alerta:
            corpo = [txt.erro(alerta)]
        elif pronto:
            corpo = [
                f"{txt.OK} Deu certo: o servidor voltou e esta no ar.",
                "",
                txt.campo("Versao do BDS", await asyncio.to_thread(ctx.docker.server_version)),
            ]
        else:
            corpo = [
                txt.erro(
                    f"Subiu, mas o BDS nao imprimiu 'Server started.' em "
                    f"{ctx.config.boot_timeout}s. O jogo provavelmente nao abre."
                )
            ]
        await notificar(bot, ctx, "\n".join([txt.cabecalho("👀", motivo), "", *corpo]))


async def faxina(ctx: AppContext) -> None:
    while True:
        await asyncio.sleep(900)
        handlers.drop_expired()


async def main() -> None:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("configuracao invalida: %s", exc)
        raise SystemExit(1) from None

    ctx = AppContext.build(cfg)
    bot = Bot(cfg.token)
    dp = Dispatcher()
    dp["ctx"] = ctx
    # update-outer: precisa rodar antes de qualquer handler para injetar `sessao`.
    dp.update.outer_middleware(AuthMiddleware(ctx.auth, bot))
    # admin antes do comum: /packs, /config e /removerpack existem nos dois.
    dp.include_router(admin.router)
    dp.include_router(handlers.router)

    me = await bot.get_me()
    admins = [u.user_id for u in ctx.store.usuarios() if u.eh_admin]
    log.info(
        "bot @%s (id %s) | alvo: %s | auto-update %02d:%02d %s | admins: %s",
        me.username, me.id, cfg.bds_container, cfg.update_hour, cfg.update_minute, cfg.tz,
        admins or "ninguem, esperando /start <codigo>",
    )

    try:
        await bot.set_my_commands(handlers.BOT_COMMANDS, scope=BotCommandScopeDefault())
    except Exception as exc:
        log.warning("nao consegui registrar os comandos: %s", exc)

    tasks = [
        asyncio.create_task(agendador(ctx, bot)),
        asyncio.create_task(agendador_backup(ctx, bot)),
        asyncio.create_task(reconciliador(ctx, bot)),
        asyncio.create_task(vigia(ctx, bot)),
        asyncio.create_task(faxina(ctx)),
    ]
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        if admins:
            await notificar(
                bot, ctx,
                "\n".join(
                    [
                        txt.cabecalho("🛡️", "bot no ar"),
                        "",
                        txt.campo("Container", cfg.bds_container),
                        txt.campo("Auto-update", f"{cfg.update_hour:02d}:{cfg.update_minute:02d} ({cfg.tz})"),
                        txt.campo(
                            "Backup semanal",
                            f"{ops.DIAS_SEMANA[cfg.backup_day % 7]} "
                            f"{cfg.backup_hour:02d}:{cfg.backup_minute:02d} ({cfg.tz})",
                        ),
                        txt.campo("Dropbox", cfg.dropbox.resumo()),
                        "",
                        txt.info("Use /admin para os comandos."),
                    ]
                ),
            )
        # UpdateType e um str Enum sem nenhum metodo: a lista completa de tipos
        # de update e list(UpdateType). (UpdateType.all() nao existe no
        # aiogram 3 e derrubava o bot no start_polling.)
        await dp.start_polling(bot, allowed_updates=list(UpdateType))
    finally:
        for task in tasks:
            task.cancel()
        await bot.session.close()
        ctx.store.fecha()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
