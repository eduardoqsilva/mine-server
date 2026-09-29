#!/usr/bin/env python3
"""Reset completo do ambiente do Mine Bedrock.

Este script:
1. pede confirmacao explicita; 
2. derruba o compose;
3. apaga o mundo e o banco sqlite em ./data e ./state;
4. recria as pastas vazias; 
5. sobe o docker compose novamente.

Ele NUNCA apaga os arquivos do projeto (compose.yml, .env, codigo fonte, etc.),
so limpa os dados do runtime do servidor e do bot.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def confirm(message: str) -> bool:
    try:
        resposta = input(message).strip()
    except EOFError:
        print("\nSem entrada interativa. Abortado.")
        return False
    return resposta == "RESET_ALL"


def ensure_repo_root(repo_root: Path) -> None:
    if not (repo_root / "compose.yml").is_file():
        raise FileNotFoundError(f"compose.yml nao encontrado em {repo_root}. Rode o script na raiz do projeto.")


def clear_directory(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    if not path.is_dir():
        raise NotADirectoryError(f"Esperava diretorio: {path}")
    for item in path.iterdir():
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()


def run(cmd: list[str]) -> None:
    print("\n>>>", " ".join(cmd))
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"Comando falhou com codigo {proc.returncode}: {' '.join(cmd)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset completo do ambiente Mine Bedrock.")
    parser.add_argument(
        "--yes",
        "--force",
        action="store_true",
        help="Executa sem pedir confirmacao interativa. Use com cuidado.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    try:
        ensure_repo_root(repo_root)
    except Exception as exc:
        print(f"Erro: {exc}")
        return 1

    print("=" * 72)
    print("RESET COMPLETO DO SERVIDOR MINE BEDROCK")
    print("=" * 72)
    print("Isso vai apagar:")
    print("- ./data (mundo, server.properties, packs, logs do servidor)")
    print("- ./state (banco sqlite do bot, backups e auditoria)")
    print("- parar containers do compose e subir tudo de novo")
    print("")
    print("Nao apaga os arquivos do projeto, como compose.yml, .env, codigo-fonte e scripts.")
    print("=" * 72)

    if args.yes:
        confirmou = True
        print("Modo --yes ativado: confirmacao automatica.")
    else:
        confirmou = confirm("Digite RESET_ALL para confirmar e zera tudo: ")

    if not confirmou:
        print("Operacao cancelada. Nenhum dado foi apagado.")
        return 0

    print("\nIniciando reset...")

    try:
        run(["docker", "compose", "down", "--remove-orphans"])
        for alvo in (repo_root / "data", repo_root / "state"):
            clear_directory(alvo)
        run(["docker", "compose", "up", "-d", "--build"])
    except Exception as exc:
        print(f"\nFalha durante o reset: {exc}")
        return 1

    print("\nReset completo.")
    print("O servidor e o bot foram reiniciados com o ambiente limpo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
