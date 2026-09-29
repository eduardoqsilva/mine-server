"""Confere as 5 permissoes do Dropbox sem parar o servidor.

Sobe um tar.gz de bosta, lista a pasta, cria um link e apaga o proprio
arquivo: e o caminho inteiro do /backup em ~5 segundos, contra os 3 minutos
que um backup de verdade leva com o jogo fora do ar.

    python tools/dropbox_check.py

Sai com codigo 0 se tudo deu certo, 1 se faltou permissao. Da para rodar de
novas quantas vezes quiser: o arquivo de teste e apagado no fim.
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PASTA = "/backups"


def _acha_app() -> None:
    """Acha o pacote `app` tanto na repo quanto na imagem do bot.

    Na repo o pacote esta em bot/app, e na imagem em /app/app, entao o mesmo
    caminho relativo nao serve para os dois. Sem isto o script so rodaria na
    sua maquina e nao dentro do container, que e onde o .env esta.
    """
    aqui = Path(__file__).resolve().parent
    for candidato in (aqui.parent / "bot", aqui.parent, Path("/app")):
        if (candidato / "app" / "dropbox.py").is_file():
            sys.path.insert(0, str(candidato))
            return
    print("Nao achei o pacote `app`. Rode de dentro da repo ou de dentro do container.")
    raise SystemExit(1)


_acha_app()

# import fora do topo de proposito: o sys.path so pode ser mexido depois do
# _acha_app, que e quem descobre onde o pacote esta neste layout
from app import dropbox

ESCOPOS = (
    "files.content.write",
    "files.metadata.read",
    "files.metadata.write",
    "sharing.write",
    "sharing.read",
)


def escopos_do_token(access: str) -> dict[str, bool]:
    """Pergunta ao proprio Dropbox quais escopos o token carrega.

    Importante porque "permissao faltando" tem duas causas que exigem
    consertos diferentes: a app nao tem o escopo (vao na aba Permissions), ou
    a app tem mas o token e antigo (refaca a troca). A API /2/check/user
    responde só com os escopos que o token tem, e e o jeito de distinguir as
    duas sem chute.
    """
    import json
    import urllib.request

    corpo = json.dumps({"scope": list(ESCOPOS)}).encode()
    req = urllib.request.Request(
        "https://api.dropboxapi.com/2/check/user",
        data=corpo,
        headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
        method="POST",
    )
    dados = json.loads(urllib.request.urlopen(req, timeout=60).read())["result"]
    if not isinstance(dados, dict):
        # o Dropbox devolve uma string vazia (e nao um mapa) quando o token
        # nao tem nenhum dos escopos pedidos, que e justo o caso que mais
        # importa aqui
        return {escopo: False for escopo in ESCOPOS}
    return {escopo: bool(dados.get(escopo, False)) for escopo in ESCOPOS}


def main() -> int:
    app_key = os.getenv("DROPBOX_APP_KEY", "").strip()
    app_secret = os.getenv("DROPBOX_APP_SECRET", "").strip()
    refresh = os.getenv("DROPBOX_REFRESH_TOKEN", "").strip()

    if not (app_key and app_secret and refresh):
        print("Falta DROPBOX_APP_KEY, DROPBOX_APP_SECRET ou DROPBOX_REFRESH_TOKEN no .env")
        return 1

    dbx = dropbox.Dropbox(token="", refresh_token=refresh, app_key=app_key, app_secret=app_secret)

    print("1. renovando o access token")
    try:
        access = dbx._token()
    except dropbox.DropboxError as exc:
        print(f"   FALHOU: {exc}")
        print(
            "   Se o erro fala de grant, o refresh token foi revogado ou o app secret"
            "\n   foi resetado: refaca o processo da URL de autorizacao."
        )
        return 1
    print("   ok: o refresh token esta valendo")

    print("\n2. o que este token consegue fazer (escopos que ele carrega)")
    try:
        do_token = escopos_do_token(access)
    except Exception as exc:  # noqa: BLE001 - aqui so precisa do texto do erro
        print(f"   nao deu para consultar: {exc}")
        do_token = {}
    for escopo, tem in do_token.items():
        print(f"   {'ok  ' if tem else 'NAO '} {escopo}")
    if do_token and not any(do_token.values()):
        print(
            "\n   >>> O TOKEN NAO TEM NENHUM DOS ESCOPOS. <<<\n\n"
            "   A app nao esta concedendo nenhum destes 5 escopos. Isso tem uma\n"
            "   unica causa provavel, e ela e na configuracao da app, nao no token:\n\n"
            "     1. Voce esta na app certa? A API nomeia o app como ID 8703075.\n"
            "        Se voce tem mais de uma app no console, provavelmente editou outra.\n"
            "     2. As 5 caixas estao marcadas E aplicadas? Marcar nao basta se a\n"
            "        mudanca nao foi salva/aplicada na pagina.\n"
            "     3. A app esta em rascunho, com as permissoes pendentes de aprovacao?\n\n"
            "   Refazer a troca do codigo NAO resolve isso: um token recem-criado sai\n"
            "   com a mesma lista de escopos, porque quem decide a lista e a app.\n\n"
            "   Depois de corrigir, so a troca do codigo e precisa ser refeita:\n"
            "     python tools/dropbox_token.py APP_KEY APP_SECRET CODIGO_NOVO --salvar\n"
            "     docker compose up -d --force-recreate bot"
        )
        return 1

    # um tar.gz de verdade: o upload por sessao e o caminho que o bot usa com
    # o backup de 35 MB, e um upload simples nao exercitaria o mesmo codigo
    with tempfile.TemporaryDirectory() as tmp:
        arquivo = Path(tmp) / f"mine-bedrock-PERMISSOES-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.tar.gz"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w|gz") as tar:
            info = tarfile.TarInfo("data/_teste.txt")
            info.size = 5
            tar.addfile(info, io.BytesIO(b"teste"))
        arquivo.write_bytes(buf.getvalue())
        destino = f"{PASTA}/{arquivo.name}"

        falhas: list[str] = []
        link = ""

        def cheque(escopo: str, nome: str, acao) -> None:
            nonlocal link
            try:
                link = acao() or ""
                print(f"   ok   {escopo:<20} {nome}")
            except dropbox.DropboxError as exc:
                print(f"   ERRO {escopo:<20} {nome}")
                print(f"        {exc}")
                falhas.append(escopo)

        print(f"\n3. subindo {arquivo.stat().st_size} bytes por sessao (o caminho de 35 MB)")
        cheque("files.content.write", "sobe o arquivo", lambda: dbx.envia(arquivo, destino))

        print("\n4. o resto, com o arquivo ja la")
        entradas: list[dict] = []
        cheque("files.metadata.read", "lista a pasta", lambda: entradas.extend(dbx.lista(PASTA)))
        if entradas:
            print(f"        {len(entradas)} arquivo(s) em {PASTA}")
        cheque("sharing.write", "cria o link", lambda: dbx.link(destino))
        cheque("sharing.read", "procura o link", lambda: dbx.link(destino))
        cheque("files.metadata.write", "apaga o arquivo", lambda: dbx.apaga(destino))

    if link:
        print(f"\n   link de exemplo (sera invalido depois do apaga): {link}")

    print()
    if falhas:
        print("FALTARAM:", ", ".join(dict.fromkeys(falhas)))
        print("\nVa em https://www.dropbox.com/developers/apps -> clique na sua app")
        print("-> aba Permissions e marque as que faltam. Recarregue a pagina depois:")
        print("se as caixinhas voltarem vazias, nao salvou.")
        return 1
    print("As 5 permissoes estao de pe. O /backup deve funcionar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
