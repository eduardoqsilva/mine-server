"""Tira o BOM UTF-8 dos arquivos de texto (o Python aceita, mas yml/sh/nginx nao)."""

from __future__ import annotations

import pathlib
import sys

BOM = b"\xef\xbb\xbf"
ALVOS = {".py", ".yml", ".yaml", ".md", ".txt", ".conf", ".stream", ".exemplo", ".example", ""}

tirados = 0
for p in sorted(pathlib.Path(".").rglob("*")):
    if not p.is_file() or ".git" in p.parts or p.suffix not in ALVOS:
        continue
    raw = p.read_bytes()
    if raw.startswith(BOM):
        p.write_bytes(raw[len(BOM) :])
        print("BOM removido:", p)
        tirados += 1

print("arquivos corrigidos:", tirados)
sys.exit(0)
