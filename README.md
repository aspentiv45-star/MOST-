# MOST

AmneziaWG на сервере и Telegram-бот, который сам выдаёт конфиги.

```
Телефон / ПК (AmneziaWG)  ──►  Сервер в Нидерландах  ──►  Интернет
Telegram-бот  ──►  выдаёт файлы .conf и QR-коды
```

## Что понадобится

1. **Сервер** с Ubuntu 22.04 или 24.04 и доступом root.
2. **Токен бота.** Напишите [@BotFather](https://t.me/BotFather) → `/newbot` → скопируйте токен вида `123456:ABC...`.
3. **Ваш Telegram ID.** Напишите [@userinfobot](https://t.me/userinfobot), он пришлёт число.

## Установка

```bash
apt update && apt install -y git
git clone https://github.com/aspentiv45-star/MOST-.git
cd MOST-
sudo bash install.sh --token 'ТОКЕН_БОТА' --admin ВАШ_ID
```

Если у хостинга есть свой файрвол, откройте в нём UDP-порт, который назовёт скрипт.

## Как пользоваться ботом

**Пользователь:**
- `/start`: если доступа нет, администратору придёт запрос с кнопками «Разрешить» и «Отклонить».
- «➕ Новый конфиг»: бот присылает файл `.conf` и QR-код.
- «📄 Мои конфиги»: скачать заново или удалить.

Один человек может взять до 3 конфигов (меняется через `--max`).

**Администратор:**
```
/users       кому разрешён доступ
/stats       кто подключался и сколько трафика
/allow ID    разрешить доступ
/deny ID     забрать доступ и удалить все конфиги человека
```

## Приложения

- iPhone / Mac: AmneziaVPN или AmneziaWG из App Store
- Android: AmneziaVPN или AmneziaWG из Google Play
- Windows / Linux: AmneziaVPN с [amnezia.org](https://amnezia.org)

В приложении: «+» → импорт файла или сканирование QR-кода.

## Обслуживание

```bash
git pull && sudo bash install.sh --token '...' --admin ...   # обновить (ключи и клиенты сохраняются)
journalctl -u most-bot -f                                    # логи бота
awg show                                                     # подключённые клиенты
sudo bash uninstall.sh                                       # удалить (с резервной копией)
```

## Безопасность

- Ключи и токен лежат только на сервере: `/etc/amnezia/amneziawg/`, `/etc/most/`, `/var/lib/most/`. В репозитории их нет.
- Конфиг — это ключ доступа. Не пересылайте его и не выкладывайте.
