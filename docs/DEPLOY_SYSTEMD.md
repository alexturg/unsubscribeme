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

Перед обновлением проверьте `WorkingDirectory` и `EnvironmentFile` через
`systemctl cat unsubscribeme`. Найдите `DB_PATH` в серверном `.env`.
Относительный путь считается от `WorkingDirectory`; при отсутствии `DB_PATH`
используется `data/bot.sqlite`. Следующие команды предполагают, что вы уже
подставили **фактический абсолютный путь** рабочей базы в `DB_FILE`.
Демо-база `data/bot.demo.sqlite` для обновления не используется.

```bash
cd /opt/unsubscribeme
DB_FILE=/opt/unsubscribeme/data/bot.sqlite  # замените на фактический DB_PATH
OLD_REV=$(git rev-parse HEAD)
sudo systemctl stop unsubscribeme
sudo install -d -m 700 /var/backups/unsubscribeme
BACKUP_FILE=/var/backups/unsubscribeme/bot-$(date +%Y%m%d-%H%M%S).sqlite
sudo python3 - "$DB_FILE" "$BACKUP_FILE" <<'PY'
import sqlite3
import sys

source, destination = sys.argv[1:]
with sqlite3.connect(source) as original, sqlite3.connect(destination) as backup:
    assert original.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    original.backup(backup)
    assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    for table in ("users", "feeds", "items", "deliveries"):
        left = original.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        right = backup.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert left == right, table
        print(f"{table}: {left}")
print("Backup verified:", destination)
PY
git pull
.venv/bin/pip install -U .
sudo systemctl start unsubscribeme
systemctl status unsubscribeme
journalctl -u unsubscribeme -n 80 --no-pager
```

При первом запуске новая версия добавляет таблицы карточек и **один раз**
помечает все прежние успешные доставки как обработанные (✓). Текущие таблицы
и записи остаются на месте. Проверьте, что сервис запустился, `/someday`
отвечает, а количества строк в `users`, `feeds`, `items` и `deliveries` не
уменьшились по сравнению с напечатанными перед обновлением. Во время
перезапуска возможна короткая пауза в доставке.

Если обновление не запустилось, остановите сервис, верните предыдущую версию
`git switch --detach "$OLD_REV"`, установите её через `.venv/bin/pip install -U .`
и запустите сервис. Новые таблицы совместимы со старой версией, поэтому обычно
восстанавливать БД не нужно. Если повреждение базы подтверждено **до появления
новых пользовательских данных**, восстановите копию через SQLite Backup API:

```bash
sudo systemctl stop unsubscribeme
sudo python3 - "$BACKUP_FILE" "$DB_FILE" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as snapshot, sqlite3.connect(sys.argv[2]) as current:
    snapshot.backup(current)
    assert current.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
PY
sudo systemctl start unsubscribeme
```

После начала работы обновлённого сервиса не заменяйте рабочую базу старой
копией без сверки: так можно потерять новые действия пользователя. Значения
`OLD_REV`, `DB_FILE` и `BACKUP_FILE` сохраните до окончания проверки.

## 4) Docker (опционально)

Если предпочитаете Docker, используйте `Dockerfile` в корне и создайте отдельный юнит с `ExecStart=/usr/bin/docker run ...` или docker-compose (не включено в этот пример). 

## 5) Параметры .env

Минимально:
- `TELEGRAM_BOT_TOKEN=...`
- `ALLOWED_CHAT_IDS=...`
- `TZ=Europe/Moscow` (или `Asia/Almaty`)

Полный список смотрите в `.env.example`.

*** Конфигурация готова. Бот поднимется автоматически при перезагрузке. ***
