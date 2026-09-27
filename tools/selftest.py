"""Testes da logica pura: sem Telegram, sem Docker.

Roda em qualquer maquina:  python tools/selftest.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

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

        falso.BaseMiddleware = BaseMiddleware
        falso.Bot = Bot
        falso.__path__ = []  # permite "import aiogram.types"

        tipos = types.ModuleType("aiogram.types")

        class Message:  # usado no isinstance do AdminOnly
            pass

        tipos.Message = Message
        falso.types = tipos
        sys.modules["aiogram"] = falso
        sys.modules["aiogram.types"] = tipos

from app import addons  # noqa: E402
from app import auth  # noqa: E402
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
    print("store: usuarios, chaves, overrides, negados")
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

        st.nega("JogadorRuim", "grief", 2)
        check("negado registrado", st.esta_negado("jogadorruim"), st.negados())
        st.permite("JogadorRuim")
        check("negado liberado", not st.esta_negado("jogadorruim"))

        st.audita(2, "config", "difficulty=hard")
        reg = st.auditoria(5)
        check("auditoria gravada", bool(reg) and reg[0]["action"] == "config", reg)
        st.fecha()
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
    print("serverctl: server.properties, allowlist, permissions")
    tmp = Path(tempfile.mkdtemp())
    try:
        data = tmp / "data"
        data.mkdir()
        (data / "server.properties").write_text(
            "server-name=Revolucao\ngamemode=survival\ndifficulty=easy\n#comentario\n\n", encoding="utf-8"
        )
        (data / "allowlist.json").write_text(json.dumps([{"name": "Ze", "xuid": "2535453759792258"}]), encoding="utf-8")
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
        check("boolAceita sim", serverctl.valida(serverctl.POR_CHAVE["allow-list"], "sim") == "true")
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

        srv.set_prop("level-name", "Mundo Novo")
        check("nome do mundo com espaco", srv.nivel_do_mundo("world") == "Mundo Novo", srv.nivel_do_mundo("world"))

        novo = srv.add_allowlist("Ana")
        check("jogador adicionado", novo and srv.allowlist()[-1]["name"] == "Ana", srv.allowlist())
        check("chamada repetida nao duplica", srv.add_allowlist("Ana") is False)
        check("xuid preservado dos outros", srv.allowlist()[0]["xuid"] == "2535453759792258", srv.allowlist())

        log = "[2026] Player connected: Bia/1234567890123456"
        check(
            "xuid do log preenche allowlist por nick",
            srv.add_allowlist("Bia", log_txt=log) and srv.allowlist()[-1]["xuid"] == "1234567890123456",
            srv.allowlist(),
        )
        check(
            "entrada existente sem xuid ganha xuid",
            srv.add_allowlist("Ana", log_txt="[2026] Player connected: Ana/9999999999999999") is False,
            srv.allowlist(),
        )
        check("xuid da entrada existente foi atualizado", srv.allowlist()[1]["xuid"] == "9999999999999999", srv.allowlist())

        check("remocao funciona", srv.remove_allowlist("Ana") and srv.remove_allowlist("Bia") and len(srv.allowlist()) == 1)
        check("remover ausente nao quebra", srv.remove_allowlist("Fantasma") is False)
        check("allowlist continua json valido", json.loads((data / "allowlist.json").read_text(encoding="utf-8"))[0]["name"] == "Ze")

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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    check("sim sozinho e valor, nao confirmacao", inter(["allow-list", "sim"]) == ("allow-list", "sim", False))
    check("3 palavras com sim no fim e confirmacao", inter(["server-name", "para", "sim"]) == ("server-name", "para", True))
    check("sim e valor de verdade", serverctl.valida(serverctl.POR_CHAVE["allow-list"], "sim") == "true")

    perigosa = serverctl.POR_CHAVE["max-players"]
    check("perigosa pede confirmacao", conf(perigosa, False))
    check("perigosa aceita com sim", not conf(perigosa, True))
    check("prop sem cuidado nao pede nada", not conf(serverctl.POR_CHAVE["view-distance"], False))

    # o caminho feliz: validar so depois de decidir a confirmacao
    chave, bruto, ok = inter(["level-seed", "abc", "sim"])
    valor = serverctl.valida(serverctl.POR_CHAVE[chave], bruto)
    check("fluxo completo aplica", (chave, valor) == ("level-seed", "abc") and not conf(serverctl.POR_CHAVE[chave], ok))


if __name__ == "__main__":
    for teste in (test_detect_mcaddon, test_nome_de_traducao, test_zip_slip, test_apply_update, test_raknet, test_store, test_auth, test_serverctl, test_interpreta_config):
        teste()
    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} -> {falhas}")
        raise SystemExit(1)
    print("tudo certo")
