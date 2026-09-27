"""Instalacao e atualizacao de add-ons (behavior/resource packs) do BDS.

O BDS nao tem comando de console para ativar pack: o pack so entra no jogo se a
pasta existir em behavior_packs/ ou resource_packs/ E estiver referenciado no
world_behavior_packs.json / world_resource_packs.json do mundo. Este modulo
faz as duas coisas, com backup da versao anterior.

Layouts aceitos no arquivo enviado (todos sao .zip renomeado):
  .mcaddon          -> data/ (behavior) + resources/ (resource) na raiz
  .mcpack           -> um pack, com manifest.json na raiz
  zip do servidor   -> behavior_packs/ + resource_packs/
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("bds.addons")

BEHAVIOR = "behavior"
RESOURCE = "resource"

PACK_DIRS = {BEHAVIOR: "behavior_packs", RESOURCE: "resource_packs"}
WORLD_FILES = {BEHAVIOR: "world_behavior_packs.json", RESOURCE: "world_resource_packs.json"}

# resource pack vanilla do jogo: instalar isso por cima quebra o servidor
VANILLA_RESOURCE_UUID = "66c6e9a8-3093-462a-9c36-dbb052165822"

MAX_UNCOMPRESSED = 400 * 1024 * 1024
MAX_MEMBERS = 20_000
IGNORED_PARTS = ("__MACOSX", ".git", ".github")


class AddonError(RuntimeError):
    pass


@dataclass(frozen=True)
class Pack:
    uuid: str
    name: str
    version: tuple[int, int, int]
    kind: str
    min_engine: tuple[int, ...] | None
    root: Path

    @property
    def version_str(self) -> str:
        return ".".join(str(n) for n in self.version)


@dataclass(frozen=True)
class InstalledPack:
    uuid: str
    name: str
    version: tuple[int, int, int]
    kind: str
    folder: Path
    description: str = ""
    # True quando o nome/descricao vieram dos arquivos de idioma do pack, e
    # nao da chave crua do manifest. E o que prova que o pack nao e de fabrica.
    traduzido: bool = False

    @property
    def version_str(self) -> str:
        return ".".join(str(n) for n in self.version)

    @property
    def interno(self) -> bool:
        """True se o pack e um dos que o BDS ja traz de fabrica.

        O BDS deixa dozens de packs em behavior_packs/ e resource_packs/
        (vanilla de cada versao, chemistry, editor, as libraries do
        @minecraft/server) e o proprio servidor os carrega: eles nao passam pelo
        world_*.packs.json. Sem separa-los, a listagem de add-ons do admin vira
        uma parede de 70 linhas de packs que ele nao instalou.
        """
        return _e_interno(self.name, self.description, self.traduzido, self.folder.name)


# Chave de traducao crua do manifest ("pack.name", "resourcePack.vanilla_server.
# description"): identificador com ponto e sem espaco. O manifest quase sempre
# traz isso no lugar do nome de verdade; o texto que o jogador ve esta nos
# arquivos de idioma do proprio pack. Os internos do BDS nao tem esses
# arquivos, entao a chave deles nunca resolve - e ai sim a chave e a melhor
# pista de que o pack e de fabrica.
_CHAVE_TRADUCAO = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")

# Ordem de preferencia do idioma do texto de exibicao. pt_BR primeiro porque
# e o servidor daqui; en_US e o fallback padrao de quase todo pack.
_IDIOMAS = ("pt_BR", "en_US", "en_GB", "es_MX", "es_ES", "fr_FR", "de_DE")


def _codigos_declarados(root: Path) -> list[str]:
    """Os codigos que texts/languages.json declara, se o pack declarar."""
    try:
        data = json.loads((root / "texts" / "languages.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [str(c) for c in data if isinstance(c, str)]
    if isinstance(data, dict):
        return [str(c) for c in data]
    return []


def _le_lang(caminho: Path) -> dict[str, str]:
    """Le o formato antigo do Mojang: uma linha "chave=valor"."""
    achados: dict[str, str] = {}
    try:
        texto = caminho.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return achados
    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        achados[chave.strip()] = valor.strip()
    return achados


def _traduz(root: Path, chave: str) -> str:
    """Resolve uma chave de traducao do pack. Devolve "" se nao houver texto.

    Isso e o que separa um pack de verdade de um interno do BDS: os internos
    (vanilla, chemistry, as libraries) nao trazem pasta texts/, entao a chave
    fica sem traducao. Um pack de terceiros sempre traz, senao o jogador veria
    "pack.name" no menu de add-ons dele tambem.
    """
    if not _CHAVE_TRADUCAO.match(chave or ""):
        return ""
    textos = root / "texts"
    if not textos.is_dir():
        return ""
    idiomas = [c for c in _IDIOMAS if c not in _codigos_declarados(root)] + _codigos_declarados(root)
    for idioma in idiomas:
        # moderno: texts/languages/<codigo>.json, {"chave": "texto"}
        alvo = textos / "languages" / f"{idioma}.json"
        try:
            dados = json.loads(alvo.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            dados = None
        if isinstance(dados, dict):
            valor = dados.get(chave)
            if isinstance(valor, str) and valor.strip():
                return valor.strip()
        # legado: texts/<codigo>.lang
        valor = _le_lang(textos / f"{idioma}.lang").get(chave, "")
        if valor:
            return valor
    return ""


def _nome_exibicao(root: Path, header: dict) -> tuple[str, str, bool]:
    """(nome, descricao, traduzido) de um manifest, resolvendo as chaves.

    O terceiro valor diz se o texto veio mesmo do pack. E o sinal confiavel de
    que o pack nao e de fabrica: os internos do BDS usam chave de traducao sem
    nenhum arquivo de idioma junto, enquanto um pack de terceiros sempre traz o
    proprio texto. Sem isso, "pack.name" era lido como nome e o pack sumia da
    listagem junto com os 70+ internos que o BDS larga nas pastas.
    """
    bruto_nome = str(header.get("name", "") or "").strip()
    bruto_desc = str(header.get("description", "") or "").strip()
    nome = _traduz(root, bruto_nome) or bruto_nome or root.name
    descricao = _traduz(root, bruto_desc) or bruto_desc
    # Traduzido so conta se o texto do nome veio do pack: um interno pode ter
    # descricao traduzida e continuar sendo interno.
    traduzido = bool(_traduz(root, bruto_nome))
    return nome, descricao, traduzido


# Packs que o BDS larga em behavior_packs/ e resource_packs/ no boot. A
# chemical e o editor tambem trazem texts/ com nome traduzido de verdade, entao
# "tem arquivo de idioma" nao separa pack de fabrica de pack de terceiro. O que
# separa e o NOME DA PASTA, que o Mojang padroniza e o bot nunca usa: um pack
# instalado por ele vira <slug>_<8 chars do uuid>.
_BASES_INTERNAS = frozenset({"vanilla", "vanilla_base", "chemistry", "editor"})
_PREFIXOS_INTERNOS = ("experimental_",)


def _pasta_interna(nome_pasta: str) -> bool:
    """True se a pasta tem cara de pack de fabrica do BDS."""
    base = re.sub(r"_\d+(?:\.\d+)*$", "", nome_pasta or "")
    if base in _BASES_INTERNAS:
        return True
    if any(base.startswith(p) for p in _PREFIXOS_INTERNOS):
        return True
    # server_library, server_ui_library, server_editor_library
    return base.endswith("_library")


# Alguns internos do BDS trazem nome em ingles de verdade, e nao chave. Sao
# poucos e o nome e estavel entre versoes.
_NOMES_INTERNOS = ("Library", "Experimental", "Vanilla Voxel", "Villager Trade")


def _e_interno(nome: str, descricao: str, traduzido: bool = False, pasta: str = "") -> bool:
    # Regra mais forte primeiro: a pasta nao mente.
    if pasta and _pasta_interna(pasta):
        return True
    nome = nome or ""
    descricao = descricao or ""
    # Texto resolvido do proprio pack = prova de que nao e de fabrica.
    if traduzido:
        return False
    if _CHAVE_TRADUCAO.match(nome) or _CHAVE_TRADUCAO.match(descricao):
        return True
    if nome.startswith("@minecraft/"):
        return True
    return any(trecho in nome for trecho in _NOMES_INTERNOS)


@dataclass
class ApplyResult:
    name: str
    uuid: str
    kind: str
    old_version: tuple[int, int, int] | None
    new_version: tuple[int, int, int]
    folder: str
    world_entry: str
    backups_removed: int


# --------------------------------------------------------------------------- utils


def _version_key(text: str) -> tuple[int, ...]:
    try:
        return _normalize_version(text)
    except AddonError:
        return (0, 0, 0)


def _normalize_version(value: object) -> tuple[int, int, int]:
    if isinstance(value, str):
        parts: list[object] = value.replace("-", ".").split(".")
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        raise AddonError(f"versao invalida: {value!r}")
    nums: list[int] = []
    for part in parts[:3]:
        match = re.match(r"\d+", str(part))
        if not match:
            raise AddonError(f"versao invalida: {value!r}")
        nums.append(int(match.group()))
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def _slug(name: str, limit: int = 40) -> str:
    flat = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    flat = re.sub(r"[^A-Za-z0-9._-]+", "_", flat).strip("_.")
    return (flat or "pack")[:limit].rstrip("_.")


def _load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AddonError(f"manifest.json ilegivel em {path.parent.name}: {exc}") from None
    if not isinstance(data, dict):
        raise AddonError(f"manifest.json invalido em {path.parent.name}")
    return data


def _kind_of(manifest: dict, root: Path) -> str:
    types = {str(m.get("type", "")).lower() for m in manifest.get("modules", []) if isinstance(m, dict)}
    if types & {"data", "script"}:
        return BEHAVIOR
    if "resources" in types:
        return RESOURCE
    hint = "/".join(root.parts).lower()
    if "resource" in hint:
        return RESOURCE
    if "behavior" in hint or "behaviour" in hint:
        return BEHAVIOR
    raise AddonError(
        f"nao consegui identificar se '{root.name}' e behavior ou resource pack "
        "(manifest sem modules e pasta sem nome util)"
    )


def _read_json(path: Path, default):
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AddonError(f"{path.name} esta com JSON invalido: {exc}") from None


def _write_json(path: Path, payload) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------- mundo / extração


def world_dir(data_dir: Path, fallback_level: str) -> Path:
    """Descobre o nome do mundo lendo level-name do server.properties."""
    level = fallback_level
    props = data_dir / "server.properties"
    if props.is_file():
        for line in props.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("level-name="):
                level = line.split("=", 1)[1].strip() or level
                break
    world = data_dir / "worlds" / level
    if not world.is_dir():
        raise AddonError(f"mundo {level!r} nao encontrado em {data_dir / 'worlds'}")
    return world


def extract_archive(archive: Path, dest: Path) -> None:
    """Descompacta com guarda contra zip-slip, symlink e zip bomb."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise AddonError(f"arquivo com {len(infos)} arquivos, demais")
        total = 0
        for info in infos:
            name = info.filename
            if any(part in IGNORED_PARTS for part in Path(name).parts):
                continue
            total += info.file_size
            if total > MAX_UNCOMPRESSED:
                raise AddonError("conteudo descomprimido acima do limite (400 MB)")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise AddonError("o arquivo contem symlink, recusado")
            target = (root / name).resolve()
            if target != root and not target.is_relative_to(root):
                raise AddonError(f"caminho perigoso bloqueado: {name}")
        zf.extractall(root)


