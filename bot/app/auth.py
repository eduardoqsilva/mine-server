"""Quem pode falar com o bot: resgate de admin, chaves de leitura e papel por usuario.

Regra do acesso:
  - admin: um unico user_id, assumido com TELEGRAM_ADMIN_CLAIM_CODE
  - viewer: qualquer pessoa com uma chave emitida pelo admin (so leitura)
  - sem papel: o bot so responde a dica de como entrar

Nao existe "primeiro a mandar vence": sem o codigo certo ninguem entra. O codigo
pode ser trocado no .env a qualquer momento para expulsar quem roubar a conta,
e quem mandar o codigo novo assume o admin e rebaixa o anterior.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message

from . import txt
from .store import ROLE_ADMIN, ROLE_VIEWER, Key, Store, hash_token, normaliza

log = logging.getLogger("bds.auth")

DICA_COOLDOWN = 30.0
TENTATIVAS_MAX = 5
BLOQUEIO_SEGUNDOS = 600


class AuthError(RuntimeError):
    pass


@dataclass
class Session:
    user_id: int
    role: str
    username: str | None
    eh_admin: bool


class Auth:
    def __init__(self, store: Store, claim_code: str) -> None:
        if not normaliza(claim_code):
            raise AuthError(
                "TELEGRAM_ADMIN_CLAIM_CODE vazio: sem ele ninguem consegue assumir "
                "o admin. Gere um (openssl rand -hex 6) e ponha no .env"
            )
        self.store = store
        self.codigo = normaliza(claim_code)
        self._tentativas: dict[int, list[float]] = {}

    # ------------------------------------------------------------------ consulta

    def papel(self, user_id: int) -> str | None:
        user = self.store.usuario(user_id)
        return user.role if user else None

    def eh_admin(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return self.papel(user_id) == ROLE_ADMIN

    def sessao(self, user_id: int, username: str | None) -> Session | None:
        user = self.store.usuario(user_id)
        if user is None:
            return None
        self.store.touch(user_id, username)
        return Session(user_id=user_id, role=user.role, username=user.username, eh_admin=user.eh_admin)

    # ------------------------------------------------------------------ resgate

    def _conta_tentativas(self, user_id: int) -> tuple[int, float]:
        agora = time.time()
        lista = [t for t in self._tentativas.get(user_id, []) if agora - t < BLOQUEIO_SEGUNDOS]
        self._tentativas[user_id] = lista
        return len(lista), lista[0] if lista else 0.0

    def _registra_falha(self, user_id: int) -> None:
        self._tentativas.setdefault(user_id, []).append(time.time())

    def _travado(self, user_id: int) -> float:
        total, primeira = self._conta_tentativas(user_id)
        if total < TENTATIVAS_MAX:
            return 0.0
        return max(0.0, BLOQUEIO_SEGUNDOS - (time.time() - primeira))

    def resgatou_admin(self, texto: str | None) -> bool:
        """True se o texto trouxer o codigo de resgate como palavra inteira.

        Compara palavra por palavra de proposito: se o codigo fosse testado como
        substring, um texto como 'xRV-9f3a' casaria e gente errada viraria admin.
        """
        if not texto:
            return False
        for parte in texto.split():
            if normaliza(parte.split("@")[0]) == self.codigo:
                return True
        return False

    def assume_admin(self, user_id: int, username: str | None) -> Session:
        anterior = self.store.admin()
        novo = self.store.define_admin(user_id, username)
        self.store.audita(user_id, "admin.assumido", f"id={user_id}")
        if anterior and anterior.user_id != user_id:
            log.warning("admin trocado: %s -> %s", anterior.user_id, user_id)
            self.store.audita(user_id, "admin.substituido", f"antigo={anterior.user_id}")
        return Session(novo.user_id, novo.role, novo.username, True)

    def registra_tentativa(self, user_id: int, ok: bool) -> None:
        if ok:
            self._tentativas.pop(user_id, None)
        else:
            self._registra_falha(user_id)

    def segundos_bloqueio(self, user_id: int) -> float:
        return self._travado(user_id)

    # -------------------------------------------------------------------- chave

    def resgata_chave(self, user_id: int, username: str | None, token: str) -> tuple[Session, Key, bool]:
        """Troca uma chave por acesso. Retorna (sessao, chave, primeira_vez)."""
        dono = self.store.admin()
        if dono is not None and dono.user_id == user_id:
            raise AuthError("voce ja e admin deste bot")
        digest = hash_token(token)
        key = self.store.chave_por_hash(digest)
        if key is None:
            raise AuthError("chave inexistente")
        if not key.ativa:
            raise AuthError("essa chave foi revogada")
        if key.bound_to is not None and key.bound_to != user_id:
            raise AuthError(
                f"essa chave ja esta em uso por {key.bound_name or key.bound_to}. "
                "Se nao for voce, avise o admin: revogue e crie outra."
            )
        if key.role == ROLE_ADMIN:
            raise AuthError("esta versao do bot so emite chave de leitura; o admin e unico")

        primeira = key.bound_to is None
        self.store.registra_uso(key.id, user_id, username)
        self.store.audita(user_id, "chave.usada", f"chave={key.label} id={key.id}")
        self.store.registra_viewer(user_id, username, note=f"chave {key.label}")
        key = self.store.chave_por_hash(digest) or key
        return Session(user_id, ROLE_VIEWER, username, False), key, primeira


class AuthMiddleware(BaseMiddleware):
    """Portao de entrada: resolve quem e a pessoa e cria o acesso na hora.

    Ordem de decisao, toda vez que chega uma mensagem:
      1. ja e admin  -> passa com sessao de admin
      2. o codigo de resgate apareceu no texto -> assume admin e passa
      3. /start <algo> e o usuario ainda nao tem papel -> tenta chave de leitura
      4. ja tem papel (viewer) -> passa com sessao de viewer
      5. nao tem papel nenhum -> dica de como entrar e some

    Tentativas de resgate erradas contam para o bloqueio: 5 em 10 minutos e o
    usuario fica 10 minutos sem tentar, o que trava forca bruta do codigo.
    """

    def __init__(self, auth: Auth, bot: Bot) -> None:
        super().__init__()
        self.auth = auth
        # O bot entra pelo construtor de proposito: este middleware roda no nivel
        # update, onde o objeto do evento e um Update e nao uma Message, entao nao
        # existe atalho .answer/.reply para responder. Alem disso o envio da dica
        # e create_task (fire-and-forget) e o escopo do dispatcher ja foi
        # fechado quando a task roda: resolver o bot por contextvar do
        # dispatcher seria depender de estado ambient. Guardar a referencia e o
        # jeito explicito de nao depender dele.
        self.bot = bot
        self._ultima_dica: dict[int, float] = {}
        # create_task nao segura referencia forte: sem esta lista a task pode
        # ser coletada no meio do envio e a dica nunca chega.
        self._tarefas: set[asyncio.Task] = set()

    async def __call__(self, handler, event, data: dict[str, Any]):
        user = data.get("event_from_user")
        if user is None:
            return None
        chat = data.get("event_chat")
        texto = self._texto(event)
        sessao = self.auth.sessao(user.id, user.username)

        if sessao is not None and sessao.eh_admin:
            data["sessao"] = sessao
            return await handler(event, data)

        # 2. resgate: o codigo vale em qualquer mensagem, mas so palavra inteira
        if self.auth.resgatou_admin(texto):
            travado = self.auth.segundos_bloqueio(user.id)
            if travado:
                self._dica(chat, user.id, txt.erro(f"Codigo nao bate. Tente de novo em {int(travado)}s."))
                return None
            data["sessao"] = self.auth.assume_admin(user.id, user.username)
            self.auth.registra_tentativa(user.id, ok=True)
            log.warning("ADMIN assumido por id=%s usuario=%s", user.id, user.username)
            if chat is not None:
                await self._envia(
                    chat,
                    "\n".join(
                        [
                            txt.cabecalho("🛡️", "voce virou admin"),
                            "",
                            txt.ok("Codigo certo. Este servidor e seu."),
                            txt.sub(["O que estava com o admin anterior virou leitura."]),
                            "",
                            txt.info("Comece por /admin para ver tudo que da para fazer."),
                        ]
                    ),
                )
            return await handler(event, data)

        # 3. /start com argumento: chave de leitura para quem ainda nao tem papel
        if sessao is None and texto:
            argumento = self._argumento_start(texto)
            if argumento:
                travado = self.auth.segundos_bloqueio(user.id)
                if travado:
                    self._dica(chat, user.id, txt.erro(f"Muitas tentativas. Tente de novo em {int(travado)}s."))
                    return None
                try:
                    sessao, key, primeira = self.auth.resgata_chave(user.id, user.username, argumento)
                except AuthError:
                    self.auth.registra_tentativa(user.id, ok=False)
                    self._dica(chat, user.id, self._SEM_ACESSO)
                    return None
                self.auth.registra_tentativa(user.id, ok=True)
                log.info("leitura liberada para id=%s pela chave #%s", user.id, key.id)
                data["sessao"] = sessao
                self._dica(
                    chat,
                    user.id,
                    "\n".join(
                        [
                            txt.cabecalho("🔑", "acesso liberado"),
                            "",
                            txt.ok(f"Chave {key.label} vinculada a esta conta."),
                            txt.info("Daqui para frente voce ve o status: /status"),
                            txt.sub(["So admin mexe em config, lista e reinicio."]),
                        ]
                    ),
                )
                return None

        if sessao is not None:
            data["sessao"] = sessao
            return await handler(event, data)

        # 5. sem papel: /entrar e o unico comando liberado, porque e ele que cria
        #    o acesso. Qualquer outra coisa e ignorada com a dica de como entrar.
        if self._comando(texto) == "/entrar":
            return await handler(event, data)

        self._dica(chat, user.id, self._SEM_ACESSO)
        return None

    _SEM_ACESSO = "\n".join(
        [
            txt.cabecalho("🔒", "voce ainda nao tem acesso"),
            "",
            txt.info("Para ser admin, resgate com o codigo do dono:"),
            txt.sub(["/start CODIGO_DE_RESCATE"]),
            "",
            txt.info("Para so olhar o status, peça uma chave de leitura:"),
            txt.sub(["/entrar CHAVE"]),
        ]
    )

    @staticmethod
    def _texto(event) -> str | None:
        """Texto da mensagem, venha o evento do nivel update ou do nivel message.

        Este middleware e registrado em dp.update (ver main.py), e nesse nivel o
        aiogram entrega o Update, nao a Message: Update nao tem .text, entao
        ler event.text direto devolve None sempre e ninguem nunca resgata o
        admin. No nivel message o proprio evento ja e a Message. Callback query
        fica de fora de proposito: so mensagem enviada pelo usuario entra no
        resgate.
        """
        if isinstance(getattr(event, "text", None), str):
            return event.text
        msg = getattr(event, "message", None)
        if msg is not None and msg is not event and isinstance(getattr(msg, "text", None), str):
            return msg.text
        return None

    @staticmethod
    def _comando(texto: str | None) -> str | None:
        if not texto:
            return None
        primeiro = texto.strip().split(maxsplit=1)[0]
        if not primeiro.startswith("/"):
            return None
        return primeiro.split("@")[0].lower()

    @staticmethod
    def _argumento_start(texto: str) -> str | None:
        partes = texto.strip().split(maxsplit=1)
        if len(partes) != 2 or partes[0].split("@")[0].lower() != "/start":
            return None
        return partes[1].strip()

    def _dica(self, chat, user_id: int, texto: str) -> None:
        agora = time.time()
        if agora - self._ultima_dica.get(user_id, 0) < DICA_COOLDOWN:
            return
        self._ultima_dica[user_id] = agora
        log.info("aviso de acesso para id=%s", user_id)
        if chat is None:
            log.info("update sem chat utilizavel para id=%s: dica descartada", user_id)
            return
        tarefa = asyncio.create_task(self._envia(chat, texto))
        self._tarefas.add(tarefa)
        tarefa.add_done_callback(self._tarefas.discard)

    async def _envia(self, chat, texto: str) -> None:
        """Manda a dica de acesso.

        Vai por bot.send_message e nao por chat.send: Chat nao tem send no
        aiogram 3 (os atalhos do objeto Chat sao ban, set_title, get_member...;
        o de mensagem existe so em Bot). A excecao nao pode ser engolida em
        silencio: foi exatamente o except Exception: pass daqui que escondeu
        um AttributeError e deixou o bot mudo sem ninguem perceber.
        """
        try:
            await self.bot.send_message(chat.id, texto)
        except Exception:
            log.exception("falha ao enviar aviso para chat=%s", chat.id)


class AdminOnly(BaseMiddleware):
    """Middleware de router: barra tudo que nao vier com sessao de admin.

    Vai de middleware (e nao de filtro) porque o filtro de evento do aiogram
    recebe so o objeto Message/CallbackQuery, e o papel vive no dicionario de
    dados injetado pelo AuthMiddleware.
    """

    def __init__(self, aviso: str | None = None) -> None:
        super().__init__()
        self.aviso = aviso or txt.erro("Isso e so para admin.")

    async def __call__(self, handler, event, data: dict[str, Any]):
        sessao = data.get("sessao")
        if getattr(sessao, "eh_admin", False):
            return await handler(event, data)
        log.info("bloqueado por papel: id=%s", getattr(data.get("event_from_user"), "id", "?"))
        # Este middleware fica no nivel message/callback_query, entao o evento ja
        # e a Message ou o CallbackQuery e da para responder por ele mesmo. O
        # chat.send() usado antes nao existe no aiogram 3 e o aviso sumia.
        try:
            if isinstance(event, Message):
                await event.answer(self.aviso)
            elif event.message is not None:
                await event.message.answer(self.aviso)
        except Exception:
            log.exception("falha ao avisar bloqueio por papel")
        return None
