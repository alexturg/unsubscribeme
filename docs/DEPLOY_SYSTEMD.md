# Деплой бота через systemd

Ниже — шаблон юнита systemd и шаги для автозапуска бота при перезагрузке сервера.

## 1) Подготовка каталога и окружения

- Куда ставим код: `/opt/unsubscribeme`
- Пример:
  - `sudo mkdir -p /opt/unsubscribeme`
  - `sudo chown -R $USER:$USER /opt/unsubscribeme`
  - `cd /opt/unsubscribeme && git clone git@github.com:alexturg/unsubscribeme.git .`
  - Python venv:
    - `python3 -m venv .venv`
    - `source .venv/bin/activate`
    - `pip install --upgrade pip`
    - `pip install .[test]` (или просто `pip install .`)
  - Создайте файл окружения `/opt/unsubscribeme/.env` по образцу `.env.example`.

## 2) Юнит systemd

- Скопируйте файл `deploy/systemd/unsubscribeme.service` в `/etc/systemd/system/unsubscribeme.service`:
  - `sudo cp deploy/systemd/unsubscribeme.service /etc/systemd/system/unsubscribeme.service`
  - Откройте и поправьте:
    - `User=`/`Group=` на вашего пользователя
    - `WorkingDirectory=` и `ExecStart=` (путь до venv), например:
      - `WorkingDirectory=/opt/unsubscribeme`
      - `ExecStart=/opt/unsubscribeme/.venv/bin/unsubscribeme`
    - `EnvironmentFile=/opt/unsubscribeme/.env`

- Перечитайте демона и включите автозапуск:
  - `sudo systemctl daemon-reload`
  - `sudo systemctl enable unsubscribeme`
  - `sudo systemctl start unsubscribeme`
  - Проверка статуса: `systemctl status unsubscribeme`
  - Логи: `journalctl -u unsubscribeme -f`

## 3) Обновления

- Обновить код:
  - `cd /opt/unsubscribeme && git pull`
  - Обновить зависимости/бинарь:
    - `source .venv/bin/activate && pip install -U .`
  - Перезапустить сервис:
    - `sudo systemctl restart unsubscribeme`

## 4) Docker (опционально)

Если предпочитаете Docker, используйте `Dockerfile` в корне и создайте отдельный юнит с `ExecStart=/usr/bin/docker run ...` или docker-compose (не включено в этот пример). 

## 5) Параметры .env

Минимально:
- `TELEGRAM_BOT_TOKEN=...`
- `ALLOWED_CHAT_IDS=...`
- `TZ=Europe/Moscow` (или `Asia/Almaty`)

Полный список смотрите в `.env.example`.

*** Конфигурация готова. Бот поднимется автоматически при перезагрузке. ***