def detect_packs(extracted: Path) -> list[Pack]:
    """Acha todos os manifest.json e monta um Pack por pack (ignora os aninhados)."""
    manifests = sorted(extracted.rglob("manifest.json"), key=lambda p: (len(p.parts), str(p)))
    packs: list[Pack] = []
    for path in manifests:
        root = path.parent
        if any(root.is_relative_to(found.root) for found in packs):
            continue
        manifest = _load_manifest(path)
        header = manifest.get("header") or {}
        uuid = str(header.get("uuid", "")).strip().lower()
        if not uuid:
            log.warning("pack %s sem header.uuid, ignorado", root.name)
            continue
        if uuid == VANILLA_RESOURCE_UUID:
            raise AddonError("esse e o resource pack vanilla do jogo, nao da para instalar")
        try:
            version = _normalize_version(header.get("version", [0, 0, 0]))
        except AddonError:
            log.warning("pack %s com versao ilegivel, assumindo 0.0.0", root.name)
            version = (0, 0, 0)
        min_engine = None
        raw_min = header.get("min_engine_version")
        if raw_min:
            try:
                min_engine = _normalize_version(raw_min)
            except AddonError:
                min_engine = None
        nome, _desc, _trad = _nome_exibicao(root, header)
        packs.append(
            Pack(
                uuid=uuid,
                name=nome,
                version=version,
                kind=_kind_of(manifest, root),
                min_engine=min_engine,
                root=root,
            )
        )
    if not packs:
        raise AddonError("nenhum manifest.json valido dentro do arquivo")
    return packs


