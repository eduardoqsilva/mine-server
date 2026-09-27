# nginx: expose o Minecraft na mesma maquina do site

O container do jogo **nao publica porta nenhuma**. Quem fala com a internet e o
nginx, na rede `proxy`. Com `transport=nethernet` (BDS 1.26.52+) sao **duas**
encaminhamentos, porque o protocolo tem duas etapas:

| etapa | tipo | porta | o que e |
| --- | --- | --- | --- |
| signalling | **TCP** | 19132 | handshake HTTP, e o "Accepting clients on [::]:19132" do log |
| gameplay | **UDP** | 19133-19172 | trafego WebRTC/ICE, faixa fixada por `server-udp-ports` |

A faixa UDP e o ponto que costuma faltar: sem ela o handshake TCP completa e o
jogo trava ao entrar, porque o WebRTC sorteia portas efemeras que nao voltam
atraves do NAT.

Tres coisas precisam mudar no nginx que voce ja tem.

## 1. Descobrir o nginx.conf atual

```bash
mkdir -p /home/ubuntu/docker/nginx/stream.d
cd /home/ubuntu/docker/nginx
docker exec nginx cat /etc/nginx/nginx.conf > nginx.conf
docker exec nginx cat /etc/nginx/modules/ngx_stream_module.so > /dev/null && echo "stream OK"
```

Se aparecer `stream OK`, o modulo esta compilado e o passo 2 funciona.
Se der erro, o build do nginx nao tem stream: pule para o plano B no fim.

## 2. Adicionar o bloco stream no nginx.conf

Abra `nginx.conf` e **sume** as duas partes (nao troque o arquivo inteiro, o
seu tem as configs de TLS/Cloudflare):

1. junto dos outros `load_module`, garanta:

```nginx
load_module /etc/nginx/modules/ngx_stream_module.so;
```

2. no fim do arquivo, **depois** do bloco `http { ... }`:

```nginx
stream {
    resolver 127.0.0.11 valid=10s ipv6=off;

    server {
        listen 0.0.0.0:19132;              # TCP: signalling
        set $jogo mine-bedrock:19132;
        proxy_pass $jogo;
        proxy_connect_timeout 5s;
        proxy_timeout 300s;
    }

    server {
        listen 19133-19172 udp reuseport;   # UDP: gameplay WebRTC
        set $jogo_udp mine-bedrock:19133-19172;
        proxy_pass $jogo_udp;
        proxy_responses 0;
        proxy_timeout 3600s;
    }
}
```

O mesmo conteudo esta em `nginx.conf.stream` e `stream.d/minecraft.conf`.

Repare no que **nao** esta no bloco TCP: o `proxy_responses 0` fica so no UDP.
Ele existe para protocolos sem handshake (o raknet antigo, que nao tem SYN) e
no TCP atrapalharia o HTTP.

Por que `resolver` + `$jogo`: com um nome literal no `proxy_pass`, o nginx
resolve o DNS **uma vez no boot** e falha (`host not found in upstream`) se o
container do jogo ainda nao subiu. Com variavel, resolve a cada conexao.

O `stream.d/minecraft.conf` tem o mesmo `server {}` **sem** o `stream {}` em
volta, porque ele ja entra dentro do bloco. Escolha um dos dois jeitos, nunca os
dois (o mesmo `server` em dois arquivos na mesma porta faz o nginx nao subir):

- colar o bloco `stream { ... }` inteiro no fim do `nginx.conf` (e ignore o
  `stream.d`), ou
- manter o `stream.d` montado e escrever no `nginx.conf`:

```nginx
stream {
    include /etc/nginx/stream.d/*.conf;
    # ... o resto do seu bloco stream ...
}
```

## 3. Recriar o nginx com a porta e os mounts

No compose de `/home/ubuntu/docker/nginx`, acrescente (veja
`compose.yaml.exemplo` para o arquivo inteiro):

```yaml
    ports:
      - "19132:19132/tcp"              # signalling
      - "19133-19172:19133-19172/udp"   # gameplay WebRTC
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./stream.d:/etc/nginx/stream.d:ro
```

