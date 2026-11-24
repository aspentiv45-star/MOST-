#!/usr/bin/env bash
# Run on the Germany server with the Netherlands public IP and the dedicated SSH public key.
set -euo pipefail
umask 077
die() { printf '%s\n' "$*" >&2; exit 1; }
[[ $EUID == 0 && $# == 2 ]] || die "sudo bash germany-setup.sh NL_PUBLIC_IPV4 'ssh-ed25519 AAAA...'"
cd "$(dirname "$0")"
nl_ip=$1
read -r key_type key_data _ <<< "$2"
[[ "$key_type" == ssh-ed25519 && "$key_data" =~ ^[A-Za-z0-9+/]{68}$ ]] || die "Нужен публичный ключ Ed25519"
python3 -c 'import ipaddress, sys; ipaddress.IPv4Address(sys.argv[1])' "$nl_ip"
[[ ! -f /etc/most/bot.env ]] || die "Запустите на новом сервере в Германии"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq software-properties-common iptables openssh-server
if ! command -v awg >/dev/null || ! modinfo amneziawg >/dev/null 2>&1; then
  add-apt-repository -y ppa:amnezia/ppa
  apt-get install -y -qq "linux-headers-$(uname -r)" amneziawg amneziawg-tools
fi
modprobe amneziawg

wan=$(ip -4 route show default | awk 'NR == 1 {print $5}')
[[ "$wan" =~ ^[A-Za-z0-9_.:-]+$ ]] || die "Не найден внешний интерфейс"
config=/etc/amnezia/amneziawg/awg-de.conf
install -d -m 0700 /etc/amnezia/amneziawg
if [[ ! -e "$config" ]]; then
  private_key=$(awg genkey)
  read -r h1 h2 h3 h4 < <(python3 -c 'import random; print(*random.SystemRandom().sample(range(5, 2**31 - 1), 4))')
  cat >"$config" <<EOF
[Interface]
PrivateKey = $private_key
Address = 10.8.0.1/22
ListenPort = 51820
MTU = 1280
Jc = 4
Jmin = 50
Jmax = 120
S1 = 30
S2 = 70
H1 = $h1
H2 = $h2
H3 = $h3
H4 = $h4
PostUp = iptables -I FORWARD -i %i -o $wan -j ACCEPT; iptables -I FORWARD -i $wan -o %i -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT; iptables -t nat -A POSTROUTING -s 10.8.0.0/22 -o $wan -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -o $wan -j ACCEPT; iptables -D FORWARD -i $wan -o %i -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT; iptables -t nat -D POSTROUTING -s 10.8.0.0/22 -o $wan -j MASQUERADE
EOF
fi
printf 'net.ipv4.ip_forward=1\n' >/etc/sysctl.d/90-most-germany.conf
sysctl -p /etc/sysctl.d/90-most-germany.conf
install -m 0755 germany-peer-sync.py /usr/local/sbin/most-germany-peer-sync

cat >/etc/systemd/system/most-germany-expire.service <<'EOF'
[Unit]
Description=Expire the MOST Germany peer lease
After=awg-quick@awg-de.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/most-germany-peer-sync expire
EOF
cat >/etc/systemd/system/most-germany-expire.timer <<'EOF'
[Unit]
Description=Check the MOST Germany peer lease

[Timer]
OnBootSec=15s
OnUnitActiveSec=15s
AccuracySec=1s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now awg-quick@awg-de.service most-germany-expire.timer
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
  ufw allow 51820/udp
fi

install -d -m 0700 /root/.ssh
authorized=/root/.ssh/authorized_keys
touch "$authorized"
chmod 600 "$authorized"
new_auth=$(mktemp /root/.ssh/authorized_keys.XXXXXX)
sed '/ most-germany-sync$/d' "$authorized" >"$new_auth"
printf 'restrict,from="%s",command="/usr/local/sbin/most-germany-peer-sync" %s %s most-germany-sync\n' \
  "$nl_ip" "$key_type" "$key_data" >>"$new_auth"
mv "$new_auth" "$authorized"
printf '\nСкопируйте эту публичную строку в Termius на сервере в Нидерландах:\n'
cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub
printf 'Германия готова. Разрешите 51820/UDP также в файрволе хостинга.\n'