# --------------------------------------------------------------------- estado atual


def installed_packs(data_dir: Path) -> dict[str, InstalledPack]:
    found: dict[str, InstalledPack] = {}
    for kind, dirname in PACK_DIRS.items():
        base = data_dir / dirname
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            mpath = folder / "manifest.json"
            if not folder.is_dir() or not mpath.is_file():
                continue
            try:
                manifest = _load_manifest(mpath)
                header = manifest.get("header") or {}
                uuid = str(header.get("uuid", "")).strip().lower()
                if not uuid:
                    continue
                nome, descricao, traduzido = _nome_exibicao(folder, header)
                found[uuid] = InstalledPack(
                    uuid=uuid,
                    name=nome,
                    version=_normalize_version(header.get("version", [0, 0, 0])),
                    kind=kind,
                    folder=folder,
                    description=descricao,
                    traduzido=traduzido,
                )
            except AddonError as exc:
                log.warning("pack em %s ignorado: %s", folder.name, exc)
    return found


def packs_do_mundo(world: Path) -> dict[str, dict[str, str]]:
    """Packs que o mundo referencia de verdade: {tipo: {uuid: versao}}.

    Estar instalado na pasta nao basta: o BDS so ativa o pack que tem entrada
    no world_behavior_packs.json / world_resource_packs.json do mundo. Um pack
    sem entrada fica no disco sem efeito nenhum no jogo, e e exatamente o
    caso que a listagem precisa denunciar em vez de so contar arquivos.
    """
    ativos: dict[str, dict[str, str]] = {}
    for kind, fname in WORLD_FILES.items():
        entries = _read_json(world / fname, [])
        achados: dict[str, str] = {}
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                uuid = str(entry.get("pack_id", "")).strip().lower()
                if not uuid:
                    continue
                versao = entry.get("version")
                if isinstance(versao, list):
                    achados[uuid] = ".".join(str(n) for n in versao)
                else:
                    achados[uuid] = str(versao or "?")
        ativos[kind] = achados
    return ativos


