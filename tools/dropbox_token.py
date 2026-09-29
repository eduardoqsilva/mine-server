"""Troca o codigo do OAuth por um refresh token do Dropbox.

Roda em qualquer maquina que tenha Python:

    python tools/dropbox_token.py APP_KEY APP_SECRET CODE --salvar

Com --salvar o script escreve as 3 linhas no .env de uma vez, que e o jeito
recomendado: colar na mao e onde esse processo costuma falhar, com o token
velho ficando no arquivo sem ninguem perceber.

Sem --salvar ele so imprime as 3 linhas, para voce colar a mao.

Para nao colar o secret no historico do terminal:

    $env:DROPBOX_APP_KEY = "..."; $env:DROPBOX_APP_SECRET = "..."
    python tools/dropbox_token.py CODIGO --salvar

Por que este script existe: o refresh token nao aparece em lugar nenhum do
App Console. O botao "Generate" da secao OAuth 2 da app so emite um access
token, que expira em ~4 horas. O refresh token so existe depois da troca do
codigo de autorizacao por tokens, e e isso que este script faz.

O CODE sai da pagina do Dropbox quando voce abre a URL que este script
imprime e aceita o acesso. Sem redirect_uri cadastrado, o Dropbox mostra o
codigo na propria pagina, para copiar e colar aqui.

A URL que este script imprime ja traz o parametro scope com os 4 escopos de
que o bot precisa. Isso e essencial: sem o scope, o Dropbox usa o conjunto
PADRAO da app, e e esse conjunto padrao que sai vazio mesmo com as caixas da
aba Permissions marcadas - o token fica sem escopo nenhum e so se descobre
quando o primeiro upload falha, no meio de um backup. Com o scope, o Dropbox
tambem passa a recusar em voz alta se a app nao puder pedir aquele escopo.

Marque as permissoes na aba Permissions da app antes de abrir a URL, e
confirme com F5 que elas continuam marcadas depois do reload: se voltarem
desmarcadas, o Submit do rodapé (que o banner de cookies pode cobrir) nao
foi clicado e nada disso vai funcionar.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
AUTORIZAR = "https://www.dropbox.com/oauth2/authorize"

# Os 4 escopos que o bot realmente usa, com o endpoint que exige cada um:
#   files.content.write  upload por sessao e delete do backup velho
#   files.metadata.read  listar a pasta para achar o mais velho
#   sharing.write        criar o link do backup novo
#   sharing.read         recuperar o link quando ele ja existe
# files.metadata.write nao e necessario: nenhum endpoint chamado pelo bot usa
# ele (o delete exige files.content.write, e a API confirma isso em erro).
ESCOPOS = (
    "files.content.write",
    "files.metadata.read",
    "sharing.write",
    "sharing.read",
)


def falha(mensagem: str) -> None:
    print(f"\nERRO: {mensagem}\n")
    raise SystemExit(1)


def gravar_env(destino: Path, novas: dict[str, str]) -> Path:
    """Troca so as linhas DROPBOX_* no .env, sem mexer no resto.

    Existe porque colar 3 linhas na mao e exatamente onde esse processo
    costuma dar errado: a linha vai para outro arquivo, o .env aberto no
    editor sobrescreve depois, ou o token velho fica. Aqui nao ha como errar.

    Nao cria copia de backup do arquivo de proposito: ela carregaria os
    segredos num arquivo que o .gitignore nao cobre. O que o script faz e
    reescrever 3 linhas, e o token antigo e descartavel em 30 segundos.

    Le com utf-8-sig porque .env salvo pelo Bloco de Notas ou por powershell
    ganha BOM, e o caractere de BOM nao conta como espaço: sem o sig a chave
    da linha nao casaria e o script acrescentaria uma duplicada em vez de
    trocar a velha. Grava sem BOM para o proximo programa ler limpo.
    """
    if destino.exists():
        linhas = destino.read_text(encoding="utf-8-sig").splitlines()
    else:
        linhas = ["# .env gerado por tools/dropbox_token.py"]

    vistas: set[str] = set()
    for i, linha in enumerate(linhas):
        chave = linha.split("=", 1)[0].strip()
        if chave in novas:
            linhas[i] = f"{chave}={novas[chave]}"
            vistas.add(chave)

    faltando = [c for c in novas if c not in vistas]
    if faltando:
        if linhas and linhas[-1].strip():
            linhas.append("")
        for chave in faltando:
            linhas.append(f"{chave}={novas[chave]}")

    destino.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    trocadas = ", ".join(sorted(vistas)) or "nenhuma"
    print(f"   linhas reescritas: {trocadas}")
    if faltando:
        print(f"   linhas acrescentadas: {', '.join(faltando)}")
    return destino


def main(argv: list[str]) -> int:
    # tira as flags antes de contar os posicionais, senao "KEY SECRET CODE
    # --salvar" vira 4 argumentos e o script cai na ajuda em vez de rodar
    salvar = "--salvar" in argv[1:]
    args = [arg for arg in argv[1:] if arg != "--salvar"]

    if any(arg in ("-h", "--help") for arg in args):
        print(__doc__)
        return 0

    if len(args) == 3:
        app_key, app_secret, code = args
    elif len(args) == 1:
        app_key = os.getenv("DROPBOX_APP_KEY", "")
        app_secret = os.getenv("DROPBOX_APP_SECRET", "")
        code = args[0]
    else:
        print(__doc__)
        return 1

    app_key, app_secret, code = app_key.strip(), app_secret.strip(), code.strip()
    if not (app_key and app_secret and code):
        falha("preciso do app key, do app secret e do codigo de autorizacao (veja o texto deste script)")

    if salvar:
        print("   (com --salvar eu escrevo no .env por voce, sem mostrar os valores)")

    print(f"App key: {app_key[:6]}...  ({len(app_key)} caracteres)")
    print("\nAbra esta URL no navegador, aceite o acesso e copie o codigo que o")
    print("Dropbox mostrar na propria pagina (ele mostra porque o redirect_uri e")
    print("opcional nesse fluxo). A URL ja vem com os escopos que o bot precisa:\n")

    url = (
        f"{AUTORIZAR}?client_id={app_key}"
        f"&response_type=code&token_access_type=offline"
        f"&scope={urllib.parse.quote(' '.join(ESCOPOS))}"
    )
    print(f"  {url}\n")

    print("O parametro scope nao e opcional aqui: sem ele o Dropbox usa o conjunto")
    print("PADRAO da app, e e esse conjunto padrao que costuma sair vazio mesmo com as")
    print("caixas da aba Permissions marcadas. Pedir os escopos na URL resolve isso,")
    print("e de quebra o Dropbox passa a recusar em voz alta se a app nao puder pedi-los,")
    print("em vez de entregar um token silenciosamente sem escopo nenhum.\n")
    print("Escopos pedidos nesta URL:")
    for escopo in ESCOPOS:
        print(f"  - {escopo}")
    print("\nTrocando o codigo por tokens...")

    form = urllib.parse.urlencode(
        {
            "code": code,
            "grant_type": "authorization_code",
            "client_id": app_key,
            "client_secret": app_secret,
        }
    ).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=form, method="POST"), timeout=60) as resp:
            dados = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        corpo = exc.read().decode("utf-8", "replace")
        try:
            erro = json.loads(corpo).get("error_description", corpo)
        except ValueError:
            erro = corpo
        if "invalid_grant" in erro:
            # o erro mais comum, e a causa quase sempre e uma destas duas
            erro += (
                "\nO codigo expira em ~1h e so funciona uma vez. Se ja usou, gere outro"
                "\npela URL acima e confira se app key e app secret sao do mesmo app."
            )
        falha(erro)
    except urllib.error.URLError as exc:
        falha(f"nao consegui falar com o Dropbox: {exc.reason}")

    refresh = dados.get("refresh_token", "")
    if not refresh:
        falha(
            "o Dropbox devolveu token sem refresh_token. Acontece quando a URL de autorizacao\n"
            "nao tem token_access_type=offline. Refaca abrindo a URL acima."
        )

    expira_em = int(dados.get("expires_in", 0))
    quando = datetime.now(timezone.utc) + timedelta(seconds=expira_em)
    novas = {
        "DROPBOX_APP_KEY": app_key,
        "DROPBOX_APP_SECRET": app_secret,
        "DROPBOX_REFRESH_TOKEN": refresh,
    }

    salvar_arquivo = "--salvar" in argv[1:]
    if salvar_arquivo:
        destino = gravar_env(Path(".env"), novas)
        print(f"\nEscrevi as 3 linhas em {destino} (o resto do arquivo ficou como estava).")
    else:
        print("\n" + "=" * 64)
        print("Cole estas 3 linhas no seu .env:\n")
        for chave, valor in novas.items():
            print(f"{chave}={valor}")
        print("=" * 64)
        print("\nDica: com --salvar no fim do comando, o script escreve no .env por voce.")

    print(
        f"\nO access token desta troca dura {expira_em // 3600}h (sai as "
        f"{quando.strftime('%H:%M')} UTC) e voce nao precisa anotar: e o refresh token\n"
        "que o bot usa para renovar sozinho. Nao cole o access token no .env."
    )
    print("\nDepois: docker compose up -d --force-recreate bot, e /backup no Telegram.")
    print("Confere que o token novo pegou os escopos com: python tools/dropbox_check.py\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
