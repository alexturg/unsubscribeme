Продолжение работы по автодеплою (GitHub Actions + systemd)

Контекст
- Проект: /Users/zhu/PROGRAM/unsubscribeme
- Цель: автодеплой на сервер при пуше в main, с обязательным прогоном unit-тестов.
- Ветка: main.
- Сервер: systemd; пользователь хочет отдельного пользователя для бота.

Что уже сделано
- Добавлен workflow: `.github/workflows/deploy.yml`
  - Job `test`: checkout, setup Python 3.11, `pip install .[test]`, `pytest`
  - Job `deploy`: зависит от `test`, делает SSH и выполняет:
    `cd $DEPLOY_PATH && git pull --ff-only origin main && ./.venv/bin/pip install -U . && sudo systemctl restart $DEPLOY_SERVICE`
  - Секреты: `DEPLOY_SSH_KEY`, `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH`, `DEPLOY_SERVICE`, `DEPLOY_PORT` (опц.)
- Обновлена инструкция `docs/DEPLOY_SYSTEMD.md`:
  - Рекомендован отдельный пользователь бота.
  - Путь по умолчанию: `/home/BOT_USER/unsubscribeme`
  - Добавлен раздел про GitHub Actions и sudoers.
- Обновлён systemd unit: `deploy/systemd/unsubscribeme.service`
  - `User=unsubscribeme`, `Group=unsubscribeme`
  - Пути: `/home/unsubscribeme/unsubscribeme`
- Добавлены тесты:
  - `tests/test_config_utils.py`
  - `tests/test_rss_utils.py`
  - `tests/test_rss_fetch.py`
  - `tests/test_yt_channel_id_utils.py`
- Локально все тесты проходят: `pytest` (24 passed).

Важно уточнить/проверить
- Реальный путь к коду на сервере: у пользователя код был в `~/unsubscribeme`.
  Если переходите на отдельного пользователя `unsubscribeme`, путь должен быть `/home/unsubscribeme/unsubscribeme`.
  Если оставляете текущего пользователя — обновить юнит/доки/секреты на фактический путь.
- В GitHub Secrets `DEPLOY_PATH` должен быть полным путём (без `~`).
- У пользователя `DEPLOY_USER` должен быть доступ к репозиторию для `git pull`.
- Нужен sudo без пароля для `systemctl restart $DEPLOY_SERVICE`:
  `DEPLOY_USER ALL=NOPASSWD: /bin/systemctl restart unsubscribeme`
  (или `/usr/bin/systemctl` — проверить `command -v systemctl`).

Что осталось сделать
- Уточнить у пользователя, куда именно развернут код и какое имя пользователя нужно (оставить текущий или создать `unsubscribeme`).
- Настроить на сервере:
  - Создать пользователя (если нужен), склонировать репозиторий, создать venv, установить зависимости.
  - Положить `.env`.
  - Обновить/скопировать systemd unit и перезапустить `systemctl`.
  - Выключить старый сервис при миграции.
- В GitHub:
  - Добавить secrets для деплоя.
  - Проверить, что Actions запускаются и деплой проходит.

Файлы, затронутые в репозитории
- `.github/workflows/deploy.yml` (новый job test + deploy)
- `docs/DEPLOY_SYSTEMD.md` (обновлены пути и шаги)
- `deploy/systemd/unsubscribeme.service` (пути и пользователь)
- `tests/test_config_utils.py` (новый)
- `tests/test_rss_utils.py` (новый)
- `tests/test_rss_fetch.py` (новый)
- `tests/test_yt_channel_id_utils.py` (новый)