# --------------------------------------------------------------------- aplicacao


def _backup(data_dir: Path, existing: InstalledPack, keep: int) -> int:
    root = data_dir / ".addon-backups" / existing.uuid
    root.mkdir(parents=True, exist_ok=True)
    dest = root / existing.version_str
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(existing.folder), str(dest))
    versions = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: _version_key(p.name))
    removed = 0
    while len(versions) > max(keep, 1):
        shutil.rmtree(versions.pop(0))
        removed += 1
    return removed


def _upsert_world_entry(world: Path, pack: Pack) -> str:
    path = world / WORLD_FILES[pack.kind]
    entries = _read_json(path, [])
    if not isinstance(entries, list):
        raise AddonError(f"{path.name} nao e uma lista JSON")
    new_version = list(pack.version)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("pack_id", "")).strip().lower() == pack.uuid:
            changed = entry.get("version") != new_version
            entry["version"] = new_version
            _write_json(path, entries)
            return "atualizado" if changed else "mantido"
    entries.append({"pack_id": pack.uuid, "version": new_version, "priority": len(entries)})
    _write_json(path, entries)
    return "novo"


def _upsert_valid_known(data_dir: Path, pack: Pack, rel_path: str) -> None:
    path = data_dir / "valid_known_packs.json"
    if not path.is_file():
        return
    entries = _read_json(path, [])
    if not isinstance(entries, list):
        return
    kept = [e for e in entries if not (isinstance(e, dict) and str(e.get("uuid", "")).lower() == pack.uuid)]
    kept.append({"path": rel_path, "uuid": pack.uuid, "version": pack.version_str})
    _write_json(path, kept)


