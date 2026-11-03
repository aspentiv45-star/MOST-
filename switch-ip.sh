#!/usr/bin/env bash
# MOST — переключить вход на новый IP основного сервера. Запускать НА ВХОДЕ (московском сервере).
#
# Когда IP основного сервера блокируют, у хостинга берут новый и выполняют:
#   sudo bash switch-ip.sh НОВЫЙ_IP           — проверит адрес из России и переключит вход
#   sudo bash switch-ip.sh НОВЫЙ_IP --force   — переключить без проверки
# Клиентам ничего менять не нужно: их конфиги ведут на этот вход.
set -euo pipefail

# Ubuntu после установки пакетов спрашивает (needrestart), какие службы перезапустить; в тихом режиме
# вопрос не виден и установка висит вечно — пусть перезапускает сама и ни о чём не спрашивает.
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1

die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите через sudo"
NEW="${1:-}"
[[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "использование: sudo bash switch-ip.sh НОВЫЙ_IP [--force]"
cd "$(dirname "$0")"

RULES=/usr/local/sbin/most-relay
[[ -f "$RULES" ]] || die "вход ещё не настроен (нет $RULES) — сначала relay.sh"
# Порты берём из текущей пересылки: «порты: 50967=443,50968».
PORTS="$(sed -n 's/^# MOST: .*порты: //p' "$RULES")"
[[ -n "$PORTS" ]] || die "не нашёл порты в $RULES"

if [[ "${2:-}" != "--force" ]]; then
  echo "==> Проверяю $NEW из России (20 попыток)"
  ok=0
  for _ in $(seq 20); do timeout 3 bash -c "</dev/tcp/$NEW/22" 2>/dev/null && ok=$((ok + 1)); done
  echo "    прошло: $ok из 20"
  [[ $ok -ge 19 ]] || die "адрес режут или он ещё не поднялся — смените IP в панели или подождите (--force — без проверки)"
fi

echo "==> Переключаю вход на $NEW (порты $PORTS)"
systemctl disable --now xray >/dev/null 2>&1 || true
bash relay.sh --to "$NEW:$PORTS" >/dev/null
command -v conntrack >/dev/null || apt-get install -y -qq conntrack >/dev/null 2>&1 || true
conntrack -F >/dev/null 2>&1 || true

# Туннель для российских сайтов (ru-node.sh) принимает подключения только с IP основного сервера.
if [[ -f /etc/amnezia/amneziawg/awg1.conf ]] && command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  RU_PORT="$(awk -F' *= *' '$1=="ListenPort"{print $2; exit}' /etc/amnezia/amneziawg/awg1.conf)"
  ufw allow from "$NEW" to any port "$RU_PORT" proto udp comment "MOST awg1" >/dev/null
fi

cat <<EOF

✅ Вход переключён на $NEW. Клиентам ничего делать не нужно — пусть выключат и включат VPN.

На ОСНОВНОМ сервере (зайдите уже по новому адресу: ssh root@$NEW) выполните:

  sudo sed -i 's/^ENDPOINT_HOST=.*/ENDPOINT_HOST=$NEW/' /etc/most/bot.env && sudo systemctl restart most-bot
EOF
