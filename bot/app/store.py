"""Estado do bot em SQLite: quem e admin, chaves de acesso, overrides e auditoria.

Fica em /state/bot.db (volume ./state no host). Tudo que o bot precisa lembrar
entre reinicios mora aqui, e nada disso e segredo em texto puro: as chaves
sao guardadas como sha256, entao quem ler o .db nao consegue se logar.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_ADMIN, ROLE_VIEWER)

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id   INTEGER PRIMARY KEY,
    role      TEXT NOT NULL,
    username  TEXT,
    note      TEXT,
    created_at REAL NOT NULL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash  TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL,
    role        TEXT NOT NULL,
    created_by  INTEGER,
    created_at  REAL NOT NULL,
    bound_to    INTEGER,
    bound_name  TEXT,
    bound_at    REAL,
    last_used   REAL,
    used_count  INTEGER NOT NULL DEFAULT 0,
    revoked_at  REAL
);
CREATE TABLE IF NOT EXISTS overrides (
    k          TEXT PRIMARY KEY,
    v          TEXT NOT NULL,
    updated_by INTEGER,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS denied (
    name   TEXT PRIMARY KEY COLLATE NOCASE,
    reason TEXT,
    by     INTEGER,
    at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      REAL NOT NULL,
    user_id INTEGER,
    action  TEXT NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at DESC);
"""


def normaliza(token: str) -> str:
    """Compara chaves sem se preocupar com maiuscula, hifen e espaco.

    "rv-7f3k-92qx" e "RV7F3K92QX" sao a mesma chave: digitar no celular
    estraga os hifens.
    """
    return "".join(c for c in token.strip().upper() if c.isalnum())


def hash_token(token: str) -> str:
    return hashlib.sha256(normaliza(token).encode("utf-8")).hexdigest()


def gera_chave() -> str:
    bruto = secrets.token_urlsafe(12).replace("_", "A").replace("-", "B")
    bruto = "".join(c for c in bruto.upper() if c.isalnum())[:12]
    return f"{bruto[:4]}-{bruto[4:8]}-{bruto[8:12]}"


@dataclass(frozen=True)
class User:
    user_id: int
    role: str
    username: str | None
    note: str | None
    created_at: float
    last_seen: float | None

    @property
    def eh_admin(self) -> bool:
        return self.role == ROLE_ADMIN


