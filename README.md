# Revolucao - Minecraft Bedrock no Docker, com bot de admin e add-ons

Servidor Bedrock com atualizacao automatica diaria e um bot de Telegram que
administra o servidor (config, lista de jogadores, reinicio) e instala, atualiza
e remove packs (.mcaddon/.mcpack).

```
Telegram ──> bot (docker.sock) ──> /data/behavior_packs, /data/resource_packs
                                          │
Jogadores ──TCP 19132 + UDP 19133-72────────────────────> mine-bedrock (VERSION=LATEST)
```

- `bds`: `itzg/minecraft-bedrock-server:stable` com `VERSION=LATEST`. Roda em
  `network_mode: host`, entao escuta direto na interface da maquina as duas
  etapas do nethernet: `TCP 19132` (signalling) e `UDP 19133-19172` (gameplay).
  Nao ha proxy no meio e nao ha `ports:` no compose.
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
ufw allow 19132/tcp; ufw allow 7551/udp; ufw allow 19133:19172/udp
docker compose config      # revisa o que vai subir
docker compose up -d --build
docker compose logs -f bot
```

As tres regras de firewall sao obrigatorias, nao um extra: o `bds` roda em
`network_mode: host`, entao quem precisa abrir a porta e o host, e sem elas o
servidor sobe perfeito e ninguem entra. O painel do provedor costuma ter um
firewall proprio, que e separado deste.

O primeiro `/start` no Telegram mostra como entrar. Sem o codigo, o log do bot
explica o que faltou:

```bash
docker compose logs bot | tail -20
```

Para confirmar que subiu certo, sem depender do Telegram:

```bash
docker inspect -f '{{.HostConfig.NetworkMode}}' mine-bedrock   # host
curl -s http://127.0.0.1:19132/v1/join                        # JSON
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

1. O `compose.yml` roda o `bds` em `network_mode: host`, entao o container
   escuta direto no host: nao existe bloco `ports:` e nao existe mapeamento.
   A faixa UDP tem que bater com o `server-udp-ports` do
   `data/server.properties`; se mudar la, mude no firewall tambem.
2. Liberar no firewall da maquina e no painel do provedor. No UFW:
   `ufw allow 19132/tcp`, `ufw allow 7551/udp` e `ufw allow 19133:19172/udp`.
3. No Cloudflare, o registro do jogo fica **so DNS (cinza)**.

Os jogadores entram em **Adicionar servidor -> Endereço -> `seu.dominio`**.

