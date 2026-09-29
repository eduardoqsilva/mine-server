#!/bin/sh
# Abre no INPUT da maquina as portas do nethernet, e nada mais.
#
# Por que este script existe: em imagem Ubuntu da OCI o cloud-init escreve um
# iptables local que so abre 22/80/443 e fecha o INPUT com
#     -A INPUT -j REJECT --reject-with icmp-host-prohibited
# Tudo que vem depois dessa regra nunca e alcancado, e o ping responde "host
# proibido" em vez de simplesmente nao responder -- sinal de bloqueio local,
# nao de pacote perdido no caminho. Liberar a porta na security list da OCI nao
# resolve, porque o REJECT e local. E o cloud-init roda uma vez so, entao
# regra digitada a mao desaparece no reboot; este script e a parte que volta.
#
# Idempotente: pode rodar quantas vezes quiser, antes ou depois do docker.
#   sudo sh /opt/mine-bedrock/firewall.sh
#
# As portas sao sobrescritiveis, porque precisam seguir o SERVER_UDP_PORTS
# do .env (a faixa que o nethernet anuncia):
#   TCP_PORTS=19132 UDP_PORTS="19133:19172 7551" sudo -E sh firewall.sh
set -eu

TCP_PORTS="${TCP_PORTS:-19132}"
UDP_PORTS="${UDP_PORTS:-19133:19172 7551}"

abrir() {
    # -C sai com codigo diferente de zero quando a regra ainda nao existe.
    # Se ja existe, nao inserimos de novo: rodar duas vezes nao duplica nada.
    if iptables -C INPUT "$@" -j ACCEPT 2>/dev/null; then
        return 0
    fi
    # -I INPUT 1: precisa entrar ANTES do REJECT do cloud-init, nunca depois.
    iptables -I INPUT 1 "$@" -j ACCEPT
    echo "  + $*"
}

for porta in $TCP_PORTS; do
    abrir -p tcp --dport "$porta"
done

for portas in $UDP_PORTS; do
    abrir -p udp --dport "$portas"
done

echo "--- ACCEPT no INPUT ---"
iptables -L INPUT -n | grep -- 'ACCEPT' || true