@dataclass(frozen=True)
class Key:
    id: int
    label: str
    role: str
    created_by: int | None
    created_at: float
    bound_to: int | None
    bound_name: str | None
    bound_at: float | None
    last_used: float | None
    used_count: int
    revoked_at: float | None

    @property
    def ativa(self) -> bool:
        return self.revoked_at is None


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.commit()

    def fecha(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ usuarios

    def usuario(self, user_id: int) -> User | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return self._user(row) if row else None

    def _user(self, row: sqlite3.Row) -> User:
        return User(
            user_id=row["user_id"],
            role=row["role"],
            username=row["username"],
            note=row["note"],
            created_at=row["created_at"],
            last_seen=row["last_seen"],
        )

    def usuarios(self) -> list[User]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM users ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, created_at"
            ).fetchall()
        return [self._user(r) for r in rows]

    def admin(self) -> User | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        return self._user(row) if row else None

    def touch(self, user_id: int, username: str | None) -> None:
        with self._lock:
            self._db.execute("UPDATE users SET last_seen = ?, username = COALESCE(?, username) WHERE user_id = ?", (time.time(), username, user_id))
            self._db.commit()

    def _cria_usuario(self, user_id: int, role: str, username: str | None, note: str | None = None) -> User:
        agora = time.time()
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO users (user_id, role, username, note, created_at, last_seen) VALUES (?,?,?,?,?,?)",
                (user_id, role, username, note, agora, agora),
            )
            self._db.commit()
        return User(user_id=user_id, role=role, username=username, note=note, created_at=agora, last_seen=agora)

    def define_admin(self, user_id: int, username: str | None) -> User:
        """Promove a admin. So existe um: o anterior vira viewer na mesma hora."""
        with self._lock:
            self._db.execute(
                "UPDATE users SET role = ?, note = 'ex-admin' WHERE role = ? AND user_id != ?",
                (ROLE_VIEWER, ROLE_ADMIN, user_id),
            )
            self._db.commit()
        return self._cria_usuario(user_id, ROLE_ADMIN, username, note="conta do dono")

    def registra_viewer(self, user_id: int, username: str | None, note: str | None = None) -> User:
        """Da acesso de leitura. Nunca rebaixa quem ja e admin."""
        atual = self.usuario(user_id)
        if atual is not None and atual.role == ROLE_ADMIN:
            self.touch(user_id, username)
            return atual
        return self._cria_usuario(user_id, ROLE_VIEWER, username, note)

    def remove_usuario(self, user_id: int) -> bool:
        if self.admin() and self.admin().user_id == user_id:  # type: ignore[union-attr]
            raise ValueError("nao da para remover o proprio admin")
        with self._lock:
            cur = self._db.execute("DELETE FROM users WHERE user_id = ? AND role != 'admin'", (user_id,))
            self._db.execute("UPDATE keys SET revoked_at = ? WHERE bound_to = ? AND revoked_at IS NULL", (time.time(), user_id))
            self._db.commit()
        return cur.rowcount > 0

    # --------------------------------------------------------------------- chaves

    def cria_chave(self, role: str, label: str, created_by: int) -> tuple[Key, str]:
        if role not in ROLES:
            raise ValueError(f"papel invalido: {role}")
        for _ in range(8):
            token = gera_chave()
            digest = hash_token(token)
            with self._lock:
                existe = self._db.execute("SELECT 1 FROM keys WHERE token_hash = ?", (digest,)).fetchone()
                if not existe:
                    break
        else:  # pragma: no cover - 8 colisoes seguidas e impossivel
            raise RuntimeError("nao consegui gerar uma chave unica")

        agora = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO keys (token_hash, label, role, created_by, created_at) VALUES (?,?,?,?,?)",
                (digest, label, role, created_by, agora),
            )
            self._db.commit()
            key_id = int(cur.lastrowid or 0)
        return (
            Key(key_id, label, role, created_by, agora, None, None, None, None, 0, None),
            token,
        )

    def chaves(self, incluir_revogadas: bool = False) -> list[Key]:
        sql = "SELECT * FROM keys"
        if not incluir_revogadas:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._db.execute(sql).fetchall()
        return [self._key(r) for r in rows]

    def _key(self, row: sqlite3.Row) -> Key:
        return Key(
            id=row["id"],
            label=row["label"],
            role=row["role"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            bound_to=row["bound_to"],
            bound_name=row["bound_name"],
            bound_at=row["bound_at"],
            last_used=row["last_used"],
            used_count=row["used_count"],
            revoked_at=row["revoked_at"],
        )

    def chave_por_hash(self, digest: str) -> Key | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM keys WHERE token_hash = ?", (digest,)).fetchone()
        return self._key(row) if row else None

    def revoga_chave(self, key_id: int) -> Key | None:
        with self._lock:
            self._db.execute("UPDATE keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (time.time(), key_id))
            self._db.commit()
            row = self._db.execute("SELECT * FROM keys WHERE id = ?", (key_id,)).fetchone()
        return self._key(row) if row else None

    def registra_uso(self, key_id: int, user_id: int, username: str | None) -> None:
        agora = time.time()
        with self._lock:
            self._db.execute(
                "UPDATE keys SET bound_to = COALESCE(bound_to, ?), bound_name = COALESCE(bound_name, ?),"
                " bound_at = COALESCE(bound_at, ?), last_used = ?, used_count = used_count + 1 WHERE id = ?",
                (user_id, username, agora, agora, key_id),
            )
            self._db.commit()

    # ----------------------------------------------------------------- kv / config

    def kv_get(self, chave: str, padrao: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT v FROM kv WHERE k = ?", (chave,)).fetchone()
        return row["v"] if row else padrao

    def kv_set(self, chave: str, valor: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?,?)", (chave, valor))
            self._db.commit()

    # ----------------------------------------------------------------- overrides

    def overrides(self) -> dict[str, str]:
        with self._lock:
            rows = self._db.execute("SELECT k, v FROM overrides ORDER BY k").fetchall()
        return {r["k"]: r["v"] for r in rows}

    def set_override(self, chave: str, valor: str, by: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO overrides (k, v, updated_by, updated_at) VALUES (?,?,?,?)",
                (chave, valor, by, time.time()),
            )
            self._db.commit()

    def limpa_override(self, chave: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM overrides WHERE k = ?", (chave,))
            self._db.commit()

    # -------------------------------------------------------------------- negar

    def nega(self, nome: str, motivo: str | None, by: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO denied (name, reason, by, at) VALUES (?,?,?,?)",
                (nome, motivo, by, time.time()),
            )
            self._db.commit()

    def permite(self, nome: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM denied WHERE name = ?", (nome,))
            self._db.commit()

    def negados(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM denied ORDER BY at DESC").fetchall()
        return [dict(r) for r in rows]

    def esta_negado(self, nome: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM denied WHERE name = ?", (nome,)).fetchone()
        return row is not None

    # ---------------------------------------------------------------- auditoria

    def audita(self, user_id: int | None, acao: str, detalhe: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO audit (at, user_id, action, detail) VALUES (?,?,?,?)",
                (time.time(), user_id, acao, detalhe),
            )
            self._db.commit()

    def auditoria(self, limite: int = 30) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM audit ORDER BY at DESC LIMIT ?", (limite,)
            ).fetchall()
        return [dict(r) for r in rows]
