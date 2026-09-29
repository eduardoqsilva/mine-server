"""Testes da logica pura: sem Telegram, sem Docker.

Roda em qualquer maquina:  python tools/selftest.py
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

if "aiogram" not in sys.modules:
    try:  # aiogram so existe no container; aqui a gente testa logica pura
        import aiogram  # noqa: F401
    except ImportError:
        import types

        falso = types.ModuleType("aiogram")

        class BaseMiddleware:  # o suficiente para os middlewares herdarem
            def __init__(self) -> None:
                pass

        class Bot:  # so a assinatura, auth.py nunca instancia no teste
            pass

        class _Observador:
            """Cuida de .middleware(...) e de ser usado como @router.message(...).

            O Router do aiogram e' as duas coisas: no admin.py ele recebe
            middlewares e tambem decora os handlers. Um duble so de assinatura
            passaria no import e estouraria no uso, entao os dois caminhos
            ficam cobertos aqui.
            """

            def __init__(self, nome: str = "") -> None:
                self.nome = nome
                self.handlers: list[object] = []
                self.middlewares: list[object] = []

            def middleware(self, mw: object) -> object:
                self.middlewares.append(mw)
                return mw

            def __call__(self, *args: object, **kwargs: object) -> object:
                def deco(func: object) -> object:
                    self.handlers.append(func)
                    return func

                return deco

        class Router(_Observador):
            def __init__(self, name: str = "") -> None:
                super().__init__(name)
                self.message = _Observador("message")
                self.callback_query = _Observador("callback_query")

        class _Filtro:
            """F.data.startswith(...) e' o unico uso de F no admin."""

            class data:
                @staticmethod
                def startswith(prefixo: str) -> tuple[str, str]:
                    return ("data", prefixo)

        falso.BaseMiddleware = BaseMiddleware
        falso.Bot = Bot
        falso.Router = Router
        falso.F = _Filtro
        falso.__path__ = []  # permite "import aiogram.types"

        tipos = types.ModuleType("aiogram.types")

        class Message:  # usado no isinstance do AdminOnly
            pass

        class CallbackQuery:
            pass

        class InlineKeyboardButton:
            def __init__(self, text: str = "", callback_data: str = "") -> None:
                self.text = text
                self.callback_data = callback_data

        class InlineKeyboardMarkup:
            def __init__(self, inline_keyboard: object = None) -> None:
                self.inline_keyboard = inline_keyboard

        tipos.Message = Message
        tipos.CallbackQuery = CallbackQuery
        tipos.InlineKeyboardButton = InlineKeyboardButton
        tipos.InlineKeyboardMarkup = InlineKeyboardMarkup
        falso.types = tipos

        filtros = types.ModuleType("aiogram.filters")

        class Command:  # so a assinatura: o filtro nunca roda no teste
            def __init__(self, *nomes: str) -> None:
                self.nomes = nomes

        class CommandObject:
            args: str | None = None

        filtros.Command = Command
        filtros.CommandObject = CommandObject
        falso.filters = filtros
        sys.modules["aiogram"] = falso
        sys.modules["aiogram.types"] = tipos
        sys.modules["aiogram.filters"] = filtros

if "docker" not in sys.modules:
    try:  # docker so existe no container; aqui a gente testa logica pura
        import docker  # noqa: F401
    except ImportError:
        import types

        falso = types.ModuleType("docker")

        class DockerException(Exception):
            pass

        class APIError(DockerException):
            pass

        class NotFound(DockerException):
            pass

        erros = types.ModuleType("docker.errors")
        erros.DockerException = DockerException
        erros.APIError = APIError
        erros.NotFound = NotFound
        falso.errors = erros
        falso.__path__ = []
        sys.modules["docker"] = falso
        sys.modules["docker.errors"] = erros

from app import addons  # noqa: E402
from app import admin  # noqa: E402
from app import auth  # noqa: E402
from app import backup  # noqa: E402
from app import docker_ctl  # noqa: E402
from app import dropbox  # noqa: E402
from app import ops  # noqa: E402
from app import raknet  # noqa: E402
from app import serverctl  # noqa: E402
from app import store  # noqa: E402

falhas: list[str] = []


def check(nome: str, cond: bool, detalhe: str = "") -> None:
    if cond:
        print(f"  ok   {nome}")
    else:
        print(f"  FAIL {nome} {detalhe}")
        falhas.append(nome)


def manifest(uuid: str, name: str, version: list[int], tipo: str, min_engine: list[int] | None = None) -> dict:
    header = {"name": name, "uuid": uuid, "version": version}
    if min_engine:
        header["min_engine_version"] = min_engine
    return {
        "format_version": 2,
        "header": header,
        "modules": [{"description": name, "type": tipo, "uuid": "11111111-2222-3333-4444-555555555555", "version": version}],
    }


def zipar(destino: Path, arvore: dict[str, object]) -> Path:
    """arvore: nome -> conteudo. dict vira JSON de arquivo, str vira texto."""
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as zf:
        for nome, conteudo in arvore.items():
            if isinstance(conteudo, dict):
                zf.writestr(nome, json.dumps(conteudo))
            else:
                zf.writestr(nome, str(conteudo))
    return destino


