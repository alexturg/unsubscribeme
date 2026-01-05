# Деплой бота через systemd

Ниже — шаблон юнита systemd и шаги для автозапуска бота при перезагрузке сервера.

## 1) Подготовка каталога и окружения

- Рекомендуется отдельный пользователь для бота, например `unsubscribeme`.
- Куда ставим код: `/home/BOT_USER/unsubscribeme` (это `~/unsubscribeme` для выбранного пользователя; в systemd/Secrets используйте полный путь)
- Пример:
  - Создайте пользователя и переключитесь на него:
    - `sudo useradd --system --create-home --home-dir /home/unsubscribeme --shell /usr/sbin/nologin unsubscribeme`
    - `sudo -iu unsubscribeme`
  - `mkdir -p ~/unsubscribeme`
  - `cd ~/unsubscribeme && git clone git@github.com:alexturg/unsubscribeme.git .`
  - Python venv:
    - `python3 -m venv .venv`
    - `source .venv/bin/activate`
    - `pip install --upgrade pip`
    - `pip install .[test]` (или просто `pip install .`)
  - Создайте файл окружения `/home/BOT_USER/unsubscribeme/.env` по образцу `.env.example`.

## 2) Юнит systemd

- Скопируйте файл `deploy/systemd/unsubscribeme.service` в `/etc/systemd/system/unsubscribeme.service`:
  - `sudo cp deploy/systemd/unsubscribeme.service /etc/systemd/system/unsubscribeme.service`
  - Откройте и поправьте:
    - `User=`/`Group=` на пользователя бота
    - `WorkingDirectory=` и `ExecStart=` (путь до venv), например:
      - `WorkingDirectory=/home/BOT_USER/unsubscribeme`
      - `ExecStart=/home/BOT_USER/unsubscribeme/.venv/bin/unsubscribeme`
    - `EnvironmentFile=/home/BOT_USER/unsubscribeme/.env`

- Перечитайте демона и включите автозапуск:
  - `sudo systemctl daemon-reload`
  - `sudo systemctl enable unsubscribeme`
  - `sudo systemctl start unsubscribeme`
  - Проверка статуса: `systemctl status unsubscribeme`
  - Логи: `journalctl -u unsubscribeme -f`

## 3) Обновления

- Обновить код:
  - `cd /home/BOT_USER/unsubscribeme && git pull`
  - Обновить зависимости/бинарь:
    - `source .venv/bin/activate && pip install -U .`
  - Перезапустить сервис:
    - `sudo systemctl restart unsubscribeme`

## 4) GitHub Actions: автодеплой при пуше в main

В репозитории добавлен workflow `.github/workflows/deploy.yml`. Он по пушу в `main` сначала запускает тесты, а затем делает SSH на сервер, выполняет `git pull --ff-only`, обновляет зависимости и перезапускает systemd‑сервис.

### 4.1) Подготовка SSH‑ключа для GitHub Actions

- Локально:
  - `ssh-keygen -t ed25519 -f ~/.ssh/unsubscribeme_deploy -C "github-actions"`
- На сервере: добавьте публичный ключ в `~/.ssh/authorized_keys` пользователя, под которым будет выполняться деплой.
- В GitHub → Settings → Secrets and variables → Actions добавьте секреты:
  - `DEPLOY_SSH_KEY` — приватный ключ из `~/.ssh/unsubscribeme_deploy`
  - `DEPLOY_HOST` — IP/домен сервера
  - `DEPLOY_USER` — пользователь для SSH
  - `DEPLOY_PATH` — путь к репозиторию, например `/home/BOT_USER/unsubscribeme` (не используйте `~`, он не разворачивается в secrets)
  - `DEPLOY_SERVICE` — имя systemd‑сервиса, например `unsubscribeme`
  - `DEPLOY_PORT` — опционально, если SSH не на 22

Убедитесь, что на сервере настроен доступ к GitHub для `git pull` (deploy‑key на сервере или HTTPS‑токен).

### 4.2) Права на sudo

Workflow делает `sudo systemctl restart ...`. Убедитесь, что `DEPLOY_USER` может выполнять это без пароля, например:
- `sudo visudo -f /etc/sudoers.d/unsubscribeme`
  - `DEPLOY_USER ALL=NOPASSWD: /bin/systemctl restart unsubscribeme`

Путь до `systemctl` может быть `/usr/bin/systemctl` — проверьте `command -v systemctl`.

### 4.3) Проверка

- Сделайте коммит/пуш в `main` и проверьте workflow в GitHub Actions.
- На сервере проверьте логи: `journalctl -u unsubscribeme -f`

## 5) Docker (опционально)

Если предпочитаете Docker, используйте `Dockerfile` в корне и создайте отдельный юнит с `ExecStart=/usr/bin/docker run ...` или docker-compose (не включено в этот пример). 

## 6) Параметры .env

Минимально:
- `TELEGRAM_BOT_TOKEN=...`
- `ALLOWED_CHAT_IDS=...`
- `TZ=Europe/Moscow` (или `Asia/Almaty`)

Полный список смотрите в `.env.example`.

*** Конфигурация готова. Бот поднимется автоматически при перезагрузке. ***