```bash
cd /home/ubuntu/docker/nginx
docker compose config
docker compose up -d nginx
docker exec nginx nginx -t
```

O `nginx -t` roda **depois** do `up -d`, porque o `nginx.conf` novo so entra no
container quando ele recria. Se o teste falhar, o container pode ter parado com
o site no ar: confira `docker compose logs nginx`, corrija o arquivo e rode
`docker compose up -d nginx` de novo.

Confirme as duas portas. No `nginx:alpine` **nao tem `ss`**, use `netstat` (a
faixa UDP sai repetida, uma linha por porta, e 40 linhas e o certo):

```bash
docker exec nginx netstat -lnt | grep 19132    # esperado: 1 linha, 0.0.0.0:19132
docker exec nginx netstat -lnu | grep -c 191  # esperado: 40 (19133..19172)
```

## 4. Liberar no host

Faixa no `ufw` e uma regra so, nao 40:

```bash
sudo ufw allow 19132/tcp
sudo ufw allow 19133:19172/udp
sudo nft list ruleset | grep 19132   # conferir
```

E no painel do provedor (Oracle Cloud / Hetzner / etc.) duas regras de ingress:
`TCP 19132` e `UDP 19133-19172`. Sem a segunda o handshake passa e o jogo
trava ao entrar.

## 5. DNS

O registro do jogo tem que ser **so DNS (cinza), nunca proxied (laranja)**:
Cloudflare nao faz proxy de UDP de jogo, e o lodo do laranja faz o
Bedrock tentar conectar na porta 80/443 e falhar.

## Testando

### 1. O BDS direto (sem nginx)

De dentro do container do jogo, no signalling TCP:

```bash
docker exec mine-bedrock sh -c 'printf "GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n" | nc 127.0.0.1 19132'
```

Qualquer resposta HTTP (ate `404`) prova que o handshake responde. `0` sem
resposta e o BDS nao subiu direito.

O `mc-monitor` da imagem **nao serve para isso anymore**: ele faz ping raknet e
o nethernet nao responde, entao sempre da erro mesmo com o servidor perfeito.

### 2. Atraves do nginx, da propria VPS

```bash
docker exec nginx sh -c 'printf "GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n" | nc 127.0.0.1 19132'
```

### 3. Pela internet, de fora

```bash
nc -vz SEU_DOMINIO 19132          # tem que conectar em TCP
```

E o teste que só o jogador faz: entrar no jogo. O handshake passa e o jogo
trava ao spawnar = falta a faixa UDP.

### Como saber se a faixa UDP esta obeying

Com um jogador conectado, o socket de gameplay tem que aparecer dentro da
faixa. Do host:

```bash
docker exec mine-bedrock sh -c "awk '{print \$2}' /proc/net/udp6 | head -20"
```

Procure portas entre `19133` e `19172` (`4A4D` ate `4AB8` em hex). Se aparecer
algo como `7FFE` (`32766`, efemera) em vez disso, o `server-udp-ports` nao foi
honrado e o gameplay nunca vai passar pelo seu proxy.

## Plano B: sem modulo stream no seu nginx

Se o `ngx_stream_module.so` nao existir no seu build, nao mexa no seu nginx: suba
um nginx **separado**, so para o jogo, e publique as duas portas nele. O seu
site continua exatamente como esta.

```yaml
services:
  mc-proxy:
    image: nginx:alpine
    container_name: mc-proxy
    restart: unless-stopped
    ports:
      - "19132:19132/tcp"
      - "19133-19172:19133-19172/udp"
    volumes:
      - ./mc-nginx.conf:/etc/nginx/nginx.conf:ro
      - ./stream.d:/etc/nginx/stream.d:ro
    networks:
      - proxy
```

Salve o `nginx.conf` dele como `mc-nginx.conf`, com o minimo possivel: um
`stream {}` com o `include` do arquivo que ja existe no repo:

```nginx
events {}
stream {
    include /etc/nginx/stream.d/minecraft.conf;
}
```

Um proxy assim e stateless, entao resolve o nethernet inteiro - diferente do
raknet, que chegava a precisar de gambiarra de pong.
