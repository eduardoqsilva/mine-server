"""Configuracao do bot, lida do .env (injetado pelo compose)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

# getFile da Bot API aceita no maximo 20 MiB exatos; acima disso o Telegram
# nem devolve os bytes (400 "file is too big").
TELEGRAM_DOWNLOAD_LIMIT = 20_971_520

_PLACEHOLDERS = ("", "changeme", "trocarsenha", "123456:ABCDEF")


class ConfigError(RuntimeError):
    pass


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} precisa ser inteiro, veio {raw!r}") from None


def _segredo(name: str, ajuda: str) -> str:
    raw = os.getenv(name, "").strip()
    if not raw or raw.lower() in _PLACEHOLDERS:
        raise ConfigError(f"{name} ausente ou ainda com o valor de exemplo. {ajuda}")
    if len(raw) < 8:
        raise ConfigError(f"{name} muito curto (minimo 8 caracteres)")
    return raw


@dataclass(frozen=True)
class DropboxConfig:
    """Credencial do Dropbox, lida do .env.

    Deliberadamente solta no dataclass Config: sao credenciais, nao
    configuracao de comportamento, e ficao num objeto so para o backup poder
    dizer "sem credencial do Dropbox" sem precisar checar quatro variaveis.
    """

    token: str = ""
    refresh_token: str = ""
    app_key: str = ""
    app_secret: str = ""

    @classmethod
    def from_env(cls) -> "DropboxConfig":
        return cls(
            token=os.getenv("DROPBOX_TOKEN", "").strip(),
            refresh_token=os.getenv("DROPBOX_REFRESH_TOKEN", "").strip(),
            app_key=os.getenv("DROPBOX_APP_KEY", "").strip(),
            app_secret=os.getenv("DROPBOX_APP_SECRET", "").strip(),
        )

    def resumo(self) -> str:
        """Como mostrar o estado sem vazar o segredo."""
        if self.refresh_token and self.app_key and self.app_secret:
            return "refresh token (renova sozinho)"
        if self.token:
            return "so token fixo (expira em ~4h, o backup semanal vai falhar)"
        return "nao configurado"


@dataclass(frozen=True)
class Config:
    token: str
    admin_claim_code: str
    state_dir: Path
    bds_container: str
    data_dir: Path
    bds_host: str
    bds_port: int
    level_name: str
    update_hour: int
    update_minute: int
    announce_seconds: int
    boot_timeout: int
    backup_keep: int
    max_upload_bytes: int
    tz: ZoneInfo
    staging_dir: Path
    icon_file: Path
    backups_dir: Path
    backup_local_keep: int
    backup_dropbox_keep: int
    backup_day: int
    backup_hour: int
    backup_minute: int
    dropbox: DropboxConfig

    @classmethod
    def from_env(cls) -> Config:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token or token in _PLACEHOLDERS:
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN ausente ou ainda com o valor de exemplo. "
                "cp .env.example .env e cole o token do @BotFather"
            )
        data_dir = Path(os.getenv("BDS_DATA_DIR", "/data"))
        container = os.getenv("BDS_CONTAINER", "mine-bedrock")
        return cls(
            token=token,
            # Unica forma de virar admin: mandar /start <codigo>. Sem isso o
            # primeiro usuario a falar com o bot seria admin, e qualquer um
            # acharia o bot e ficaria com o docker. Guarde este codigo como
            # senha: quem mandar assume o admin e rebaixa o anterior.
            admin_claim_code=_segredo(
                "TELEGRAM_ADMIN_CLAIM_CODE",
                "Copie TELEGRAM_ADMIN_CLAIM_CODE para o .env (ex.: RV-9f3a-...) e mande "
                "/start esse-codigo no Telegram. Guarde: e a chave mestra do bot.",
            ),
            state_dir=Path(os.getenv("STATE_DIR", "/state")),
            bds_container=container,
            data_dir=data_dir,
            # o bot roda em outro container: quem responde e o bds pelo nome DNS
            # dele na rede do compose. So sobrescreva para apontar para outro host.
            bds_host=os.getenv("BDS_HOST", "").strip() or container,
            bds_port=_int("BDS_PORT", 19132),
            # precisa bater com o LEVEL_NAME do container do jogo: e assim que
            # a gente acha a pasta do mundo em /data/worlds/<level_name>.
            level_name=os.getenv("LEVEL_NAME", "world").strip() or "world",
            update_hour=_int("UPDATE_HOUR", 5),
            update_minute=_int("UPDATE_MINUTE", 0),
            announce_seconds=_int("ANNOUNCE_SECONDS", 20),
            boot_timeout=_int("BOOT_TIMEOUT", 240),
            backup_keep=_int("BACKUP_KEEP", 3),
            max_upload_bytes=_int("MAX_UPLOAD_BYTES", TELEGRAM_DOWNLOAD_LIMIT),
            tz=ZoneInfo(os.getenv("TZ", "America/Sao_Paulo")),
            staging_dir=Path(os.getenv("STAGING_DIR", "/tmp/mcbot")),
            # O Bedrock ignora server-icon.png (recursos do Java edition), entao
            # o icone vive aqui para o bot mandar no /status do Telegram.
            icon_file=Path(os.getenv("ICON_FILE", "/app/assets/server-icon.png")),
            backups_dir=Path(os.getenv("BACKUPS_DIR", "/state/backups")),
            # 5 no disco (virada rapida se o Dropbox estiver fora) e 3 na nuvem
            # (e o espaco apertado la). Os dois sao apagados por rotacao, nunca
            # por data.
            backup_local_keep=_int("BACKUP_LOCAL_KEEP", 5),
            backup_dropbox_keep=_int("BACKUP_DROPBOX_KEEP", 3),
            # Sabado as 04:00: uma hora antes do restart das 05:00 que busca a
            # versao nova, para os dois nao cairem no mesmo dia colados.
            backup_day=_int("BACKUP_DAY", 5),
            backup_hour=_int("BACKUP_HOUR", 4),
            backup_minute=_int("BACKUP_MINUTE", 0),
            dropbox=DropboxConfig.from_env(),
        )
