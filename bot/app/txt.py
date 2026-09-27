"""Vocabulario de formatacao das mensagens: titulo em caixa alta, icone por
situacao e divisor.

Tudo aqui e texto puro de proposito. O bot nao manda parse_mode em nenhuma
mensagem, entao Markdown apareceria com os asteriscos na tela e HTML exigiria
escapar cada trecho que veio do mundo (nome de pack, versao) - e um & solto
quebraria a mensagem inteira. Com texto puro, caixa alta, icone e divisor, o
resultado ja fica legivel e nada precisa de escape.

Os textos do projeto sao escritos sem acento (voce, nao, esta). Isso vale
tambem para as mensagens: acento novo em so um lugar deixaria o bot com duas
grafias diferentes na mesma tela.
"""

from __future__ import annotations

# Divisor dos titulos. Caractere de caixa cheia, nao "-": o Telegram renderiza
# o bloco em fonte proporcional e o tracejado de unico caractere fica fraco.
DIVISOR = "━━━━━━━━━━━━━━━━━━━━"

# Um icone por situacao, usado sempre no mesmo lugar da linha para o olho
# pegar o padrao sem ler.
OK = "✅"
ERRO = "❌"
AVISO = "⚠️"
INFO = "ℹ️"

# Icones por assunto, so para quebrar visualmente as listas longas.
SERVIDOR = "🛠️"
JOGADORES = "👥"
CHAVES = "🔑"
PACOTES = "📦"
LOG = "📋"
CONFIG = "⚙️"
MUNDO = "🌍"


def cabecalho(icone: str, texto: str) -> str:
    """Titulo principal, com divisor embaixo."""
    return f"{icone} {texto.upper()}\n{DIVISOR}"


def secao(icone: str, texto: str, contagem: object = None) -> str:
    """Titulo de bloco. `contagem` aparece entre parenteses quando informado."""
    sufixo = f" ({contagem})" if contagem not in (None, "", 0) else ""
    return f"\n{icone} {texto.upper()}{sufixo}"


def campo(rotulo: str, valor: object) -> str:
    """Linha "rotulo: valor" de um dicionario de dados."""
    return f"{rotulo}: {valor}"


def sub(linhas: list[str], recuo: str = "   ") -> str:
    """Bloco de detalhe sob um item, com recuo para ler como subtexto."""
    return "\n".join(f"{recuo}{linha}" for linha in linhas)


def ok(texto: str) -> str:
    return f"{OK} {texto}"


def erro(texto: str) -> str:
    return f"{ERRO} {texto}"


def aviso(texto: str) -> str:
    return f"{AVISO} {texto}"


def info(texto: str) -> str:
    return f"{INFO} {texto}"