def test_detect_mcaddon() -> None:
    print("detect .mcaddon (data/ + resources/)")
    tmp = Path(tempfile.mkdtemp())
    try:
        arc = zipar(
            tmp / "addon.mcaddon",
            {
                "data/manifest.json": manifest("aaaa1111-0000-0000-0000-000000000001", "Revolução Pack", [1, 0, 0], "data", [1, 21, 0]),
                "data/entities/p.json": "{}",
                "data/entities/nested/deep.json": "{}",
                "resources/manifest.json": manifest("aaaa1111-0000-0000-0000-000000000002", "Revolução Texturas", [1, 0, 0], "resources"),
                "resources/textures/x.png": "png",
            },
        )
        addons.extract_archive(arc, tmp / "x")
        packs = addons.detect_packs(tmp / "x")
        check("achou 2 packs", len(packs) == 2, [p.name for p in packs])
        por_tipo = {p.kind: p for p in packs}
        check("behavior detectado", "behavior" in por_tipo)
        check("resource detectado", "resource" in por_tipo)
        check("nome com acento preservado", por_tipo["behavior"].name == "Revolução Pack")
        check("versao lida", por_tipo["behavior"].version == (1, 0, 0))
        check("min_engine lido", por_tipo["behavior"].min_engine == (1, 21, 0))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_nome_de_traducao() -> None:
    """Pack de terceiro com manifest usando chave crua (pack.name).

    Regressao real: o manifest do Actions & Stuff traz "name": "pack.name", e o
    codigo lia isso como se fosse o nome. O pack sumia da listagem, porque
    _e_interno() tratava qualquer chave como pack de fabrica.
    """
    print("nome de exibicao vem do arquivo de idioma, nao da chave")
    tmp = Path(tempfile.mkdtemp())
    try:
        # formato legado: texts/en_US.lang com chave=valor
        legado = tmp / "legado"
        (legado / "texts").mkdir(parents=True)
        # o pack real traz nome E descricao como chave, so que o manifest
        # precisa declarar as duas para a traducao pegá-las.
        head = manifest("cccc3333-0000-0000-0000-000000000001", "pack.name", [1, 1, 24], "resources")["header"]
        head["description"] = "pack.description"
        (legado / "manifest.json").write_text(json.dumps({"header": head}), encoding="utf-8")
        (legado / "texts" / "languages.json").write_text('["en_US"]', encoding="utf-8")
        (legado / "texts" / "en_US.lang").write_text(
            "pack.name=Actions & Stuff 1.9\npack.description=By Oreville Studios\n", encoding="utf-8"
        )
        nome, desc, trad = addons._nome_exibicao(legado, head)
        check("nome veio do .lang", nome == "Actions & Stuff 1.9", nome)
        check("descricao veio do .lang", desc == "By Oreville Studios", desc)
        check("marcado como traduzido", trad is True)
        check("nao e interno", not addons._e_interno(nome, desc, trad))
        # a pasta real do pack instalado, com o slug da chave crua
        check(
            "pasta pack.name_... nao vira interna",
            not addons._e_interno(nome, desc, trad, "pack.name_2cf066eb"),
        )
        # o slug da pasta na installacao vem do nome JA resolvido: era o que
        # gerava a pasta "pack.name_2cf066eb" no /data/resource_packs.
        check(
            "pasta instalada usa o nome resolvido",
            addons._slug(nome) == "Actions_Stuff_1.9",
            addons._slug(nome),
        )

        # formato moderno: texts/languages/en_US.json
        moderno = tmp / "moderno"
        (moderno / "texts" / "languages").mkdir(parents=True)
        (moderno / "manifest.json").write_text(
            json.dumps(manifest("cccc3333-0000-0000-0000-000000000002", "meu.pack.nome", [1, 0, 0], "data")),
            encoding="utf-8",
        )
        (moderno / "texts" / "languages" / "en_US.json").write_text(
            json.dumps({"meu.pack.nome": "Meu Pack Brasil"}), encoding="utf-8"
        )
        nome2, _d2, trad2 = addons._nome_exibicao(moderno, json.loads((moderno / "manifest.json").read_text())["header"])
        check("nome veio do languages/*.json", nome2 == "Meu Pack Brasil", nome2)
        check("marcado como traduzido (json)", trad2 is True)

        # pack do BDS: chave crua e SEM pasta texts/ -> interno, como antes
        interno = tmp / "interno"
        interno.mkdir()
        (interno / "manifest.json").write_text(
            json.dumps(manifest("dddd4444-0000-0000-0000-000000000001", "resourcePack.vanilla.name", [1, 21, 80], "resources")),
            encoding="utf-8",
        )
        nome3, _d3, trad3 = addons._nome_exibicao(interno, json.loads((interno / "manifest.json").read_text())["header"])
        check("interno continua sem traducao", trad3 is False)
        check("interno continua interno", addons._e_interno(nome3, "", trad3))

        # nome que ja vem em ingles (nao e chave) nao passa por arquivo nenhum
        direto = tmp / "direto"
        direto.mkdir()
        (direto / "manifest.json").write_text(
            json.dumps(manifest("eeee5555-0000-0000-0000-000000000001", "Meu Pack Direto", [1, 0, 0], "data")),
            encoding="utf-8",
        )
        nome4, _d4, trad4 = addons._nome_exibicao(direto, json.loads((direto / "manifest.json").read_text())["header"])
        check("nome literal preservado", nome4 == "Meu Pack Direto", nome4)
        check("literal nao marcado como traduzido", trad4 is False)

        # .lang quebrado nao pode derrubar a listagem
        quebrado = tmp / "quebrado"
        (quebrado / "texts").mkdir(parents=True)
        (quebrado / "manifest.json").write_text(
            json.dumps(manifest("ffff6666-0000-0000-0000-000000000001", "pack.name", [1, 0, 0], "resources")),
            encoding="utf-8",
        )
        (quebrado / "texts" / "en_US.lang").write_bytes(b"\xff\xfe\x00\x00binario")
        nome5, _d5, trad5 = addons._nome_exibicao(quebrado, json.loads((quebrado / "manifest.json").read_text())["header"])
        check("lang invalido nao quebra", nome5 == "pack.name", nome5)

        # Pastas de fabrica do BDS. Chemistry e editor TAMBEM trazem texts com
        # nome traduzido, entao so o nome da pasta separa eles de pack de
        # terceiro - foi o bug que apareceu depois do fix do nome.
        for pasta in (
            "vanilla",
            "vanilla_base",
            "vanilla_1.21.80",
            "vanilla_1.14",
            "chemistry",
            "chemistry_1.20.50",
            "editor",
            "experimental_poi",
            "experimental_villager_trade",
            "server_library",
            "server_ui_library",
            "server_editor_library",
        ):
            check(f"pasta interna: {pasta}", addons._pasta_interna(pasta))
        for pasta in ("pack.name_2cf066eb", "Meu_Pack_1.0.0_deadbeef", "texturas", "minha_textura"):
            check(f"pasta de terceiro: {pasta}", not addons._pasta_interna(pasta))
        # nome traduzido + pasta de fabrica = interno (a pasta vence)
        check(
            "pasta de fabrica vence nome traduzado",
            addons._e_interno("Chemistry", "Explore o mundo da quimica!", True, "chemistry"),
        )
        check(
            "editor interno mesmo traduzido",
            addons._e_interno("Recursos do Editor", "Recursos de Cliente", True, "editor"),
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_zip_slip() -> None:
    print("seguranca do zip")
    tmp = Path(tempfile.mkdtemp())
    try:
        arc = tmp / "evil.zip"
        with zipfile.ZipFile(arc, "w") as zf:
            zf.writestr("../../escapou/manifest.json", "{}")
        try:
            addons.extract_archive(arc, tmp / "x")
            check("zip-slip bloqueado", False, "deveria ter levantado erro")
        except addons.AddonError as exc:
            check("zip-slip bloqueado", "perigoso" in str(exc), str(exc))
        check("nada escapou do tmp", not (tmp.parent / "escapou").exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_apply_update() -> None:
    print("instalar, atualizar e remover")
    tmp = Path(tempfile.mkdtemp())
    try:
        data = tmp / "data"
        (data / "worlds" / "Revolucao").mkdir(parents=True)
        (data / "worlds" / "Revolucao" / "level.dat").write_bytes(b"x")
        (data / "server.properties").write_text("server-port=19132\nlevel-name=Revolucao\n", encoding="utf-8")
        (data / "valid_known_packs.json").write_text("[]", encoding="utf-8")

        v1 = tmp / "v1"
        v1.mkdir()
        addons.extract_archive(
            zipar(
                v1 / "a.mcpack",
                {
                    "manifest.json": manifest("bbbb2222-0000-0000-0000-000000000001", "Addon Bom", [1, 0, 0], "script"),
                    "README.txt": "v1",
                },
            ),
            v1 / "x",
        )
        world = addons.world_dir(data, "world")
        check("achou o mundo pelo server.properties", world.name == "Revolucao")

        pack1 = addons.detect_packs(v1 / "x")[0]
        r1 = addons.apply(pack1, data, world, "new")
        check("pasta instalada", (data / "behavior_packs" / r1.folder / "manifest.json").is_file(), r1.folder)
        check("versao anterior None", r1.old_version is None)

        wbp = json.loads((world / "world_behavior_packs.json").read_text(encoding="utf-8"))
        check("entrada no world_behavior_packs.json", wbp[0]["pack_id"] == pack1.uuid and wbp[0]["version"] == [1, 0, 0], wbp)
        vkp = json.loads((data / "valid_known_packs.json").read_text(encoding="utf-8"))
        check("valid_known_packs.json atualizado", vkp[0]["version"] == "1.0.0" and vkp[0]["path"].startswith("behavior_packs/"), vkp)

        v2 = tmp / "v2"
        v2.mkdir()
        addons.extract_archive(
            zipar(
                v2 / "a.mcpack",
                {
                    "manifest.json": manifest("bbbb2222-0000-0000-0000-000000000001", "Addon Bom", [1, 1, 0], "script"),
                    "README.txt": "v2",
                },
            ),
            v2 / "x",
        )
        pack2 = addons.detect_packs(v2 / "x")[0]
        r2 = addons.apply(pack2, data, world, "update")
        check("atualizou versao", (r2.old_version, r2.new_version) == ((1, 0, 0), (1, 1, 0)), r2)
        check("mantem a mesma pasta", r2.folder == r1.folder, (r1.folder, r2.folder))
        check("conteudo novo no lugar", (data / "behavior_packs" / r2.folder / "README.txt").read_text() == "v2")
        wbp = json.loads((world / "world_behavior_packs.json").read_text(encoding="utf-8"))
        check("so uma entrada, versao nova", len(wbp) == 1 and wbp[0]["version"] == [1, 1, 0], wbp)
        check("backup da v1 guardado", (data / ".addon-backups" / pack1.uuid / "1.0.0" / "README.txt").read_text() == "v1")

        try:
            addons.apply(pack2, data, world, "new")
            check("bloqueia instalar duplicado", False)
        except addons.AddonError as exc:
            check("bloqueia instalar duplicado", "ja esta instalado" in str(exc), str(exc))

        addons.remove(pack1.uuid, data, world)
        check("pasta removida", not (data / "behavior_packs" / r2.folder).exists())
        wbp = json.loads((world / "world_behavior_packs.json").read_text(encoding="utf-8"))
        check("entrada removida do mundo", wbp == [], wbp)

        check("engine warning vazio quando ok", addons.engine_warning((1, 20, 0), "1.26.51.03") == "")
        check("engine warning dispara", "1.27.0" in addons.engine_warning((1, 27, 0), "1.26.51.03"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_raknet() -> None:
    print("parse do pong RakNet")
    motd = b"MCPE;Revolucao;800;1.26.51.03;3;10;123456;Revolucao;Survival;1;19132;19133;"
    pong = bytes((raknet.UNCONNECTED_PONG,)) + raknet.MAGIC + len(motd).to_bytes(2, "big") + motd
    r = raknet._parse_pong(pong)
    check("pong valido", r.motd_ok and r.ok)
    check("jogadores 3/10", (r.players, r.max_players) == (3, 10), (r.players, r.max_players))
    check("versao lida", r.version == "1.26.51.03", r.version)

    quebrado = (
        bytes((raknet.UNCONNECTED_PONG,))
        + raknet.MAGIC
        + (14).to_bytes(2, "big")
        + b"\x00" * 14
    )
    check("datagram do bug tem 33 bytes", len(quebrado) == 33, len(quebrado))
    rb = raknet._parse_pong(quebrado)
    check("pong de 33 bytes detectado", rb.ok and not rb.motd_ok, rb.resumo())
    check("resumo do bug cita MCPE", "MCPE" in rb.resumo(), rb.resumo())

    check("id errado rejeitado", not raknet._parse_pong(b"\x02" + raknet.MAGIC + b"\x00\x00").ok)
    check("lixo rejeitado", not raknet._parse_pong(b"\x1c" + b"\x00" * 8).ok)
    check("magic divergente rejeitado", "magic" in raknet._parse_pong(b"\x1c" + b"\x11" * 16 + b"\x00\x00").error)

    # nethernet (BDS 1.26.52+): mesmo pong, mas com 17 bytes de prefixo antes
    # do magic. Medido no servidor real: 131 bytes, payload de 96, sem sobra.
    prefixo = bytes.fromhex("00 00 01 a0 e4 70 4a 34 b2 eb eb 08 90 59 9f 5b 00")
    check("prefixo tem 17 bytes", len(prefixo) == 17, len(prefixo))
    nethernet = (
        bytes((raknet.UNCONNECTED_PONG,))
        + prefixo
        + raknet.MAGIC
        + len(motd).to_bytes(2, "big")
        + motd
    )
    rn = raknet._parse_pong(nethernet)
    check("pong do nethernet aceito", rn.ok and rn.motd_ok, rn.resumo())
    check("nethernet le jogadores", (rn.players, rn.max_players) == (3, 10), (rn.players, rn.max_players))
    check("nethernet le versao", rn.version == "1.26.51.03", rn.version)
    check(
        "payload do nethernet fecha o pacote",
        len(nethernet) == 1 + len(prefixo) + len(raknet.MAGIC) + 2 + len(motd),
        len(nethernet),
    )

    # o parser nao pode aceitar um MAGIC perdido no meio de um payload enorme
    isca = (
        bytes((raknet.UNCONNECTED_PONG,))
        + b"\x00" * 4
        + raknet.MAGIC
        + (2).to_bytes(2, "big")
        + b"XY"
        + b"\x00" * 8
    )
    check("framing invalido rejeitado", not raknet._parse_pong(isca).ok, raknet._parse_pong(isca).error)


def test_store() -> None:
    print("store: usuarios, chaves, overrides")
    tmp = Path(tempfile.mkdtemp())
    try:
        st = store.Store(tmp / "bot.db")

        check("admin unico", st.admin() is None)
        a = st.define_admin(1, "dono")
        check("papel admin", a.eh_admin and a.role == store.ROLE_ADMIN, a.role)
        st.define_admin(2, "ladrão")
        check("admin trocado de maos", st.admin() is not None and st.admin().user_id == 2)
        velho = st.usuario(1)
        check("ex-admin virou viewer", velho is not None and velho.role == store.ROLE_VIEWER, velho)

        try:
            st.remove_usuario(2)
            check("admin protegido", False)
        except ValueError:
            check("admin protegido", True)

        key, token = st.cria_chave(store.ROLE_VIEWER, "amigo", 2)
        check("chave em 3 blocos", len(token) == 14 and token.count("-") == 2, token)
        check("token em texto puro nao esta no banco", token not in (tmp / "bot.db").read_bytes().decode("latin-1"))
        check("busca por hash acha", st.chave_por_hash(store.hash_token(token)) is not None)
        check("hash diferente nao acha", st.chave_por_hash(store.hash_token(token + "x")) is None)
        check("chave normaliza ignorando hifen/case", store.hash_token("rv-ABC-def") == store.hash_token("rvabcdef"))

        st.registra_uso(key.id, 7, "leitor")
        k = st.chave_por_hash(store.hash_token(token))
        check("chave vinculada", k is not None and k.bound_to == 7, k)
        st.registra_uso(key.id, 8, "outro")
        k2 = st.chave_por_hash(store.hash_token(token))
        check("chave nao troca de dono", k2 is not None and k2.bound_to == 7, k2)

        rev = st.revoga_chave(key.id)
        check("revogada", rev is not None and not rev.ativa)
        check("revogada some da lista de ativas", st.chaves() == [])
        check("revogada ainda aparece no historico", len(st.chaves(incluir_revogadas=True)) == 1)
        try:
            st.cria_chave("root", "x", 2)
            check("papel invalido rejeitado", False)
        except ValueError:
            check("papel invalido rejeitado", True)

        st.set_override("difficulty", "hard", 2)
        st.set_override("max-players", "30", 2)
        check("overrides salvos", st.overrides() == {"difficulty": "hard", "max-players": "30"}, st.overrides())
        st.limpa_override("max-players")
        check("override removido", "max-players" not in st.overrides())

        st.audita(2, "config", "difficulty=hard")
        reg = st.auditoria(5)
        check("auditoria gravada", bool(reg) and reg[0]["action"] == "config", reg)

        jogador, perguntar = st.registra_jogador("Eduhqs", "2535463291192118")
        check(
            "spawn novo salvo como visitor pendente",
            perguntar and jogador["permission"] == "visitor" and jogador["decision"] == "pending",
            jogador,
        )
        st.define_jogador("Eduhqs", "2535463291192118", "visitor", "declined")
        jogador, perguntar = st.registra_jogador("Eduhqs", "2535463291192118")
        check(
            "recusa persistida nao pergunta de novo",
            not perguntar and jogador["permission"] == "visitor" and jogador["decision"] == "declined",
            jogador,
        )
        st.define_jogador("Eduhqs", "2535463291192118", "member", "approved")
        jogador, perguntar = st.registra_jogador("Eduhqs", "2535463291192118")
        check(
            "member persistido nao recebe nova pergunta",
            not perguntar and jogador["permission"] == "member" and jogador["decision"] == "approved",
            jogador,
        )
        st.fecha()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_store_migracao_allowlist() -> None:
    """O .db de quem rodou a versao com allow-list tem que ficar limpo.

    A limpeza importa mais do que parece: o reconciliador reaplica override a
    cada 60s, entao um 'allow-list=true' sobrevivendo no .db reescreveria a
    propriedade no server.properties para sempre, mesmo com a chave fora do
    catalogo. E o servidor ficaria fechado sem ninguem ter mandado nada.
    """
    print("\nstore: migracao tira a allow-list antiga")
    tmp = Path(tempfile.mkdtemp())
    try:
        caminho = tmp / "bot.db"
        # monta um .db no formato antigo
        velho = sqlite3.connect(caminho)
        velho.executescript(
            """
            CREATE TABLE overrides (k TEXT PRIMARY KEY, v TEXT NOT NULL,
                                    updated_by INTEGER, updated_at REAL NOT NULL);
            CREATE TABLE denied (name TEXT PRIMARY KEY COLLATE NOCASE, reason TEXT,
                                 by INTEGER, at REAL NOT NULL);
            CREATE TABLE liberacao (name TEXT PRIMARY KEY COLLATE NOCASE, by INTEGER,
                                    at REAL NOT NULL);
            INSERT INTO overrides VALUES ('allow-list', 'true', 1, 0);
            INSERT INTO overrides VALUES ('difficulty', 'hard', 1, 0);
            INSERT INTO denied VALUES ('JogadorRuim', 'grief', 1, 0);
            INSERT INTO liberacao VALUES ('Esperando', 1, 0);
            """
        )
        velho.commit()
        velho.close()

        st = store.Store(caminho)
        tabelas = {
            linha[0]
            for linha in sqlite3.connect(caminho).execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        check("tabela de negados apagada", "denied" not in tabelas, sorted(tabelas))
        check("tabela de liberacao apagada", "liberacao" not in tabelas, sorted(tabelas))
        check("override de allow-list apagado", "allow-list" not in st.overrides(), st.overrides())
        check("os outros overrides ficaram", st.overrides() == {"difficulty": "hard"}, st.overrides())

        # e rodar de novo nao estraga nada
        st2 = store.Store(caminho)
        check("migracao e idempotente", st2.overrides() == {"difficulty": "hard"}, st2.overrides())
        st.fecha()
        st2.fecha()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_auth() -> None:
    print("auth: resgate, papel, limite de tentativas")
    tmp = Path(tempfile.mkdtemp())
    try:
        st = store.Store(tmp / "bot.db")
        a = auth.Auth(st, "RV-codigo-secreto")
        print("   DEBUG", a.store.__class__, a.store.__class__.__module__,
              hasattr(a.store, "adiciona_viewer"), "adiciona_viewer" in dir(a.store))

        check("codigo casa palavra inteira", a.resgatou_admin("/start RV-codigo-secreto"))
        check("codigo casa em texto puro", a.resgatou_admin("  RV-codigo-secreto "))
        check("codigo com @ funciona", a.resgatou_admin("/start@MeuBot RV-codigo-secreto"))
        check("prefixo nao casa", not a.resgatou_admin("xRV-codigo-secreto"))
        check("outro codigo nao casa", not a.resgatou_admin("/start RV-outro"))
        check("vazio nao casa", not a.resgatou_admin(None))

        s = a.assume_admin(10, "dono")
        check("sessao de admin", s.eh_admin and s.role == store.ROLE_ADMIN)
        check("eh_admin do store", a.eh_admin(10) and not a.eh_admin(11))

        s2 = a.assume_admin(11, "novo")
        check("admin novo", s2.eh_admin)
        check("antigo rebaixado", a.papel(10) == store.ROLE_VIEWER, a.papel(10))
        check("sessao nova apaga o papel do admin antigo", a.sessao(10, "dono").eh_admin is False)

        try:
            a.resgata_chave(12, "malandro", st.cria_chave(store.ROLE_ADMIN, "falsa", 11)[1])
            check("chave de admin nao cria admin", False)
        except auth.AuthError as exc:
            check("chave de admin nao cria admin", "leitura" in str(exc), str(exc))

        # admin nao pode se rebaixar usando chave de leitura
        try:
            a.resgata_chave(11, "novo", st.cria_chave(store.ROLE_VIEWER, "k2", 11)[1])
            check("admin rebaixado por chave foi barrado", False)
        except auth.AuthError as exc:
            check("admin rebaixado por chave foi barrado", "admin" in str(exc), str(exc))
        check("admin continua admin", a.eh_admin(11))

        _, token = st.cria_chave(store.ROLE_VIEWER, "leitura", 11)
        sess, key, primeira = a.resgata_chave(20, "leitor", token)
        check("chave libera leitura", sess.role == store.ROLE_VIEWER and not sess.eh_admin)
        check("primeira vez marcada", primeira is True)
        check("chave vinculou no usuario", st.usuario(20) is not None)
        sess2, _, segunda = a.resgata_chave(20, "leitor", token)
        check("mesma pessoa de novo", segunda is False and sess2.user_id == 20)
        try:
            a.resgata_chave(21, "outro", token)
            check("chave nao serve para outro telegram", False)
        except auth.AuthError as exc:
            check("chave nao serve para outro telegram", "ja esta em uso" in str(exc), str(exc))

        try:
            a.resgata_chave(22, "x", "RV-inexistente-000")
            check("chave inexistente rejeitada", False)
        except auth.AuthError:
            check("chave inexistente rejeitada", True)

        for _ in range(auth.TENTATIVAS_MAX):
            a.registra_tentativa(30, ok=False)
        check("5 erros bloqueiam", a.segundos_bloqueio(30) > 0, a.segundos_bloqueio(30))
        check("quem nao errou nao foi bloqueado", a.segundos_bloqueio(99) == 0)
        a.registra_tentativa(30, ok=True)
        check("acerto limpa o contador", a.segundos_bloqueio(30) == 0, a.segundos_bloqueio(30))

        try:
            auth.Auth(st, "")
            check("codigo vazio barrado na construcao", False)
        except auth.AuthError:
            check("codigo vazio barrado na construcao", True)
        st.fecha()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_serverctl() -> None:
    print("serverctl: server.properties e permissions")
    tmp = Path(tempfile.mkdtemp())
    try:
        data = tmp / "data"
        data.mkdir()
        (data / "server.properties").write_text(
            "server-name=Revolucao\ngamemode=survival\ndifficulty=easy\n#comentario\n\n", encoding="utf-8"
        )
        (data / "permissions.json").write_text(json.dumps([{"player": "Ze", "xuid": "2535453759792258", "permission": "member"}]), encoding="utf-8")
        srv = serverctl.ServerControl(data, backup_keep=2)

        props = srv.le_props()
        check("props lidas", props["gamemode"] == "survival", props)
        check("props sem comentario", "#comentario" not in props)

        srv.set_prop("difficulty", "hard")
        texto = (data / "server.properties").read_text(encoding="utf-8")
        check("valor trocado no lugar", "difficulty=hard" in texto, texto)
        check("comentario preservado", "#comentario" in texto, texto)
        check("props mantidas", srv.le_props()["server-name"] == "Revolucao")
        srv.set_prop("difficulty", "easy")
        check("comentario na 2a edicao", "#comentario" in (data / "server.properties").read_text(encoding="utf-8"))
        check("existe backup", list(data.glob("server.properties.*.bak")), list(data.glob("*.bak")))

        try:
            serverctl.valida(serverctl.POR_CHAVE["server-name"], "Meu Server; drop")
            check("ponto e virgula barrado", False)
        except serverctl.PropertyError:
            check("ponto e virgula barrado", True)

        try:
            serverctl.valida(serverctl.POR_CHAVE["difficulty"], "impossivel")
            check("dificuldade invalida barrada", False)
        except serverctl.PropertyError:
            check("dificuldade invalida barrada", True)
        check("bool aceita sim", serverctl.valida(serverctl.POR_CHAVE["allow-cheats"], "sim") == "true")
        try:
            serverctl.valida(serverctl.POR_CHAVE["max-players"], "muitos")
            check("numero invalido barrado", False)
        except serverctl.PropertyError:
            check("numero invalido barrado", True)

        # server-udp-ports: as 5 formas que a doc do BDS mostra, mais o lixo
        # que o BDS aceitaria em silencio e deixaria o gameplay inalcancavel.
        udp = serverctl.POR_CHAVE["server-udp-ports"]
        for bom in (
            "49152",
            "49152-49200",
            "19132-19232:32000-32100",
            "203.0.113.10:19132-19232:32000-32100",
            "[2001:db8::1]:19132-19232:32000-32100",
        ):
            try:
                serverctl.valida(udp, bom)
                check(f"udp aceita {bom}", True)
            except serverctl.PropertyError as erro:
                check(f"udp aceita {bom} -> {erro}", False)
        for ruim in (
            "",
            "abc",
            "0",
            "70000",
            "49200-49152",
            "19133-19172:",
            ":19133-19172",
            "1-2-3",
            "19133 - 19172",
            "19133;rm -rf",
        ):
            try:
                serverctl.valida(udp, ruim)
                check(f"udp barra {ruim!r}", False)
            except serverctl.PropertyError:
                check(f"udp barra {ruim!r}", True)
        check("udp e perigosa", bool(udp.cuidado))

        # Quem manda no server-udp-ports e o compose (a imagem reescreve em todo
        # boot pelo SERVER_UDP_PORTS), entao o reconciliador nao pode brigar com
        # ele e o /config nao pode guardar override.
        check("server-udp-ports e do compose", serverctl.do_compose("server-udp-ports"))
        check("level-name e do compose", serverctl.do_compose("level-name"))
        check("difficulty nao e do compose", not serverctl.do_compose("difficulty"))
        check(
            "cuidado do udp aponta pro .env",
            "SERVER_UDP_PORTS" in udp.cuidado,
            udp.cuidado,
        )

        # A allow-list saiu do catalogo de proposito: o servidor e' aberto e
        # ninguem liga a lista por acidente. Quem quiser trancar a porta liga
        # ALLOW_LIST=true no .env ou manda /console allowlist on na mao - e o
        # /status avisa que ela esta ligada.
        check("allow-list nao esta no catalogo", "allow-list" not in serverctl.POR_CHAVE)
        check("allow-list nao e do compose", not serverctl.do_compose("allow-list"))

        check("valor aceito pelo valida", serverctl.valida(udp, "157.151.1.224:19133-19172:19133-19172")
              == "157.151.1.224:19133-19172:19133-19172")

        srv.set_prop("level-name", "Mundo Novo")
        check("nome do mundo com espaco", srv.nivel_do_mundo("world") == "Mundo Novo", srv.nivel_do_mundo("world"))

        srv.set_permissao("Ana", "1234567890123456", "operator")
        check("permissao gravada", srv.permissoes()[-1]["permission"] == "operator", srv.permissoes())
        srv.set_permissao("Ana", "1234567890123456", "member")
        check("permissao atualizada sem duplicar", len(srv.permissoes()) == 2, srv.permissoes())
        try:
            srv.set_permissao("Ana", "1234567890123456", "deus")
            check("nivel invalido barrado", False)
        except serverctl.PropertyError:
            check("nivel invalido barrado", True)
        srv.clear_permissao("Ana", "1234567890123456")
        check("permissao limpa", len(srv.permissoes()) == 1, srv.permissoes())

        log = "[2026] Player connected: Ze/2535453759792258"
        check("xuid encontrado no log", srv.xuid_no_log("Ze", log) == "2535453759792258")
        check("xuid ausente vira None", srv.xuid_no_log("Ninguem", log) is None)
        check(
            "evento de conexao parseado",
            serverctl.parse_player_connected("[Server] Player connected: Ze Do Zero/2535453759792258")
            == ("Ze Do Zero", "2535453759792258"),
        )
        check(
            "evento Player Spawned com xuid e pfid parseado",
            serverctl.parse_player_connected(
                "[2026-09-29 12:00:00:000 INFO] Player Spawned: Eduhqs xuid: 2535463291192118, pfid: 47998C57B6BFE2B0"
            ) == ("Eduhqs", "2535463291192118"),
        )
        check("evento sem xuid volta None", serverctl.parse_player_connected("[Server] Player connected: Ze") is None)

        # online-mode decide se o /ops pode funcionar: o permissions.json casa
        # por xuid, e xuid so existe com autenticacao online.
        srv.set_prop("online-mode", "false")
        check("online_mode le desligado", srv.online_mode() is False, srv.le_props())
        (data / "server.properties").write_text("server-name=Revolucao\n", encoding="utf-8")
        check("sem a chave, vale o padrao do BDS (true)", srv.online_mode() is True, srv.le_props())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_promocao_confirma_reload() -> None:
    print("admin: promocao so confirma se permission reload foi aceito")

    class FakeServer:
        def online_mode(self) -> bool:
            return True

        def set_permissao(self, nome: str, xuid: str, nivel: str) -> bool:
            return True

    class FakeStore:
        def __init__(self) -> None:
            self.jogador: tuple[str, str, str, str] | None = None

        def define_jogador(self, nome: str, xuid: str, nivel: str, decisao: str) -> None:
            self.jogador = (nome, xuid, nivel, decisao)

        def audita(self, *args: object) -> None:
            pass

    class FakeDocker:
        def __init__(self, erro: str, linhas: list[str]) -> None:
            self.erro = erro
            self.linhas = linhas

        def console(self, comando: str) -> tuple[str, list[str]]:
            return self.erro, self.linhas

    store_fake = FakeStore()
    contexto = SimpleNamespace(
        server=FakeServer(),
        store=store_fake,
        docker=FakeDocker("send-command recusou: processo do BDS nao encontrado", []),
    )
    sucesso, resposta = asyncio.run(
        admin._promover_jogador(
            contexto, "Eduhqs", "2535463291192118", "member", quem=1, origem="/member"
        )
    )
    check("reload recusado nao finge sucesso", not sucesso and "recusou permission reload" in resposta, resposta)
    check("permissao pretendida fica salva no sqlite", store_fake.jogador == (
        "Eduhqs", "2535463291192118", "member", "approved"
    ), store_fake.jogador)

    contexto.docker.erro = ""
    contexto.docker.linhas = ["Reloaded permissions from file."]
    sucesso, resposta = asyncio.run(
        admin._promover_jogador(
            contexto, "Eduhqs", "2535463291192118", "member", quem=1, origem="/member"
        )
    )
    check("resposta do BDS confirma promocao", sucesso and "Reloaded permissions" in resposta, resposta)

    contexto.docker.linhas = ["permission reload"]
    sucesso, resposta = asyncio.run(
        admin._promover_jogador(
            contexto, "Eduhqs", "2535463291192118", "member", quem=1, origem="/member"
        )
    )
    check("reload sem resposta nao finge sucesso", not sucesso and "nao recebi confirmacao" in resposta, resposta)


def test_interpreta_config() -> None:
    print("serverctl: /config chave valor [sim]")
    inter = serverctl.interpreta_config
    conf = serverctl.precisa_confirmacao

    check("sem valor nao e comando", inter(["difficulty"]) is None)
    check("chave e valor simples", inter(["difficulty", "hard"]) == ("difficulty", "hard", False))
    check("chave em qualquer caixa", inter(["Difficulty", "HARD"]) == ("difficulty", "HARD", False))
    check("texto com espaco preservado", inter(["server-name", "Meu", "Server", "Legal"]) == ("server-name", "Meu Server Legal", False))
    check("sim confirma", inter(["max-players", "30", "sim"]) == ("max-players", "30", True))
    check("sim em caixa alta confirma", inter(["max-players", "30", "SIM"]) == ("max-players", "30", True))
    check("confirmacao nao some do texto", inter(["server-name", "Meu", "Server", "Legal", "sim"]) == ("server-name", "Meu Server Legal", True))
    check("sim sozinho e valor, nao confirmacao", inter(["allow-cheats", "sim"]) == ("allow-cheats", "sim", False))
    check("3 palavras com sim no fim e confirmacao", inter(["server-name", "para", "sim"]) == ("server-name", "para", True))
    check("sim e valor de verdade", serverctl.valida(serverctl.POR_CHAVE["allow-cheats"], "sim") == "true")

    perigosa = serverctl.POR_CHAVE["max-players"]
    check("perigosa pede confirmacao", conf(perigosa, False))
    check("perigosa aceita com sim", not conf(perigosa, True))
    check("prop sem cuidado nao pede nada", not conf(serverctl.POR_CHAVE["view-distance"], False))

    # o caminho feliz: validar so depois de decidir a confirmacao
    chave, bruto, ok = inter(["level-seed", "abc", "sim"])
    valor = serverctl.valida(serverctl.POR_CHAVE[chave], bruto)
    check("fluxo completo aplica", (chave, valor) == ("level-seed", "abc") and not conf(serverctl.POR_CHAVE[chave], ok))


def test_nome_do_gamertag() -> None:
    """Como o gamertag sobrevive ao /ops e ao /chutar.

    Gamertag com espaco tem que chegar inteiro, e a doc oficial exige aspas. O
    lado perigoso do parser e' o outro: mexer em 'Ze' quando o jogador se chama
    'Ze Do Zero' nao da erro nenhum, so aparece quando ele tenta entrar. Por
    isso o resto do argumento e' devolve inteiro, nunca cortado em silencio.
    """
    print("\ngamertag: nome e aspas")
    check("gamertag simples", admin._tokens("Ze") == ["Ze"], admin._tokens("Ze"))
    check("gamertag com espaco entre aspas", admin._tokens('"Ze Do Zero"') == ["Ze Do Zero"])
    check("aspas quebrada nao vira aspa no nome", admin._tokens('"Ze Do') == ["Ze", "Do"], admin._tokens('"Ze Do'))
    check("motivo do chutar fica inteiro", admin._tokens('"Ze Do Zero" quebrando o servidor')[-1] == "servidor")
    check("vazio nao quebra", admin._tokens("") == [])


def test_config_persiste() -> None:
    """O /config tem que sobreviver ao boot do container e ao recreate do bot.

    A config vive em dois lugares ao mesmo tempo: o /data/server.properties (o
    que o BDS le) e a tabela overrides do bot.db (a memoria do bot). O boot
    reescreve o arquivo, entao a unica coisa que faz a config voltar e o
    reconciliador, que compara os dois e reaplica o que divergiu.
    """
    print("config: o que o bot grava sobrevive ao boot e ao recreate")
    tmp = Path(tempfile.mkdtemp())
    try:
        data = tmp / "data"
        data.mkdir()
        props = data / "server.properties"
        padrao = "server-name=Revolucao\ndifficulty=easy\nmax-players=10\n"
        # o que a imagem deixa no arquivo depois do boot quando LEVEL_NAME e
        # SERVER_UDP_PORTS estao setados: essas duas ganham do compose
        padrao_compose = (
            padrao + "level-name=MundoNovo\nserver-udp-ports=19133-19172\n"
        )
        props.write_text(padrao, encoding="utf-8")
        db = tmp / "state" / "bot.db"

        srv = serverctl.ServerControl(data)
        st = store.Store(db)
        ctx = SimpleNamespace(store=st, server=srv)

        # o que o /config difficulty hard faz: arquivo + banco
        valor = serverctl.valida(serverctl.POR_CHAVE["difficulty"], "hard")
        srv.set_prop("difficulty", valor)
        st.set_override("difficulty", valor, 42)
        check("gravou no server.properties", srv.le_props()["difficulty"] == "hard", srv.le_props())
        check("gravou no bot.db", st.overrides() == {"difficulty": "hard"}, st.overrides())
        check("sem drift logo apos gravar", ops.overrides_sujo(ctx) == [])

        # o boot reescreve o arquivo com o padrao da imagem
        props.write_text(padrao, encoding="utf-8")
        drift = ops.overrides_sujo(ctx)
        check("drift detectado depois do boot", drift == [("difficulty", "hard", "easy")], drift)

        reaplicados = ops.aplica_overrides(ctx)
        check("reconciliador reaplicou", reaplicados == ["difficulty=hard"], reaplicados)
        check("arquivo corrigido", srv.le_props()["difficulty"] == "hard", srv.le_props())
        check("so o que divergiu foi tocado", srv.le_props()["server-name"] == "Revolucao", srv.le_props())
        check("reconciliador e idempotente", ops.aplica_overrides(ctx) == [] and ops.overrides_sujo(ctx) == [])

        # o bot foi recriado (docker compose up -d --build): o .db continua
        st.fecha()
        st2 = store.Store(db)
        srv2 = serverctl.ServerControl(data)
        ctx2 = SimpleNamespace(store=st2, server=srv2)
        check("override sobrevive ao recreate", st2.overrides() == {"difficulty": "hard"}, st2.overrides())

        # server-udp-ports e level-name sao do compose: o reconciliador nao pode
        # reescrever nada la, senao ele e a imagem brigam a cada 60s e o boot
        # seguinte desfaz o que o reconciliador fez. Teste em data proprio para
        # nao mexer na contagem de backups acima.
        with tempfile.TemporaryDirectory() as tmp_comp:
            data_c = Path(tmp_comp)
            (data_c / "server.properties").write_text(padrao, encoding="utf-8")
            stc = store.Store(data_c / "bot.db")
            srv_c = serverctl.ServerControl(data_c)
            ctx_c = SimpleNamespace(store=stc, server=srv_c)
            stc.set_override("difficulty", "hard", 42)
            stc.set_override("server-udp-ports", "19133-19172", 42)
            stc.set_override("level-name", "Mundo", 42)
            # o boot da imagem sobrescreve as duas; o reconciliador tem de
            # repassar por cima do que a imagem fez, e so do difficulty
            (data_c / "server.properties").write_text(padrao_compose, encoding="utf-8")
            so_comp = [k for k, _v, _a in ops.overrides_sujo(ctx_c)]
            check("reconciliador ignora o que e do compose", so_comp == ["difficulty"], so_comp)
            check("aplicar nao toca nas chaves do compose", ops.aplica_overrides(ctx_c) == ["difficulty=hard"])
            lido = srv_c.le_props()
            check("level-name do compose nao foi sobrescrito", lido["level-name"] == "MundoNovo", lido)
            check(
                "server-udp-ports do compose nao foi sobrescrito",
                lido["server-udp-ports"] == "19133-19172",
                lido,
            )
            check("difficulty continua sob controle do bot", lido["difficulty"] == "hard", lido)
            stc.fecha()

        props.write_text(padrao, encoding="utf-8")
        check("reaplicou depois do recreate", ops.aplica_overrides(ctx2) == ["difficulty=hard"])
        check("valor final correto", srv2.le_props()["difficulty"] == "hard", srv2.le_props())
        st2.fecha()

        # backup: um por gravacao, sem colidir, e podado no limite
        nomes = [p.name for p in sorted(data.glob("server.properties.*.bak"))]
        check("um backup por gravacao", len(nomes) == 3, nomes)
        check("sem colisao de carimbo no mesmo segundo", len(set(nomes)) == 3, nomes)
        primeiro = min(data.glob("server.properties.*.bak"))
        check("cada backup tem o conteudo anterior", "difficulty=easy" in primeiro.read_text(encoding="utf-8"))

        # agora com backup_keep=2: os mais velhos caem fora
        srv = serverctl.ServerControl(data, backup_keep=2)
        srv.set_prop("difficulty", "normal")
        srv.set_prop("difficulty", "hard")
        srv.set_prop("difficulty", "easy")
        check(
            "server.properties podado no backup_keep",
            len(list(data.glob("server.properties.*.bak"))) == 2,
            [p.name for p in data.glob("server.properties.*.bak")],
        )

        # o permissions.json tambem faz backup, e tambem e podado
        for i in range(5):
            srv.set_permissao(f"Jogador{i}", str(1000000000000000 + i), "member")
        check(
            "backup do permissions.json tambem e podado",
            len(list(data.glob("permissions.json.*.bak"))) == 2,
            [p.name for p in data.glob("permissions.json.*.bak")],
        )
        check("permissions.json continua valido", len(srv.permissoes()) == 5, srv.permissoes())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_console() -> None:
    """O /console manda no BDS e devolve so a resposta, nao o log inteiro."""
    print("console: leitura da resposta do BDS")
    from docker.errors import APIError

    # --- apresentacao: o que o admin ve no Telegram ---
    # As linhas abaixo estao no formato que o container really printa
    # ("[data hora:ms NIVEL] texto", com o nivel dentro do colchete).
    linhas = [
        "[2026-09-28 16:41:38:198 INFO] [Command] list",
        "[2026-09-28 16:45:00:124 INFO] There are 2 of a max of 10 players online:",
        "[2026-09-28 16:45:00:125 INFO] Ze, 1.2.3.4",
        "[2026-09-28 16:45:00:125 INFO] Ze, 1.2.3.4",
        "",
        "[2026-09-28 16:45:00:126 INFO] ",
    ]
    saida = ops.resposta_console("list", linhas)
    check("tira o carimbo com nivel dentro", "2026-09-28 16:45" not in saida, saida)
    check("tira o eco do comando", not any(linha.strip() == "list" for linha in saida.splitlines()), saida)
    check("tira linha repetida", saida.count("Ze, 1.2.3.4") == 1, saida)
    check("tira linha vazia", "There are 2" in saida, saida)
    # formato antigo: data num colchete e a tag [Server] em outro
    check(
        "formato antigo tambem e limpo",
        ops.limpa_linha_console("[2026-09-28 12:00:00:001 UTC] [Server] [Command] tps") == "tps",
        ops.limpa_linha_console("[2026-09-28 12:00:00:001 UTC] [Server] [Command] tps"),
    )
    check("guarda a severidade", ops.limpa_linha_console("[2026-09-28 12:00:00:001 UTC] [Error] boom") == "[Error] boom")
    check("diz que nao veio nada", "nao devolveu nada" in ops.resposta_console("save-resume", []))
    longo = ops.resposta_console("help", [f"[2026-09-28 16:45:00:1 INFO] comando numero {i} " + "x" * 60 for i in range(60)])
    check("resposta longa e cortada pelo fim", "... (inicio cortado)" in longo and len(longo) < 3600, len(longo))

    # --- a janela de log: e o que garante que a resposta seja a nova ---
    class _Fake:
        """DockerController sem Docker: logs e exec controlada a mao."""

        def __init__(self, antes: list[str], depois: list[str], erro: bool = False, recusa: str = "") -> None:
            self._linhas = list(antes)
            self._depois = list(depois)
            self._erro = erro
            self._recusa = recusa
            self.comandos: list[tuple[str, ...]] = []
            self.console = MethodType(docker_ctl.DockerController.console, self)

        def logs(self, lines: int = 200, since: float | None = None) -> str:
            return "\n".join(self._linhas[-lines:])

        def exec(self, *args: str) -> str:
            self.comandos.append(args)
            if self._erro:
                raise APIError("container parado")
            if self._recusa:
                return self._recusa
            # o comando que entrou no console aparece no log
            self._linhas = self._linhas + self._depois
            return ""

    velhas = ["[2026-09-28 16:41:38:198 INFO] Server started."]
    novas = [
        "[2026-09-28 16:45:00:123 INFO] [Command] list",
        "[2026-09-28 16:45:00:124 INFO] There are 1 of a max of 10 players online:",
    ]
    fake = _Fake(velhas, novas)
    erro, resposta = fake.console("list", espera=0.4, teto=25)
    check("manda pelo send-command", fake.comandos == [("send-command", "list")], fake.comandos)
    check("entregue sem erro", erro == "", erro)
    check("devolve so as linhas novas", resposta == novas, resposta)
    check("nao devolve o log antigo", not any("Server started" in linha for linha in resposta), resposta)

    fake = _Fake(velhas, novas, erro=True)
    erro, resposta = fake.console("list", espera=0.4)
    check("erro do docker nao levanta", bool(erro) and resposta == [], (erro, resposta))

    # o send-command recusou (nao achou o processo): tem que dizer, nao fingir
    # que o console ficou mudo. A recusa abaixo e a saida real do container.
    recusa = (
        "find: '/proc/1/exe': Permission denied\n"
        "find: '/proc/12/exe': Permission denied\n"
        "ERROR: failed to search for bedrock server process"
    )
    fake = _Fake(velhas, novas, recusa=recusa)
    erro, resposta = fake.console("list", espera=0.4)
    check("recusa do send-command e erro, nao silencio", bool(erro) and resposta == [], (erro, resposta))
    saida = ops.resposta_console("list", resposta, erro)
    check("admin ve que nao chegou", "nao chegou no console" in saida, saida)
    check("motivo do /proc eh o do Docker Desktop", "Docker Desktop" in erro, erro)
    check(
        "motivo do processo ausente nao fala de Windows",
        "Docker Desktop" not in docker_ctl._motivo_recusa("ERROR: unable to find bedrock server process"),
        docker_ctl._motivo_recusa("ERROR: unable to find bedrock server process"),
    )

    # comando sem resposta: espera o tempo todo e devolve vazio
    fake = _Fake(velhas, [])
    erro, resposta = fake.console("save-resume", espera=0.4)
    check("comando mudo devolve vazio", erro == "" and resposta == [], (erro, resposta))

    # rotacao de log encolhe a janela: melhor nao devolver nada do que mentir
    fake = _Fake(velhas, [])
    fake._linhas = ["so uma linha"]
    erro, resposta = fake.console("tps", espera=0.4)
    check("rotacao de log nao inventa linha", resposta == [], resposta)

    # teto: help despeja muita coisa e o Telegram tem limite
    fake = _Fake(velhas, [f"linha {i}" for i in range(50)])
    erro, resposta = fake.console("help", espera=0.4, teto=5)
    check("respeita o teto de linhas", len(resposta) == 5, len(resposta))

    # --- o caso que so aparece com o servidor de verdade ---
    # `docker logs --tail=500` devolve SEMPRE 500 linhas num log com mais de
    # 500, entao comparar contagem nunca acha nada. A ancora tem que ser pelo
    # conteudo, e este e o teste que pega a regressao.
    check(
        "log cheio nao esconde a resposta",
        docker_ctl._delta(["velha"] * 500, ["velha"] * 498 + novas) == novas,
        docker_ctl._delta(["velha"] * 500, ["velha"] * 498 + novas),
    )
    fake = _Fake(["velha"] * 500, novas)
    erro, resposta = fake.console("list", espera=0.4)
    check("resposta no log cheio", resposta == novas, len(resposta))
    check("log cheio nao devolve o historico", all("velha" not in linha for linha in resposta), resposta[:2])

    # linhas repetidas: a ancora tem que casar pelo maior pedaco, senao a
    # resposta repete a ultima linha do log antigo
    check(
        "ancora acha a maior sobreposicao",
        docker_ctl._delta(["A", "B", "B"], ["A", "B", "B", "C"]) == ["C"],
        docker_ctl._delta(["A", "B", "B"], ["A", "B", "B", "C"]),
    )
    check("sem sobreposicao nao inventa", docker_ctl._delta(["A", "B"], ["X", "Y"]) == [])
    check("log vazio antes devolve tudo", docker_ctl._delta([], ["A"]) == ["A"])
    check("nada novo devolve vazio", docker_ctl._delta(["A", "B"], ["A", "B"]) == [])


def test_dropbox() -> None:
    print("\ndropbox: cliente HTTP")
    chamadas: list[tuple[str, bytes]] = []
    chamadas_arg: list[tuple[str, str]] = []

    class Fake:
        """Responde como o Dropbox, guardando o que foi pedido."""

        def __init__(self) -> None:
            self.renovadas = 0
            self.headers: list[dict] = []

        def __call__(self, url: str, *, body=None, headers=None, timeout=300.0) -> bytes:
            chamadas.append((url, body or b""))
            # nas rotas de upload o corpo e o arquivo, e o argumento vai no
            # header Dropbox-API-Arg. Sem isso o teste estaria lendo JSON de
            # onde tem binario e o erro seria meu, nao do codigo.
            chamadas_arg.append((url, (headers or {}).get("Dropbox-API-Arg", "")))
            self.headers.append(dict(headers or {}))
            if url.endswith("/oauth2/token"):
                self.renovadas += 1
                return b'{"access_token":"acc1","expires_in":14400}'
            if url.endswith("/list_folder"):
                # a lista e a mesma em toda chamada: o teste nao simula o
                # efeito do delete, e o que importa aqui e quais nomes a
                # rotacao escolhe, nao o estado da pasta depois
                return b'{"entries":[' + b",".join(
                        json.dumps(
                            {
                                ".tag": "file",
                                "name": n,
                                "path_display": f"/backups/{n}",
                                "size": 100,
                            }
                        ).encode()
                        for n in (
                            "mine-bedrock-20260101T000000Z.tar.gz",
                            "mine-bedrock-20260108T000000Z.tar.gz",
                            "mine-bedrock-20260115T000000Z.tar.gz",
                            "arquivo-do-usuario.txt",
                        )
                    ) + b'],"has_more":false}'
            if url.endswith("/delete_v2"):
                return b'{"metadata":{}}'
            if url.endswith("/create_shared_link_with_settings"):
                return b'{"url":"https://dropbox.example/bkp"}'
            if url.endswith("/upload_session/start"):
                return b'{"session_id":"sess-1"}'
            if url.endswith(("/append_v2", "/finish")):
                return b"{}"
            raise AssertionError(f"rota inesperada: {url}")

    tmp = Path(tempfile.mkdtemp())
    try:
        # arquivo maior que um chunk, para exercitar a sessao de verdade
        arquivo = tmp / "mine-bedrock-20260122T000000Z.tar.gz"
        arquivo.write_bytes(b"x" * (dropbox.CHUNK * 2 + 17))
        rotas = [u.rsplit("/", 2)[-2:] for u, _ in chamadas]
        check("ainda nao chamou nada", chamadas == [])

        dbx = dropbox.Dropbox(
            token="tok", refresh_token="ref", app_key="key", app_secret="sec", http=Fake()
        )
        check("configurado com refresh token", dbx.configurado())
        dbx.envia(arquivo, "/backups/teste.tar.gz")
        def args_de(rota: str) -> list[dict]:
            return [json.loads(a) for u, a in chamadas_arg if u.endswith(rota)]

        iniciadas = args_de("/upload_session/start")
        acrescentadas = args_de("/append_v2")
        encerradas = args_de("/finish")
        check("upload por sessao", len(iniciadas) == 1, rotas)
        check("mandou os chunks do meio", len(acrescentadas) == 2, len(acrescentadas))
        check("encerrou a sessao", len(encerradas) == 1, len(encerradas))
        # o offset do append precisa crescer, senao o Dropbox devolve conflict
        offsets = [a["cursor"]["offset"] for a in acrescentadas]
        check("offset cresce entre os chunks", offsets == [dropbox.CHUNK, dropbox.CHUNK * 2], offsets)
        # e o commit tem que fechar no tamanho real do arquivo, senao trunca
        commit = encerradas[0]["commit"]
        check("commit no path certo", commit["path"] == "/backups/teste.tar.gz", commit)
        check("commit fecha no tamanho do arquivo", encerradas[0]["cursor"]["offset"] == arquivo.stat().st_size)
        check("sessaoStarted com close=false", iniciadas[0].get("close") is False, iniciadas[0])

        # refresh token: renovar uma vez e reaproveitar ate perto de expirar
        fake = Fake()
        # o relogio e injetado porque um teste de expiracao de 4 horas nao
        # cabe num selftest: aqui a "agora" anda na mao.
        relogio = [1000.0]
        dbx2 = dropbox.Dropbox(
            refresh_token="r", app_key="k", app_secret="s", http=fake, agora=lambda: relogio[0]
        )
        dbx2._token()
        primeiro_validade = dbx2._validade
        dbx2._token()
        check("renova uma vez e reusa", fake.renovadas == 1, fake.renovadas)

        # o token vale 14400s a partir da renovacao; a margem e 300s, entao
        # abaixo de (validade - 300) ainda reusa e acima disso renova
        limite = primeiro_validade - dropbox.RENOVAR_COM_MARGEM

        relogio[0] = limite - 600
        dbx2._token()
        check("reusa enquanto o token vale", fake.renovadas == 1, fake.renovadas)

        relogio[0] = limite + 1
        dbx2._token()
        check("renova assim que entra na margem", fake.renovadas == 2, fake.renovadas)

        # token fixo: usado como esta, sem tentar renovar
        dbx3 = dropbox.Dropbox(token="so-fixto", http=Fake())
        check("token fixo serve", dbx3._token() == "so-fixto")
        check("token fixo sozinho conta como configurado", dbx3.configurado())
        check("sem nada nao esta configurado", not dropbox.Dropbox().configurado())

        # link ja compartilhado: o Dropbox recusa e a gente busca o existente
        class Recusa:
            def __call__(self, url, *, body=None, headers=None, timeout=300.0) -> bytes:
                if url.endswith("/create_shared_link_with_settings"):
                    raise dropbox.DropboxError("shared link already exists", "shared_link_already_exists")
                if url.endswith("/list_shared_links"):
                    return b'{"links":[{"url":"https://dropbox.example/jah-existe"}]}'
                raise AssertionError(url)

        check("link repetido nao quebra", dropbox.Dropbox(token="t", http=Recusa()).link("/backups/a.tar.gz") == "https://dropbox.example/jah-existe")

        # rotacao: apaga os antigos do bot e nao toca no que o usuario deixou
        fake = Fake()
        dbx4 = dropbox.Dropbox(token="t", http=fake)
        apagados, _bytes, sobraram = dbx4.roda("/backups", backup.PREFIXO, manter=2)
        deletados = [json.loads(c[1])["path"] for c in chamadas if c[0].endswith("/delete_v2")]
        check("rotaciona pelo nome", apagados == ["mine-bedrock-20260101T000000Z.tar.gz"], apagados)
        check("nao apaga o que nao e backup", all("arquivo-do-usuario" not in d for d in deletados), deletados)
        check("mantem os novos", len(apagados) == 1, apagados)
        check("conta quantos sobraram", sobraram == 2, sobraram)

        # upload + rotacao + link, o caminho que o /backup faz de verdade
        chamadas.clear()
        chamadas_arg.clear()
        fake = Fake()
        arquivo = tmp / f"{backup.PREFIXO}20260122T000000Z.tar.gz"
        arquivo.write_bytes(b"conteudo")
        link, erro, apagados, sobraram = backup.sobe(
            dropbox.Dropbox(token="t", http=Fake()), arquivo, manter=2
        )
        check("sobe e devolve o link", link == "https://dropbox.example/bkp" and not erro, (link, erro))
        check(
            "apagados no caminho completo",
            apagados == ["mine-bedrock-20260101T000000Z.tar.gz"],
            (apagados, sobraram),
        )
        # sem isto o Dropbox recusa os 4 MB antes de a gente ler o erro, e o
        # usuario ve "Broken pipe" em vez de "falta a permissao X"
        uploads = [h for h in fake.headers if "Dropbox-API-Arg" in h]
        check("upload pede 100-continue", all(h.get("Expect") == "100-continue" for h in uploads), uploads[:1])

        # Dropbox fora do ar: o erro vem, e nao uma excecao
        class Cai:
            def __call__(self, url, *, body=None, headers=None, timeout=300.0):
                raise dropbox.DropboxError("nao consegui falar com o Dropbox: timeout")

        link, erro, apagados, sobraram = backup.sobe(
            dropbox.Dropbox(token="t", http=Cai()), arquivo, manter=2
        )
        check("dropbox fora devolve erro, nao levanta", link == "" and "timeout" in erro, (link, erro))

        # --- a mensagem de erro do Dropbox ---
        # Este e o caso que escondia a causa real: falta de permissao chega
        # com error_summary "other/..." e a frase boa em user_message.text.
        import urllib.error

        def erro_http(corpo: bytes, code: int = 400) -> urllib.error.HTTPError:
            return urllib.error.HTTPError("https://api.dropboxapi.com/x", code, "Bad Request", {}, io.BytesIO(corpo))

        sem_permissao = json.dumps(
            {
                "error": {".tag": "other"},
                "error_summary": "other/...",
                "user_message": {
                    "locale": "en",
                    "text": "Your app (ID: 8703075) is not permitted to access this endpoint "
                    "because it does not have the required scope 'files.content.write'.",
                },
            }
        ).encode()
        mensagem, codigo = dropbox._erro_http(erro_http(sem_permissao))
        check("erro de permissao diz qual escopo falta", "files.content.write" in mensagem, mensagem)
        check("erro de permissao nao mostra so o other", not mensagem.startswith("other"), mensagem)
        check("erro de permissao guarda a sigla", "other" in mensagem, mensagem)
        check("sigla do erro de permissao", codigo == "other", codigo)

        # sem user_message o summary ainda tem que aparecer
        simples = json.dumps({"error_summary": "path/not_found/...", "error": {".tag": "path"}}).encode()
        mensagem, codigo = dropbox._erro_http(erro_http(simples))
        check("summary usado quando nao ha user_message", "not_found" in mensagem, mensagem)
        check("sigla do path/not_found", codigo == "path", codigo)

        # corpo que nem e JSON nao pode estourar
        mensagem, _ = dropbox._erro_http(erro_http(b"<html>500</html>", 500))
        check("corpo nao-JSON nao quebra", bool(mensagem), mensagem)

        # o Dropbox responde a mesma coisa de dois jeitos: JSON no upload e
        # texto puro na API de metadados. Nos dois a frase boa e a mesma, e
        # antes do conserto a de texto puro virava "Bad Request"
        texto_puro = (
            b'Error in call to API function "files/list_folder": Your app (ID: 8703075) is not '
            b"permitted to access this endpoint because it does not have the required scope "
            b"'files.metadata.read'."
        )
        mensagem, codigo = dropbox._erro_http(erro_http(texto_puro))
        check("erro em texto puro diz o escopo", "files.metadata.read" in mensagem, mensagem)
        check("erro em texto puro nao vira Bad Request", "Bad Request" not in mensagem, mensagem)
        check("sigla do texto puro", codigo == "texto", codigo)

        # corpo vazio: a mensagem ainda precisa apontar a causa provavel
        mensagem, codigo = dropbox._erro_http(erro_http(b"", 400))
        check("corpo vazio aponta permissao", "permissao" in mensagem, mensagem)
        check("sigla do corpo vazio", codigo == "sem_detalhe", codigo)

        # conexao cortada no meio do upload: precisa apontar a causa
        # provavel. Sem isto o usuario so viu "Broken pipe" e nao deu pra
        # descobrir que a app nao tinha permissao.
        urlopen_real = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.URLError(BrokenPipeError(32, "Broken pipe"))
        )
        try:
            dropbox._http("https://content.dropboxapi.com/2/files/upload_session/append_v2", body=b"x" * 10, headers={})
            check("pipe cortado levanta erro", False, "nao levantou")
        except dropbox.DropboxError as exc:
            texto = str(exc)
            check("pipe cortado levanta erro", True)
            check("mencao de permissao, nao so o errno", "permissao" in texto, texto)
            check("some o Broken pipe cru", "Broken pipe" not in texto, texto)
        finally:
            urllib.request.urlopen = urlopen_real
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_backup() -> None:
    print("\nbackup: empacotamento e rotacao")
    tmp = Path(tempfile.mkdtemp())
    try:
        data = tmp / "data"
        state = tmp / "state"
        (data / "worlds" / "world").mkdir(parents=True)
        (data / "behavior_packs" / "p1").mkdir(parents=True)
        (data / "worlds" / "world" / "level.dat").write_bytes(b"mundo")
        (data / "server.properties").write_text("level-name=world\n", encoding="utf-8")
        (data / "allowlist.json").write_text("[]\n", encoding="utf-8")
        (data / "bedrock_server-1.26.52.3").mkdir()
        (data / "bedrock_server-1.26.52.3" / "binario").write_bytes(b"x" * 5000)
        (data / "content_log.txt").write_text("log\n", encoding="utf-8")
        (data / "server.properties.123.bak").write_text("velho\n", encoding="utf-8")
        state.mkdir()
        (state / "bot.db").write_bytes(b"sqlite")

        alvos = backup._seleciona(data, state)
        nomes = [dentro for _, dentro in alvos]
        check("mundo entra", "data/worlds" in nomes, nomes)
        check("config entra", "data/server.properties" in nomes, nomes)
        check("allowlist entra", "data/allowlist.json" in nomes, nomes)
        check("bot.db entra", "state/bot.db" in nomes, nomes)
        check("binario do BDS nao entra", not any("bedrock_server" in n for n in nomes), nomes)
        check("content log nao entra", "data/content_log.txt" not in nomes, nomes)
        check("bak nao entra", not any(n.endswith(".bak") for n in nomes), nomes)

        destino = tmp / "backup.tar.gz"
        tamanho, _itens = backup.empacota(data, state, destino)
        check("tar foi criado", destino.is_file() and tamanho > 0, tamanho)
        check("o BDS de dentro do tar seria 5 KB", 5000 > tamanho, tamanho)

        import tarfile

        with tarfile.open(destino) as tar:
            membros = tar.getnames()
        check("mundo dentro do tar", "data/worlds/world/level.dat" in membros, membros)
        check("bot.db dentro do tar", "state/bot.db" in membros, membros)
        check("nada de binario no tar", not any("bedrock_server" in m for m in membros), membros)

        # o tar tem que abrir de verdade: e o que o usuario faz na hora de restaurar
        with tarfile.open(destino) as tar:
            dado = tar.extractfile("data/worlds/world/level.dat").read()
        check("conteudo do mundo legivel", dado == b"mundo", dado)

        # rotacao local
        pasta = tmp / "bk" / "atuais"
        pasta.mkdir(parents=True)
        for dia in ("01", "08", "15", "22", "29"):
            (pasta / f"{backup.PREFIXO}2026{dia}T000000Z.tar.gz").write_bytes(b"x")
        (pasta / "nao-e-backup.txt").write_bytes(b"x")
        apagados = backup.roda_local(tmp / "bk", manter=3)
        restantes = sorted(p.name for p in pasta.iterdir())
        check("rotaciona local", len(apagados) == 2, apagados)
        check("mantem os 3 novos", len(restantes) == 4, restantes)
        check("nao apaga arquivo estranho", "nao-e-backup.txt" in restantes, restantes)

        # nome ordenavel: dois backups no mesmo dia tem que ordenar por horario
        nomes_ordenados = sorted(
            [
                backup.nome_arquivo(datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)),
                backup.nome_arquivo(datetime(2026, 1, 2, 5, 4, tzinfo=timezone.utc)),
            ]
        )
        check(
            "nome ordena por tempo",
            nomes_ordenados[0].endswith("20260102T030400Z.tar.gz"),
            nomes_ordenados,
        )
        check("prefixo do bot", nomes_ordenados[0].startswith(backup.PREFIXO), nomes_ordenados)

        # mundo travado: o LevelDB segura o .ldb logo apos o stop e o tar
        # pegaria o arquivo pela metade, sem avisar
        class Travado:
            """um .ldb que so libera na segunda vez, como o SO faz."""

            def __init__(self) -> None:
                self.falhas = 0
                self.aberturas = 0

            def rglob(self, padrao):
                return [self]

            def is_file(self):
                return True

            def open(self, modo):
                return self

            def __enter__(self):
                self.aberturas += 1
                if self.falhas < 1:
                    self.falhas += 1
                    raise PermissionError(13, "Permission denied")
                return self

            def __exit__(self, *a):
                return False

        alvo = Travado()
        backup._confere_mundo(alvo, espera=0)
        check("repete quando o mundo trava", alvo.falhas == 1, alvo.falhas)
        check("conferiu todos os arquivos do mundo", alvo.aberturas == 2, alvo.aberturas)

        # e se nunca destrava, erro em vez de um tar pela metade
        class SempreTravado(Travado):
            name = "world"

            def __enter__(self):
                self.aberturas += 1
                raise PermissionError(13, "Permission denied")

        try:
            backup._confere_mundo(SempreTravado(), tentativas=2, espera=0)
            check("mundo travado da erro", False, "nao levantou")
        except backup.BackupError:
            check("mundo travado da erro", True)

        # data/state vazios: erro claro em vez de um tar de 0 byte
        vazio = Path(tempfile.mkdtemp())
        try:
            try:
                backup.empacota(vazio, vazio, tmp / "vazio.tar.gz")
                check("vazio da erro", False, "nao levantou")
            except backup.BackupError:
                check("vazio da erro", True)
        finally:
            shutil.rmtree(vazio, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ops_backup() -> None:
    """O caminho de erro do /backup: o servidor volta e a vigia volta a olhar."""
    print("backup: fluxo de erro do /backup")

    class _Docker:
        """DockerController que para e religa na hora, sem Docker nenhum."""

        def __init__(self) -> None:
            self.parou = False
            self.subiu = False
            self.diz = ""

        def say(self, texto: str) -> None:
            self.diz = texto

        def para(self) -> None:
            self.parou = True

        def liga(self) -> None:
            self.subiu = True

        def state(self):
            return SimpleNamespace(running=self.subiu)

        def pronto(self):
            return self.subiu, ""

    async def espera_pronta_falsa(ctx):
        """Sem ping e sem BOOT_TIMEOUT: o log ja vem pronto do _Docker."""
        return ctx.docker.subiu, "", SimpleNamespace(jogadores=0)

    async def cenario(empacota: str) -> tuple[bool, bool, bool, bool]:
        """Roda o cria_backup com um empacota que leva o caminho indicado.

        Devolve (deu backup, parou, religou, em_restart no fim). O ultimo e o
        que importa: em_restart seguro impede a vigia de avisar queda de
        verdade dali em diante, entao ele tem que voltar a False mesmo
        quando o empacotamento estoura.
        """
        docker = _Docker()
        pasta = Path(tempfile.mkdtemp())
        cfg = SimpleNamespace(
            announce_seconds=0,
            backups_dir=pasta,
            data_dir=Path("."),
            state_dir=Path("."),
            backup_local_keep=5,
            backup_dropbox_keep=3,
            dropbox=SimpleNamespace(token="", refresh_token="", app_key="", app_secret=""),
        )
        ctx = SimpleNamespace(config=cfg, docker=docker, em_restart=False)

        def falha(*a, **k):
            raise backup.BackupError("mundo travado")

        def ecopota(*a, **k):
            return 123, ["data/worlds"]

        original = backup.empacota
        backup.empacota = {"falha": falha, "ok": ecopota}[empacota]
        try:
            deu = False
            try:
                await ops.cria_backup(ctx, "teste")
                deu = True
            except backup.BackupError:
                pass
            return deu, docker.parou, docker.subiu, ctx.em_restart
        finally:
            backup.empacota = original
            shutil.rmtree(pasta, ignore_errors=True)

    original_espera = ops.espera_pronto
    ops.espera_pronto = espera_pronta_falsa
    try:
        # o caminho feliz: para, empacota, religa e devolve o relatorio
        deu, parou, subiu, vigia = asyncio.run(cenario("ok"))
        check("backup feliz devolve relatorio", deu, deu)
        check("parou antes de copiar", parou, parou)
        check("religou antes de devolver", subiu, subiu)
        check("vigia volta a olhar", not vigia, vigia)

        # o caminho que importa: o empacotamento estoura
        deu, parou, subiu, vigia = asyncio.run(cenario("falha"))
        check("falha nao devolve backup", not deu, deu)
        check("mesmo falhando, parou para copiar", parou, parou)
        check("religou o servidor depois da falha", subiu, subiu)
        check("falha tambem solta a vigia", not vigia, vigia)
    finally:
        ops.espera_pronto = original_espera


def test_firewall() -> None:
    """host/firewall.sh e' o que faz o 19132 sobreviver a reboot na OCI.

    Nao testamos iptables aqui (exige root e uma maquina de verdade). O que
    trava e' o que quebra em silencio: CRLF no shebang, faixa UDP divergente
    da documentada, e a recomendacao errada de SERVER_IP voltando na doc.
    """
    print("\n[firewall]")

    raiz = Path(__file__).resolve().parent.parent
    sh = raiz / "host" / "firewall.sh"
    unit = raiz / "host" / "mine-bedrock-firewall.service"

    check("host/firewall.sh existe", sh.is_file())
    check("host/mine-bedrock-firewall.service existe", unit.is_file())
    if not sh.is_file() or not unit.is_file():
        return

    bruto = sh.read_bytes()
    texto = bruto.decode("utf-8")

    # CRLF no shebang falha no boot com "bad interpreter: /bin/sh^M", e so
    # apareceria no proximo reboot. E o que o .gitattributes existe pra evitar.
    check("firewall.sh sem BOM", not bruto.startswith(b"\xef\xbb\xbf"))
    check("firewall.sh em LF (CRLF quebra o shebang)", b"\r\n" not in bruto)
    check("firewall.sh com shebang sh", texto.startswith("#!/bin/sh"))

    # Sem o -C antes do -I, rodar duas vezes duplica regra a cada boot.
    check("firewall.sh idempotente (iptables -C antes do -I)", "iptables -C INPUT" in texto)
    check("firewall.sh insere no topo (-I INPUT 1)", "-I INPUT 1" in texto)

    # A faixa UDP do firewall e a mesma que o server-udp-ports manda anunciar.
    achado = re.search(r'UDP_PORTS="\$\{UDP_PORTS:-([^}]*)\}"', texto)
    faixa_udp = achado.group(1).split() if achado else []
    check(
        "faixa UDP do firewall = 19133:19172 + 7551",
        faixa_udp == ["19133:19172", "7551"],
        f"veio {faixa_udp!r}",
    )
    check("firewall abre 19132/tcp (signalling)", "TCP_PORTS:-19132" in texto)

    servico = unit.read_text(encoding="utf-8")
    # /bin/sh explicito: ExecStart direto depende do bit de exec, que nao e
    # garantido num checkout, e a falha aparece so no boot.
    check("servico chama o script via /bin/sh", "ExecStart=/bin/sh /opt/mine-bedrock/firewall.sh" in servico)
    check("servico espera a rede subir", "network-online.target" in servico)
    check("servico habilita no boot", "WantedBy=multi-user.target" in servico)

    # A faixa tem que bater com a doc, senao o usuario libera um e o outro fecha.
    readme = (raiz / "README.md").read_text(encoding="utf-8")
    check("README documenta a mesma faixa UDP", "19133:19172/udp" in readme)
    check("README documenta o servico", "mine-bedrock-firewall" in readme)

    # server-ip e' endereco de BIND. Se ele voltar a ser recomendado, o BDS nem
    # sobe quando o IP publico nao e local, que e o caso de quase toda VPS NAT.
    compose = (raiz / "compose.yml").read_text(encoding="utf-8")
    exemplo = (raiz / ".env.example").read_text(encoding="utf-8")
    check("compose.yml sem SERVER_IP", "SERVER_IP" not in compose)
    check("compose nao define OPS que sobrescreve permissions.json", not re.search(r"^\s+OPS:", compose, re.M))
    check(".env.example nao define SERVER_IP", not re.search(r"^SERVER_IP=", exemplo, re.M))
    check("README nao ensina /config server-ip=", "/config server-ip=" not in readme)


if __name__ == "__main__":
    for teste in (
        test_detect_mcaddon,
        test_nome_de_traducao,
        test_zip_slip,
        test_apply_update,
        test_raknet,
        test_store,
        test_store_migracao_allowlist,
        test_auth,
        test_serverctl,
        test_promocao_confirma_reload,
        test_interpreta_config,
        test_nome_do_gamertag,
        test_config_persiste,
        test_console,
        test_dropbox,
        test_backup,
        test_ops_backup,
        test_firewall,
    ):
        teste()
    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} -> {falhas}")
        raise SystemExit(1)
    print("tudo certo")
