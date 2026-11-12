#!/usr/bin/env bash
# Бот друга (вход в закрытый канал за звёзды) — отдельной службой, от отдельного пользователя,
# без доступа к VPN, ключам и базе aspentiv. Запуск: sudo bash friend-bot/setup.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "запустите через sudo"; exit 1; }
cd "$(dirname "$0")"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

ENV=/etc/friendbot/bot.env
if [[ ! -f "$ENV" ]]; then
  mkdir -p /etc/friendbot
  printf '# Токен бота от @BotFather\nBOT_TOKEN=\n# Цена входа в звёздах\nPRICE_STARS=15\n' >"$ENV"
  chmod 600 "$ENV"
  echo "Создан $ENV — впишите токен (sudo nano $ENV) и запустите скрипт снова."
  exit 0
fi
grep -qE '^BOT_TOKEN=.+' "$ENV" || { echo "В $ENV не указан BOT_TOKEN"; exit 1; }

id friendbot >/dev/null 2>&1 || useradd --system --home /opt/friendbot --shell /usr/sbin/nologin friendbot
mkdir -p /opt/friendbot
cp bot.py /opt/friendbot/bot.py
command -v python3 >/dev/null && python3 -c 'import venv' 2>/dev/null || apt-get install -y -q python3-venv
[[ -x /opt/friendbot/venv/bin/python ]] || python3 -m venv /opt/friendbot/venv
/opt/friendbot/venv/bin/pip install -q --upgrade "aiogram>=3.13,<4"
chown -R friendbot:friendbot /opt/friendbot

cat >/etc/systemd/system/friendbot.service <<'UNIT'
[Unit]
Description=Friend bot (paid channel access)
After=network-online.target
Wants=network-online.target

[Service]
User=friendbot
EnvironmentFile=/etc/friendbot/bot.env
WorkingDirectory=/opt/friendbot
ExecStart=/opt/friendbot/venv/bin/python bot.py
Restart=always
RestartSec=5
# изоляция: не видит наши файлы, не может получить root, ограничен по ресурсам
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/opt/friendbot
InaccessiblePaths=-/etc/most -/var/lib/most -/etc/amnezia -/opt/most
MemoryMax=256M
CPUQuota=30%

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable -q friendbot
systemctl restart friendbot
sleep 3
systemctl is-active --quiet friendbot && echo "Готово: бот работает. Логи: journalctl -u friendbot -f" \
  || { echo "Бот не запустился:"; journalctl -u friendbot -n 20 --no-pager; exit 1; }
