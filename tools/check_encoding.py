"""Confere encoding dos arquivos de texto do projeto (BOM, UTF-8 invalido, mojibake)."""

from __future__ import annotations

import pathlib
import sys

ALVOS = {".py", ".yml", ".yaml", ".md", ".txt", ".conf", ".stream", ".exemplo", ".example", ""}
# Diretorios de artefato: nao sao texto do projeto. O data/ tem o LevelDB do
# mundo (binario) e arquivos de backup que o proprio BDS escreve, e o
# .ruff_cache/ e .pytest_cache/ sao binarios de ferramenta. Sem esta lista o
# check acusa lixo de outra pessoa e para de valer.
IGNORADOS = {
    ".git", "data", "state", "backups", "worlds",
    ".ruff_cache", ".pytest_cache", ".mypy_cache", "__pycache__", ".venv", "venv", "node_modules",
}
MOJIBAKE = ("\u00c3", "\u00e2\u20ac", "\u00f0\u0178", "\u00ef\u00bf\u00bd", "\u00c2", "\ufffd")
RUINS = ("bringing", "Use `--stop`")

problemas = 0
for p in sorted(pathlib.Path(".").rglob("*")):
    if not p.is_file() or IGNORADOS.intersection(p.parts) or p.suffix not in ALVOS:
        continue
    if p.name == "check_encoding.py":  # a lista de lixo mora aqui dentro
        continue
    raw = p.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        print("BOM          ", p)
        problemas += 1
    try:
        texto = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        print("NAO-UTF8     ", p, exc)
        problemas += 1
        continue
    achados = [m for m in MOJIBAKE if m in texto] + [r for r in RUINS if r in texto]
    if achados:
        print("SUSPEITO     ", p, [a.encode("unicode_escape").decode() for a in achados])
        problemas += 1

print("problemas:", problemas)
sys.exit(1 if problemas else 0)