> **Por que `network_mode: host` e nao `ports:`.** O BDS 1.26.52+ usa
> `transport=nethernet`, que nao e so "abrir a porta". O cliente primeiro abre
> um socket **TCP** em 19132 para um handshake HTTP de signalling (e o que o
> log do servidor anuncia: `Accepting clients on [::]:19132`), e so depois
> negocia por **UDP** o trafego de jogo via WebRTC.
>
> Nessa segunda etapa o servidor sorteia candidatos ICE a partir das
> interfaces que ele enxerga e **anuncia um deles** pelo servico de signalling
> da Mojang - o cliente tenta aquele endereco, ele nao pergunta. Em rede bridge
> ele enxerga so a interface do container (`172.22.0.x`) e anuncia isso, que
> ninguem da internet alcanca. O sintoma e enganoso: o `/v1/join` responde JSON,
> o servidor aparece na lista, e a conexao morre em `Door` /
> `InitialConnection-NN` **sem nenhum evento no console do BDS**.
>
> Por isso nao adianta publicar porta: `ports:` muda onde o pacote chega, nao
> o endereco que o servidor anuncia. Todo config que funciona com esse
> transporte usa rede host. O `UDP 7551` ja vem aberto sozinho em qualquer
> config (e' porta fixa do NetherNet, nao a que o `server-udp-ports` move), e o
> socket de gameplay so aparece no `/proc/net/udp6` enquanto alguem esta
> conectando - entao a faixa so da para conferir com o jogo aberto.
>
> **VPS atras de NAT (IP publico nao aparece no `ip addr`).** E' o caso da
> OVH Public Cloud: a maquina tem so o IP privado na interface e o publico e um
> NAT 1:1 no edge. A rede host resolve o problema de *interface*, mas o
> nethernet passa a anunciar o IP privado, que ninguem de fora alcanca. A
> correcao nao e mexer no `server-ip` (que e endereco de *bind*: se voce
> preencher com um IP que nao e local, o BDS nem sobe), e sim no
> `server-udp-ports`, dizendo o IP publico:
>
> ```
> /config server-udp-ports=<IP-PUBLICO>:19133-19172:19133-19172
> ```
>
> Continua valendo o 1:1 (externo tem que ser igual a interno). Se a faixa
> larga nao funcionar, liste portas avulsas:
> `<IP-PUBLICO>:19133:19133,<IP-PUBLICO>:19134:19134`.
> E o edge do provedor precisa deixar passar a faixa UDP, senao nao adianta.

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
| `/console <comando>` | manda um comando no console do BDS e devolve a resposta |
| `/backup` | salva o mundo de agora e devolve o link do Dropbox |
| `/backups` | o que existe de backup, sem copiar nada |
| `/log [n]` | ultimas linhas do log (padrao 60, max 1000) |
| `/auditoria` | ultimas acoes registradas no banco |
| `/packs` | packs instalados |
| `/removerpack [nome]` | desinstala um pack (com confirmacao) |

O `/removerpack` sem argumento mostra a lista; clicar num pack ainda pede
confirmacao, e a remocao reinicia o servidor uma unica vez.

## Backup

O `/backup` salva o estado de agora: o mundo, os packs instalados, a config
(`server.properties`, allow-list, permissions) e o `bot.db` do bot. Ele
**derruba o servidor por cerca de um minuto**, avisa o jogo antes e volta
sozinho — se algo der errado, o container é religado de qualquer jeito.

O que não entra no backup, de propósito:

- **o binário do BDS** (`bedrock_server-*`): são 245 MB dos 411 MB do `/data`,
  e o `bedrock-entry.sh` rebaixa sozinho no boot
- os `server.properties.*.bak`: o arquivo atual já vai, e o histórico não
  vale 16 KB de banda
- o `content_log.txt`: é log, e o próprio bot tem `/log`

O tar é `.tar.gz` com prefixo `data/` e `state/`, então restaurar é extrair
por cima do volume. Formato de nome `mine-bedrock-<AAAAMMDD>T<hhmm>Z.tar.gz`,
em UTC, para ordenar por tempo mesmo que o servidor mude de fuso.

**Onde cada coisa fica:**

| Onde | Quantos | Quem apaga |
| --- | --- | --- |
| `./state/backups/atuais` (VPS) | 5 (`BACKUP_LOCAL_KEEP`) | o mais velho a cada backup novo |
| `/backups` (Dropbox) | 3 (`BACKUP_DROPBOX_KEEP`) | o mais velho a cada upload novo |

A rotação é por contagem, não por data: o número de arquivos fica sempre no
teto e o disco não cresce. A limpeza no Dropbox só toca em arquivo que começa
com `mine-bedrock-`, então se você jogar outra coisa na pasta `/backups` do
Dropbox ela fica intacta.

**Backup automático:** sábado às 04:00, uma hora antes do auto-update das
05:00 para os dois reinícios não caírem no mesmo dia colados. Ajuste com
`BACKUP_DAY` (0=segunda … 6=domingo), `BACKUP_HOUR` e `BACKUP_MINUTE`. O
servidor estar parado no horário pula o backup e avisa em vez de esperar.

**O link:** o arquivo vai para o Dropbox e o bot responde com um link
compartilhado permanente — o temporário do Dropbox expira em 4 horas, o que
não serve para olhar o backup no dia seguinte. Se o Dropbox estiver fora do
ar, o backup local é gravado do mesmo jeito e o bot te diz o caminho para
copiar por SSH: perder a nuvem não custa o backup.

**Cuidado com o link:** ele é criado com visibilidade pública, que é o padrão
da API do Dropbox. O `.tar.gz` tem o mundo inteiro e o `bot.db` com a lista de
admins e o histórico de auditoria, então quem tiver a URL baixa o backup —
vale não repassar o link. Se quiser travar isso, dá para pedir um link com
senha em vez de público.

O `/backup` precisa de credencial no `.env` (`DROPBOX_REFRESH_TOKEN`,
`DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`). O passo a passo para gerar está no
`.env.example` — ele lista as cinco permissões de escopo que a app precisa.
Para conferir se estão todas de pé, sem esperar um backup de verdade:

```bash
python tools/dropbox_check.py
```

O comando começa perguntando ao próprio Dropbox quais escopos o token
carrega. Isso importa porque "permissão faltando" quase nunca é problema de
token: quem decide a lista de escopos é a configuração da app, e um token
recém-criado sai com a mesma lista de escopos do token anterior. Se o
`dropbox_check.py` mostrar que o token não tem nenhum dos cinco, o conserto
está na aba **Permissions** da app — não em trocar o código de novo.

Sem credencial o resto tudo funciona, menos o link.

**Sobre a credencial:** o `refresh_token` é o que o bot usa, e ele não
expira. O `access_token` dura 4 horas — se você colocar só ele no `.env`, o
backup funciona nas primeiras horas e depois falha em silêncio, bem longe de
um backup. Por isso o `/backups` mostra qual dos dois está em uso.

E não procure o `refresh_token` no App Console: o botão **Generate** da seção
OAuth 2 da app só emite um access token, e é para isso que ele serve. O
refresh token não tem botão — ele aparece na resposta da troca do código de
autorização, que é o que o `tools/dropbox_token.py` faz por você:

```bash
python tools/dropbox_token.py APP_KEY APP_SECRET CODIGO --salvar
```

O `--salvar` faz o script escrever as três linhas no `.env` sozinho, e é o
jeito recomendado: colar na mão é exatamente onde esse processo costuma
falhar, com o token velho ficando no arquivo sem ninguém perceber. Sem a
flag, o script só imprime as linhas para você colar.

O `CODIGO` vem da URL de autorização que o `.env.example` traz, com
`token_access_type=offline` — sem esse parâmetro o Dropbox não devolve
refresh token nenhum.

**Sobre o servidor parar:** copiar um mundo enquanto o BDS escreve nele pode
produzir um arquivo que abre sem erro e só falha na hora de restaurar. Por
isso a ordem é sempre avisar → parar → copiar → subir. Se o mundo ainda
estiver travado pelo LevelDB logo após o stop, o bot tenta de novo três
vezes e, se não conseguir, **falha em vez de arquivar um mundo pela metade**.

**Sobre restaurar:** não existe comando de restauração ainda. Para voltar um
backup, pare o servidor e extraia o tar por cima do `./data` e do `./state`:

```bash
docker compose stop bds
tar -xzf state/backups/atuais/mine-bedrock-AAAAMMDDThhmmZ.tar.gz -C .
docker compose start bds
```

Restaure também o `state/bot.db` se quiser voltar as chaves de acesso, os
overrides do `/config` e a auditoria.

**Sobre o `/console`**: e a vala de escape do `/config`. Qualquer palavra que o
BDS aceite vai direto pro console — `list`, `help`, `tps`,
`gamerule doDaylightCycle false`, `time set day`, `whitelist on`.

```
/console list
/console tps
/console gamerule doFireTick false
```

Como funciona: o `send-command` da imagem nao e um cliente de console, ele
escreve o comando no stdin do processo (`/proc/<pid>/fd/0`) e sai sem responder
nada. A resposta sai no stdout do container, ou seja, no log. Por isso o bot
conta as linhas do log antes de mandar, espera a resposta parar de crescer (ate
3s) e devolve so as linhas novas, sem o carimbo de tempo e sem o eco do proprio
comando. Comando que nao fala nada (`save-resume`, `stop`) espera os 3s e
responde "o console nao devolveu nada" — isso e o comando funcionando, nao
falha. Para derrubar o servidor use `/reiniciar`, que avisa o jogo antes.

**Sobre "negar"**: o Bedrock nao tem lista de bloqueio. O que o bot faz e tirar o
jogador da allow-list, mandar `kick` e guardar o nome como negado (o `/permitir`
recusa ate voce usar `/permitido`). Com a allow-list ligada, isso bloqueia o
jogador de verdade. **Com a allow-list desligada, negar nao impede nada** - o
bot avisa e sugere `/config allow-list true`.

**Sobre as propriedades**: quem manda e o bot. Ele escreve em
`/data/server.properties` **e** guarda o valor na tabela `overrides` do
`state/bot.db`. Sao duas camadas de proposito: o arquivo e o que o BDS le, e o
`bot.db` e a memoria do bot, que sobrevive a um `docker compose up -d --build`.
A cada 60s o `reconciliador` compara os dois e reescreve no arquivo o que
divergiu, entao o que o admin gravou no Telegram nao se perde.

A imagem **nao** reescreve o `server.properties` inteiro: o `bedrock-entry.sh`
so passa para o arquivo as propriedades que tem variavel de ambiente
**setada** (`set-property --bulk`), e o que nao tem fica como o bot deixou.
Por isso o `compose.yml` nao passa `GAMEMODE`, `DIFFICULTY`, `MAX_PLAYERS`,
`VIEW_DISTANCE`, `SERVER_NAME`, `ALLOW_LIST` nem
`DEFAULT_PLAYER_PERMISSION_LEVEL`: se passasse, a variavel de ambiente
ganharia a ultima palavra no boot. Mudou algo no `.env`? Mude pelo `/config`
tambem.

A unica excecao e o **`level-name`**: o `LEVEL_NAME` do compose e o que escolhe
`worlds/<nome>` e o que escreve `level-name` no arquivo, entao la quem manda e o
compose. O `/config level-name` avisa e grava do mesmo jeito, mas so vale depois
de mudar o `LEVEL_NAME` e mover a pasta do mundo.

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
# 1. o BDS responde o signalling TCP? (o /v1/join e' o endpoint do nethernet)
curl -s http://127.0.0.1:19132/v1/join
# 2. o bds esta em rede host mesmo? (tem que aparecer, e o "Server started." no log)
docker inspect -f '{{.HostConfig.NetworkMode}}' mine-bedrock
# 3. as portas estao abertas no firewall da maquina?
ss -lntup | grep -E ':(19132|7551|1913[3-9]|19[1-6][0-9]|1917[0-2])\b'
# 4. o gameplay usou a faixa fixa? (so da pra ver com alguem conectando)
docker exec mine-bedrock sh -c "awk '{print \$2}' /proc/net/udp6"
```

Sintoma por etapa:

| o que voce ve | causa provavel |
| --- | --- |
| passo 1 nao responde | o BDS nao subiu, ou `server-port` mudou |
| passo 1 responde, mas da rede nao conecta | `19132/tcp` fechada no firewall/ingress do provedor |
| `NetworkMode` do container != `host` | volte ao `compose.yml`: em bridge o nethernet anuncia o IP privado do container e nenhum ajuste de porta resolve |
| a rede conecta e o jogo trava ao spawnar | **falta a faixa UDP**: `server-udp-ports` vazio ou divergente da faixa liberada, ou o firewall bloqueando 19133-19172 |
| passo 4 mostra porta efemera (`7FFE` etc) | `server-udp-ports` nao foi honrado; o gameplay sorteia porta e morre atras de NAT |
| so da rede local funciona, de fora nao | deve ser NAT/ingress do provedor: teste com `SERVER_IP=<ip-publico>` no `.env` |
| tudo acima ok e o jogo nao entra | DNS/Cloudflare: o registro tem que ser **cinza** (Cloudflare nao proxya UDP) |

Duas regras do `server-udp-ports` que valem no BDS 1.26.51+, e que nao dao erro
quando estao erradas - o servidor sobe e loga `Server started.` normalmente:

1. **So funciona 1:1.** `externo` tem que ser igual a `interno`
   (`19133-19172:19133-19172` ok; `61226:19133` abre o servidor e nunca escuta).
2. **Precisa estar no arquivo antes do boot.** O BDS le `server.properties` uma
   vez so. Mudou la, tem que reiniciar o `bds`.

> Nao use `mc-monitor` para diagnosticar: ele faz ping raknet e o nethernet nao
> responde, entao da erro mesmo com o servidor perfeito. Use o `/v1/join`.

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
