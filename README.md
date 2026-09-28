# Revolucao - Minecraft Bedrock no Docker, com bot de admin e add-ons

Servidor Bedrock com atualizacao automatica diaria e um bot de Telegram que
administra o servidor (config, lista de jogadores, reinicio) e instala, atualiza
e remove packs (.mcaddon/.mcpack).

```
Telegram ──> bot (docker.sock) ──> /data/behavior_packs, /data/resource_packs
                                          │
Jogadores ──TCP 19132 + UDP 19133-72────────────────────> mine-bedrock (VERSION=LATEST)
```

- `bds`: `itzg/minecraft-bedrock-server:stable` com `VERSION=LATEST`. Publica
  direto no host as duas etapas do nethernet: `TCP 19132` (signalling) e
  `UDP 19133-19172` (gameplay). Nao ha proxy no meio.
- `bot`: Python + aiogram, fala com o Docker pelo socket, faz o restart
  diario as 05:00 e responde so a quem tem o codigo de resgate (admin) ou uma
  chave de leitura.

## Estrutura

```
compose.yml            bds + bot
.env.example           copie para .env e preencha
data/                  mundo, packs e config do BDS (crie antes de subir)
state/bot.db           usuarios, chaves, config salva e auditoria do bot
assets/server-icon.png  icone (Bedrock nao usa; o bot manda no /status)
bot/app/               codigo do bot
tools/selftest.py      testes da logica pura (nao precisa de Docker)
```

## Instalar

```bash
cd /home/ubuntu/docker/mine-bedrock
cp .env.example .env
```

No `.env`:

| Variavel | O que e |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | token do @BotFather (obrigatorio) |
| `TELEGRAM_ADMIN_CLAIM_CODE` | codigo de resgate, minimo 8 caracteres (obrigatorio) |
| `PUID` / `PGID` | dono dos arquivos em `data/` (`id -u` / `id -g` do usuario) |
| `LEVEL_NAME` | nome da pasta do mundo em `data/worlds/` (so o 1o boot usa) |
| `UPDATE_HOUR` / `UPDATE_MINUTE` | horario do restart diario (padrao 05:00) |
| `BOOT_TIMEOUT` | segundos que o bot espera o jogo subir (padrao 240) |
| `BACKUP_KEEP` | versoes antigas guardadas de cada pack (padrao 3) |

Gere o codigo de resgate com `openssl rand -hex 6` e **guarde**: ele e a senha do
bot. Nao existe "primeiro a mandar vira admin".

Depois:

```bash
mkdir -p data state
docker compose config      # revisa o que vai subir
docker compose up -d --build
docker compose logs -f bot
```

O primeiro `/start` no Telegram mostra como entrar. Sem o codigo, o log do bot
explica o que faltou:

```bash
docker compose logs bot | tail -20
```

## Quem pode usar o bot

| Papel | Como entra | O que pode |
| --- | --- | --- |
| **admin** | `/start` + `TELEGRAM_ADMIN_CLAIM_CODE` | tudo: config, lista, negativos, permissoes, kick, reinicio, packs, log, auditoria |
| **leitura** | chave criada pelo admin + `/entrar CHAVE` | `/status` e `/packs` |

Regras que o bot aplica:

- Um admin so. Quem mandar o codigo de resgate assume o admin e o anterior vira
  leitor. Trocar o codigo no `.env` e reiniciar o bot e o jeito de expulsar quem
  roubar a sua conta do Telegram.
- Sem codigo, ninguem entra. O primeiro usuario a falar com o bot **nao** vira
  admin: ganha so a instrucao de como entrar.
- A chave de leitura vale para um Telegram so. O primeiro que usar vincula a
  conta; outra pessoa com a mesma chave recebe aviso para o admin revogar.
- O bot grava so o **hash** (SHA-256) das chaves. O texto aparece uma vez, na
  mensagem que cria a chave.
- Cinco tentativas erradas de resgate ou de chave travam o usuario por 10
  minutos.
- Guardar o `docker.sock` e o mesmo que dar root no host, entao o caminho do
  acesso e o unico caminho: nao ha porta do bot nem web.

## Conectar ao servidor

1. O `compose.yml` ja publica `19132/tcp` e a faixa `19133-19172/udp` direto no
   container do jogo. A faixa tem que bater com o `server-udp-ports` do
   `data/server.properties`; se mudar la, mude no compose tambem.
2. Liberar as duas no firewall da maquina e no painel do provedor. No UFW sao 2
   comandos, porque faixa e uma regra so:
   `ufw allow 19132/tcp` e `ufw allow 19133:19172/udp`
3. No Cloudflare, o registro do jogo fica **so DNS (cinza)**.

Os jogadores entram em **Adicionar servidor -> Endereço -> `seu.dominio`**.

> **Por que duas portas.** O BDS 1.26.52+ usa `transport=nethernet`, que nao e um
> protocolo so. O cliente primeiro abre um socket **TCP** em 19132 para um
> handshake HTTP de signalling (e o que o log do servidor anuncia:
> `Accepting clients on [::]:19132`), e so depois negocia por **UDP** o trafego
> de jogo via WebRTC. Como essa segunda etapa sorteia portas efemeras do
> sistema, atras de NAT ela nao funciona sem faixa fixa - e o que o
> `server-udp-ports=19133-19172:19133-19172` resolve.