def _drop_world_entry(world: Path, pack_id: str) -> None:
    for kind, fname in WORLD_FILES.items():
        path = world / fname
        if not path.is_file():
            continue
        entries = _read_json(path, [])
        if not isinstance(entries, list):
            continue
        kept = [
            e
            for e in entries
            if not (isinstance(e, dict) and str(e.get("pack_id", "")).strip().lower() == pack_id)
        ]
        if len(kept) != len(entries):
            _write_json(path, kept)
            log.info("entrada %s removida de %s", pack_id[:8], fname)


def apply(
    pack: Pack,
    data_dir: Path,
    world: Path,
    mode: str,
    backup_keep: int = 3,
) -> ApplyResult:
    if mode not in ("new", "update"):
        raise AddonError(f"modo invalido: {mode}")

    current = installed_packs(data_dir)
    existing = current.get(pack.uuid)
    if mode == "update" and existing is None:
        raise AddonError(f"'{pack.name}' nao esta instalado neste mundo")
    if mode == "new" and existing is not None:
        raise AddonError(f"'{pack.name}' ja esta instalado (v{existing.version_str}), use Atualizar")

    base = data_dir / PACK_DIRS[pack.kind]
    base.mkdir(parents=True, exist_ok=True)
    folder = existing.folder if existing else base / f"{_slug(pack.name)}_{pack.uuid[:8]}"
    if existing is not None and existing.kind != pack.kind:
        raise AddonError(
            f"'{pack.name}' estava instalado como {existing.kind} e veio como {pack.kind}; "
            "remova com /remover antes"
        )

    pruned = 0
    if existing is not None:
        pruned = _backup(data_dir, existing, backup_keep)
    if folder.exists():
        shutil.rmtree(folder)
    shutil.copytree(pack.root, folder)

    action = _upsert_world_entry(world, pack)
    _upsert_valid_known(data_dir, pack, f"{PACK_DIRS[pack.kind]}/{folder.name}")

    return ApplyResult(
        name=pack.name,
        uuid=pack.uuid,
        kind=pack.kind,
        old_version=existing.version if existing else None,
        new_version=pack.version,
        folder=folder.name,
        world_entry=action,
        backups_removed=pruned,
    )


def remove(uuid: str, data_dir: Path, world: Path) -> InstalledPack:
    current = installed_packs(data_dir)
    existing = current.get(uuid)
    if existing is None:
        raise AddonError("pack nao encontrado")
    _drop_world_entry(world, uuid)
    path = data_dir / "valid_known_packs.json"
    if path.is_file():
        entries = _read_json(path, [])
        if isinstance(entries, list):
            _write_json(
                path,
                [
                    e
                    for e in entries
                    if not (isinstance(e, dict) and str(e.get("uuid", "")).lower() == uuid)
                ],
            )
    if existing.folder.is_dir():
        shutil.rmtree(existing.folder)
    return existing


def engine_warning(min_engine: tuple[int, ...] | None, server_version: str) -> str:
    """Compara min_engine_version do pack com a versao rodando (best effort)."""
    if not min_engine or not server_version or server_version == "?":
        return ""
    running = _version_key(server_version)[:3]
    required = tuple(min_engine)[:3]
    if required > running:
        return (
            f"este pack pede Minecraft {'.'.join(str(n) for n in required)}+ "
            f"e o servidor esta em {server_version}"
        )
    return ""
