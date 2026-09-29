"""Cliente do Dropbox: subir o backup, girar os antigos e devolver o link.

Stdlib de proposito: a imagem do bot so tem aiogram, docker e tzdata, e
colocar requests aqui seria uma dependencia a mais por causa de um POST. O
que incomoda no Dropbox nao e o HTTP, e a autenticacao: o access token dura
4 horas, entao guardar um token fixo no .env funciona por um tempo e depois
falha em silencio, bem longe de um backup. Por isso o cliente prefere
refresh token, que nao expira, e so usa o access token como atalho quando
nao ha como renovar.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("bds.dropbox")

API = "https://api.dropboxapi.com"
CONTENT = "https://content.dropboxapi.com"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"

# 4 MB e o teto de chunk da API de sessao (o antigo, ainda valido). O upload
# simples so aceitaria 150 MB, e um backup com packs grandes estoura isso,
# entao tudo vai por sessao: um caminho so, e ele e o exercitado sempre.
CHUNK = 4 * 1024 * 1024
RENOVAR_COM_MARGEM = 300

Http = Callable[..., bytes]


class DropboxError(RuntimeError):
    """Erro do Dropbox, com o codigo original para decidir o que fazer."""

    def __init__(self, mensagem: str, codigo: str = "") -> None:
        super().__init__(mensagem)
        self.codigo = codigo


def _http(url: str, *, body: bytes | None = None, headers: dict | None = None, timeout: float = 300.0) -> bytes:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        # O Dropbox responde o erro como JSON no corpo, mas em XML se o
        # Accept nao pedir JSON. Ler o corpo e o jeito de ter a mensagem de
        # verdade em vez de "HTTP Error 409".
        raise DropboxError(*_erro_http(exc)) from exc
    except urllib.error.URLError as exc:
        # conexao cortada no meio do envio e o sintoma de "o servidor recusou
        # antes de ler o corpo", que e o caso classico de falta de permissao
        # num upload grande. A mensagem crua ("Broken pipe") nao ajuda ninguem
        # a descobrir isso, entao o texto diz as duas coisas.
        if isinstance(exc.reason, (BrokenPipeError, ConnectionResetError)):
            raise DropboxError(
                "o Dropbox cortou a conexao no meio do envio. Quase sempre e "
                "permissao faltando na app (veja a aba Permissions): com o "
                "header Expect: 100-continue o erro real deveria ter chegado "
                "antes do corpo, e se chegou assim e para olhar os escopos da app.",
                "conexao_cortada",
            ) from exc
        raise DropboxError(f"nao consegui falar com o Dropbox: {exc.reason}") from exc


def _erro_http(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """Traduz a resposta de erro do Dropbox em (frase para o usuario, sigla).

    O Dropbox manda o mesmo erro de tres jeitos, e so um deles e JSON:
    a API de arquivos responde JSON com user_message, a de metadados responde
    texto puro com a mesma frase, e uma app sem escopo nenhum pode receber um
    400 sem corpo. Ler so o JSON e jogar o resto fora erra duas das tres, e o
    preco e alto: a permissao que falta deixava de aparecer e o usuario
    recebia "Bad Request", que nao diz nada.
    """
    try:
        corpo = exc.read().decode("utf-8", "replace").strip()
    except OSError:
        corpo = ""

    if not corpo:
        return (
            (
                f"o Dropbox recusou sem dar detalhe (HTTP {exc.code}). Quase sempre e "
                "permissao faltando na app: va na aba Permissions dela e ligue os "
                "escopos de files.* e sharing.*"
            ),
            "sem_detalhe",
        )

    try:
        dados = json.loads(corpo)
    except ValueError:
        # texto puro: e a propria explicacao do Dropbox, entao e a melhor
        # mensagem que existe. Corta em 2 linhas porque o Telegram tem limite
        # e o log do servidor nao precisa da novela inteira
        linhas = [linha for linha in corpo.splitlines() if linha.strip()]
        return (" ".join(linhas[:2])[:400], "texto")

    # O user_message.text vem antes do error_summary de proposito. O summary
    # e uma sigla interna ("other/...", "path/not_found/...") e nao diz nada
    # de util; o text e a frase que o Dropbox escreveu para a pessoa, e e ela
    # que explica o que fazer. Sem esta precedencia, falta de permissao
    # aparecia como "other/..." e nao dava para descobrir o problema.
    texto = ""
    user_message = dados.get("user_message")
    if isinstance(user_message, dict):
        texto = str(user_message.get("text", "")).strip()

    resumo = dados.get("error_summary") or dados.get("error") or exc.reason
    if isinstance(resumo, dict):
        resumo = resumo.get(".tag", json.dumps(resumo))
    codigo = ""
    if isinstance(dados.get("error"), dict):
        codigo = dados["error"].get(".tag", "")
    elif isinstance(resumo, str) and resumo.startswith("/"):
        codigo = resumo.split("/")[-1].split("\n")[0]

    if texto:
        # a sigla fica no fim, para o log do servidor ter o codigo cru e o
        # Telegram mostrar a frase util
        return (f"{texto} [{codigo or resumo}]").split("\n")[0], codigo
    return f"{resumo}".split("\n")[0] or f"HTTP {exc.code}", codigo


class Dropbox:
    def __init__(
        self,
        *,
        token: str = "",
        refresh_token: str = "",
        app_key: str = "",
        app_secret: str = "",
        http: Http | None = None,
        agora: Callable[[], float] = time.time,
    ) -> None:
        self._fixo = token.strip()
        self._refresh = refresh_token.strip()
        self._app_key = app_key.strip()
        self._app_secret = app_secret.strip()
        # Resolvido aqui e nao como valor padrao do argumento: um padrao
        # seria preso no momento em que o modulo foi carregado, e trocar
        # dropbox._http depois (para testar contra um servidor local) nao
        # teria efeito nenhum.
        self._http = http or _http
        self._agora = agora
        self._access = ""
        self._validade = 0.0

    def configurado(self) -> bool:
        return bool(self._fixo or (self._refresh and self._app_key and self._app_secret))

    # ------------------------------------------------------------------ auth

    def _token(self) -> str:
        if self._access and self._agora() < self._validade - RENOVAR_COM_MARGEM:
            return self._access
        if self._fixo and not (self._refresh and self._app_key and self._app_secret):
            return self._fixo
        if self._refresh and self._app_key and self._app_secret:
            return self._renova()
        if self._fixo:
            return self._fixo
        raise DropboxError("sem credencial do Dropbox no .env (DROPBOX_REFRESH_TOKEN + DROPBOX_APP_KEY + DROPBOX_APP_SECRET)")

    def _renova(self) -> str:
        form = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh,
                "client_id": self._app_key,
                "client_secret": self._app_secret,
            }
        ).encode()
        bruto = self._http(
            TOKEN_URL,
            body=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        dados = json.loads(bruto)
        self._access = dados["access_token"]
        self._validade = self._agora() + float(dados.get("expires_in", 14400))
        log.info("access token do Dropbox renovado, vale ate %s", dados.get("expires_in"))
        return self._access

    def _api(self, rota: str, corpo: dict) -> dict:
        bruto = self._http(
            API + rota,
            body=json.dumps(corpo).encode(),
            headers={"Authorization": "Bearer " + self._token(), "Content-Type": "application/json"},
        )
        return json.loads(bruto) if bruto else {}

    def _conteudo(self, rota: str, api_arg: dict, body: bytes) -> dict:
        bruto = self._http(
            CONTENT + rota,
            body=body,
            headers={
                "Authorization": "Bearer " + self._token(),
                "Dropbox-API-Arg": json.dumps(api_arg),
                "Content-Type": "application/octet-stream",
                # O Dropbox valida token e permissao ANTES de ler o corpo. Sem
                # este header, o http.client comeca a despejar os 4 MB na
                # mesma hora, o Dropbox recusa e fecha a conexao, e o erro
                # chega como "Broken pipe": some o status, o corpo e o JSON
                # que diz o que aconteceu. Com o 100-continue o servidor
                # responde primeiro e o erro real vem inteiro.
                "Expect": "100-continue",
            },
        )
        return json.loads(bruto) if bruto else {}

    # ---------------------------------------------------------------- upload

    def envia(self, caminho: Path, destino: str) -> str:
        """Sobe o arquivo por sessao e devolve o path no Dropbox.

        Sessao em vez do upload simples porque o simples para em 150 MB e um
        backup com packs passa disso sem esforço. Uma sessao comeca com o
        primeiro chunk, vai somando os do meio e fecha com o ultimo, que pode
        ser vazio - que e o caso normal aqui, porque o loop abaixo ja mandou
        tudo.
        """
        tamanho = caminho.stat().st_size
        with caminho.open("rb") as fh:
            pedaco = fh.read(CHUNK)
            sessao = self._conteudo("/2/files/upload_session/start", {"close": False}, pedaco)["session_id"]
            offset = len(pedaco)
            while True:
                pedaco = fh.read(CHUNK)
                if not pedaco:
                    break
                self._conteudo(
                    "/2/files/upload_session/append_v2",
                    {"cursor": {"session_id": sessao, "offset": offset}, "close": False},
                    pedaco,
                )
                offset += len(pedaco)
            self._conteudo(
                "/2/files/upload_session/finish",
                {
                    "cursor": {"session_id": sessao, "offset": offset},
                    "commit": {
                        "path": destino,
                        "mode": "overwrite",
                        "autorename": False,
                        "mute": True,
                    },
                },
                b"",
            )
        log.info("Dropbox: %s upado (%.1f MB)", destino, tamanho / 1048576)
        return destino

    # ------------------------------------------------------- lista e limpeza

    def lista(self, pasta: str) -> list[dict]:
        """Arquivos da pasta, com paginacao cuidada."""
        entradas: list[dict] = []
        cursor = ""
        while True:
            corpo: dict = {"path": pasta, "recursive": False}
            if cursor:
                corpo["cursor"] = cursor
            dados = self._api("/2/files/list_folder", corpo)
            entradas.extend(e for e in dados.get("entries", []) if e.get(".tag") == "file")
            if not dados.get("has_more"):
                return entradas
            cursor = dados["cursor"]

    def apaga(self, caminho: str) -> None:
        self._api("/2/files/delete_v2", {"path": caminho})

    def roda(self, pasta: str, prefixo: str, manter: int) -> tuple[list[str], int, int]:
        """Deixa so os `manter` mais novos backups. Devolve (apagados, bytes, sobraram).

        Filtra pelo prefixo de proposito: a pasta e do usuario, e apagar o que
        ele tenha jogado la dentro seria um jeito bem ruim de perder arquivo.
        A ordenacao e pelo nome porque o nome tem timestamp no comeco
        (AAAAMMDDThhmmZ) e isso ordena igual a hora, sem depender de o
        list_folder devolver em ordem de verdade.

        O total que sobra volta junto porque o relatorio do /backup diz quantos
        backups estao guardados, e recalcular isso aqui custaria uma segunda
        listagem da pasta.
        """
        meus = sorted(
            (e for e in self.lista(pasta) if e["name"].startswith(prefixo)),
            key=lambda e: e["name"],
        )
        velhos = meus[: max(0, len(meus) - manter)]
        apagados: list[str] = []
        bytes_livres = 0
        for entrada in velhos:
            self.apaga(entrada["path_display"])
            apagados.append(entrada["name"])
            bytes_livres += int(entrada.get("size", 0))
        if apagados:
            log.info("Dropbox: apaguei %d backup(s) antigo(s)", len(apagados))
        return apagados, bytes_livres, len(meus) - len(apagados)

    # ------------------------------------------------------------------ link

    def link(self, caminho: str) -> str:
        """Link permanente do arquivo.

        O temporario do Dropbox expira em 4 horas, o que e inutil para quem
        quer olhar o backup amanha. Um link compartilhado continua valendo,
        e se ele ja existir o Dropbox recusa com shared_link_already_exists
        - nesse caso o caminho e buscar o que ja existe, que e o que o
        usuario espera: o mesmo link de sempre.

        A visibilidade vai explicita porque o padrao da API, quando o campo
        nao vem, e "public": o .tar.gz tem o mundo inteiro e o bot.db com a
        lista de admins e o historico de auditoria. Quem tem o link baixa.
        """
        try:
            return self._api(
                "/2/sharing/create_shared_link_with_settings",
                {"path": caminho, "settings": {"requested_visibility": "public"}},
            )["url"]
        except DropboxError as exc:
            if exc.codigo != "shared_link_already_exists":
                raise
        dados = self._api("/2/sharing/list_shared_links", {"path": caminho, "direct_only": True})
        links = dados.get("links", [])
        if not links:
            raise DropboxError("o arquivo subiu, mas o Dropbox nao devolveu link nenhum", "sem_link")
        return links[0]["url"]