## Sobre o icone

O `assets/server-icon.png` (bandeira vermelha + estrela branca) **nao aparece
na lista de servidores do Bedrock**: icone de servidor e coisa do Java Edition.
O Bedrock so mostra icone em servidores oficiais da Mojang, e o ping de um
servidor de terceiros nao manda imagem nenhuma (confirmado em
https://github.com/GeyserMC/Geyser/issues/1091 e na documentacao do
https://mcstatus.io/docs).

O que o icone faz aqui: o `/status` do bot manda ele junto com o status, e voce
pode usar em qualquer lugar (favicon do site, imagem do Discord, card de
divulgacao). Ele tambem fica montado em `/data/server-icon.png`, caso um dia
entre um Geyser/Java na frente.

Se quiser um icone **dentro** do jogo, o caminho e outro: cada resource pack
que voce mesmo empacota leva um `pack_icon.png` na raiz, e esse ai aparece na
tela de packs do cliente.

## Bot

### Admin

| Comando | O que faz |
| --- | --- |
| `/admin` | lista de tudo que existe |
| `/config` | menu de propriedades com o valor atual de cada uma |
| `/config <chave> <valor>` | muda na mao (ex.: `/config difficulty hard`) |
| `/config <chave> <valor> sim` | confirma as propriedades que pedem cuidado |
| `/config aplicar` | reescreve em `server.properties` o que o bot guardou |
| `/lista` | quem esta na allow-list e quem esta negado |
| `/permitir <gamertag> [xuid]` | adiciona na allow-list (liga a lista se preciso) |
| `/removerjogador <gamertag>` | tira da allow-list |
| `/negar <gamertag> [motivo]` | tira da lista, manda kick e marca como negado |
| `/permitido <gamertag>` | tira da lista de negados |
| `/ops <gamertag> <nivel> [xuid]` | permissoes do jogador (operator/member/visitor) |
| `/chutar <gamertag> [motivo]` | expulsa agora |
| `/chave <nome>` | cria uma chave de leitura e mostra o texto |
| `/chaves` | lista das chaves, com quem esta usando cada uma |
| `/revogar <n>` | revoga a chave `n` e tira o acesso de quem usava |
| `/pessoas` | quem tem acesso ao bot |
| `/reiniciar [motivo]` | reinicia o servidor (com aviso no jogo) |
| `/anunciar <texto>` | fala no chat do jogo |
| `/log [n]` | ultimas linhas do log (padrao 60, max 1000) |
| `/auditoria` | ultimas acoes registradas no banco |
| `/packs` | packs instalados |
| `/removerpack [nome]` | desinstala um pack (com confirmacao) |

O `/removerpack` sem argumento mostra a lista; clicar num pack ainda pede
confirmacao, e a remocao reinicia o servidor uma unica vez.

**Sobre "negar"**: o Bedrock nao tem lista de bloqueio. O que o bot faz e tirar o
jogador da allow-list, mandar `kick` e guardar o nome como negado (o `/permitir`
recusa ate voce usar `/permitido`). Com a allow-list ligada, isso bloqueia o
jogador de verdade. **Com a allow-list desligada, negar nao impede nada** - o
bot avisa e sugere `/config allow-list true`.

**Sobre as propriedades**: quem manda e o bot. Ele escreve em
`/data/server.properties` e guarda o valor tambem no `state/bot.db`, para
reaplicar depois de um boot ou de uma atualizacao da imagem (que reescreve o
arquivo). Por isso o `compose.yml` **nao** passa `GAMEMODE`, `DIFFICULTY`,
`MAX_PLAYERS`, `VIEW_DISTANCE`, `SERVER_NAME`, `ALLOW_LIST` nem
`DEFAULT_PLAYER_PERMISSION_LEVEL` para o container: variavel de ambiente ganha
a ultima palavra no boot. Mudou algo no `.env`? Mude pelo `/config` tambem.

Mudancas que o Bedrock so le no boot (dificuldade, distancia de visao, gamemode,
permissoes) pedem reinicio: o bot faz isso sozinho e avisa. Allow-list e
permissoes tem `reload` no console, sem derrubar o servidor.

Propriedades marcadas como **cuidado** (semente do mundo, porta do jogo,
`server-port`, `level-seed`, autenticacao...) nao mudam de primeira: o bot explica
o risco e so aplica no comando repetido com `sim` no fim, para ninguem derrubar
o servidor com um `/config` errado. `sim` sozinho continua sendo valor valido
(`/config allow-list sim` liga a lista).

### Leitura

| Comando | O que faz |
| --- | --- |
| `/status` | container, versao do BDS, jogadores online, mundo, packs |
| `/packs` | packs instalados |
| `/ajuda` | resumo |

Envio de add-on, `/log`, `/config`, `/reiniciar` e o resto ficam bloqueados para
quem so tem chave de leitura.

**Add-on**: mande o `.mcaddon`/`.mcpack`/`.zip` como documento. O bot mostra
os packs que encontrou e, para cada um, se da para **atualizar** um ja
instalado ou **instalar como novo**. Aplica tudo e reinicia uma vez no fim.

Detalhes que importam:

- Limite do Telegram: 20 MB por arquivo. Acima disso, suba por SFTP em
  `data/behavior_packs/` ou `data/resource_packs/` e reinicie o container.
- O zip e verificado antes de extrair: caminhos absolutos, `..`, symlinks e
  arquivos gigantes sao recusados.
- Atualizar guarda a versao antiga em `data/.addon-backups/<uuid>/<versao>/`.
- Cada `.mcaddon` pode trazer behavior pack + resource pack; o botao
  "Instalar como novo" faz as duas coisas de uma vez.

## Atualizacao diaria

O `agendador` do bot reinicia o container as 05:00 (fuso do `.env`). Como
`VERSION=LATEST`, o container reavalia a versao estavel no boot e baixa se
houver novidade. O bot avisa no Telegram com a versao antes -> depois e o ping
de verificacao.

O `pull_policy: daily` so vale quando o container e **recriado**
(`docker compose pull && docker compose up -d`). Depois disso, o bot
reaproveita a imagem nova no restart seguinte. O `/status` mostra a versao do
BDS e o `docker compose logs bds` mostra a do container.

Jogadores conectados saem no restart. Antes de reiniciar, o bot manda
`[bot] <motivo> - reiniciando em 20s` no chat (`ANNOUNCE_SECONDS`). O aviso do
proprio image esta em `STOP_SERVER_ANNOUNCE_DELAY=0` de proposito: com ele
ligado, o aviso sairia **depois** da espera do bot, e o servidor ainda ficaria
mais 20s parado antes do `stop`.

`stop_grace_period: 120s` no compose e o que protege o salvamento do mundo se o
`stop` demorar.

## Se ninguem consegue conectar

O nethernet tem duas etapas, e cada uma tem um sintoma proprio. Comece sempre
pela de dentro:

```bash
# 1. o BDS responde o signalling TCP?
docker exec mine-bedrock sh -c 'printf "GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n" | nc 127.0.0.1 19132'
# 2. as portas estao publicadas no host?
docker compose port bds 19132
netstat -an | findstr 19132
netstat -an | findstr ":191 "   # 40 linhas de UDP, 19133-19172
# 3. o gameplay usou a faixa fixa? (so com alguem conectado)
docker exec mine-bedrock sh -c "awk '{print \$2}' /proc/net/udp6"
```

Sintoma por etapa:

| o que voce ve | causa provavel |
| --- | --- |
| passo 1 nao responde | o BDS nao subiu, ou `server-port` mudou |
| passo 1 responde, mas da rede nao conecta | porta nao publicada, ou firewall/ingress do provedor bloqueando |
| a rede conecta e o jogo trava ao spawnar | **falta a faixa UDP**: `server-udp-ports` vazio ou divergente da faixa publicada, ou o firewall bloqueando 19133-19172 |
| passo 3 mostra porta efemera (`7FFE` etc) | `server-udp-ports` nao foi honrado; o gameplay sorteia porta e morre atras de NAT |
| tudo acima ok e o jogo nao entra | DNS/Cloudflare: o registro tem que ser **cinza** |

Nao use `mc-monitor` para diagnosticar: ele faz ping raknet e o nethernet nao
responde, entao da erro mesmo com o servidor perfeito.

## Manutencao

```bash
docker compose logs -f bds              # log do jogo
docker compose logs -f bot              # log do bot
docker exec -it mine-bedrock screen -r  # console do servidor
docker compose restart bot              # reinicia so o bot
docker compose pull && docker compose up -d   # imagem nova do BDS
```

Backup do mundo: `data/` inteiro (ou so `data/worlds/`) com o container
parado, ou pare a copia no meio para nao pegar o `level.db` pela metade:

```bash
docker compose stop bds
tar czf ~/bedrock-$(date +%F).tar.gz data/worlds
docker compose start bds
```

Backup dos packs: `data/behavior_packs/`, `data/resource_packs/` e
`data/.addon-backups/`. As versoes antigas de cada pack ficam em
`data/.addon-backups/<uuid>/<versao>/` (as ultimas `BACKUP_KEEP`).

Backup do acesso: `state/bot.db` tem os usuarios, as chaves (so hash), a config
que o bot controla e a auditoria. Perder esse arquivo **nao** abre o servidor
para ninguem, mas voce perde quem era admin e as chaves de leitura: a saida e
`docker compose stop bot`, apagar `state/bot.db` e mandar `/start` com o codigo
de novo.

Perdeu o Telegram do admin? Troque `TELEGRAM_ADMIN_CLAIM_CODE` no `.env`,
`docker compose up -d bot` e mande `/start` com o codigo novo no Telegram que
vcidir. Quem usava o codigo velho perde o admin.
