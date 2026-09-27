"""Desenha os textos do bot sem Telegram: util para conferir o layout.

Nao faz parte do bot. Roda com o codigo do container:

    docker run --rm -v ./data:/data -v ./state:/state --entrypoint python \\
        mine-bedrock-bot:latest /tmp/preview.py
"""

import asyncio
import sys

sys.path.insert(0, "/app")

from app import admin, handlers, ops  # noqa: E402
from app.config import Config  # noqa: E402
from app.ops import AppContext  # noqa: E402

SEP = "\n\n" + "=" * 60 + "\n\n"


async def main() -> None:
    cfg = Config.from_env()
    ctx = AppContext.build(cfg)

    print(handlers.HELP)
    print(SEP)
    print(admin.AJUDA_ADMIN)
    print(SEP)
    print(ops.lista_packs(ctx))
    print(SEP)
    print("\n".join(await ops.status_lines(ctx)))


asyncio.run(main())
