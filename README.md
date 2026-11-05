# Amnezia Monitor — мониторинг ваших серверов AmneziaVPN (только чтение)

Агент периодически заходит по SSH на каждый ваш сервер AmneziaVPN и **только читает** его состояние:
доступность, нагрузку (CPU, RAM, сеть, load), контейнеры Amnezia и подключённых клиентов.
История хранится в SQLite. При изменениях (сервер упал или поднялся, контейнер остановился, клиент подключился
или отключился, высокая нагрузка) приходит уведомление в Telegram. Если Telegram не настроен, сообщение
печатается в консоль. Раз в день можно получать сводку.

Конфигурация серверов не меняется: ничего не перезапускается, пиры не добавляются и не удаляются.

## Как устроено

```
servers.yaml ─► amnezia_monitor.py ── SSH (paramiko, пароль или ключ) ─► сервер 1..N
                     │                    только команды из белого списка
                     ├─ парсеры: /proc/*, docker ps, awg/wg show all dump, clientsTable
                     ├─ SQLite: polls, containers, peers, events, state
                     ├─ детектор изменений (сравнение с прошлым опросом)
                     └─ уведомления: Telegram Bot API или консоль
```

Что делается на каждом опросе для каждого сервера:
1. С машины мониторинга: TCP-подключение к SSH-порту (задержка) и `ping`, если он есть.
2. По SSH, только чтение: `cat /proc/uptime`, `cat /proc/loadavg`, `nproc`, `cat /proc/meminfo`,
   `head -1 /proc/stat` (CPU % считается между опросами), `cat /proc/net/dev` (скорость сети между опросами).
3. `docker ps -a --format ...`: поиск контейнеров `amnezia-*` (awg, wireguard, xray, openvpn, cloak, dns…).
   Для xray, openvpn и других протоколов показывается только «работает / остановлен».
4. Для `amnezia-awg` и `amnezia-wireguard`: `docker exec <c> sh -c 'awg show all dump || wg show all dump'`.
   Клиент считается **онлайн**, если последний handshake был меньше 180 секунд назад (порог настраивается).
   Приватный ключ сервера из первой строки dump игнорируется и нигде не сохраняется.
5. Имена клиентов берутся из `/opt/amnezia/<awg|wireguard>/clientsTable` внутри контейнера (там их хранит
   приложение Amnezia). Секция `clients:` в конфиге позволяет переопределить имя по публичному ключу.

Любая команда вне белого списка (регулярное выражение `_ALLOWED_RE`) отклоняется ещё до отправки на сервер.

## Файлы

| Файл | Назначение |
|---|---|
| `amnezia_monitor.py` | CLI и вся логика |
| `demo_data.py` | тестовые данные для `--demo` (в том числе история за 24 ч для панели) |
| `dashboard.py`, `dashboard.html` | веб-панель: HTTP-сервер (stdlib) и одностраничный интерфейс |
| `dashboard_api.py` | JSON API панели: агрегация истории из SQLite |
| `servers.yaml.example` | пример конфига на 4 тестовых сервера |
| `.env.example` | пример переменных: токен Telegram, пароли |
| `requirements.txt` | paramiko, PyYAML, requests |
| `tests/test_monitor.py` | тесты: парсеры, конфиг, демо, SSH-путь (встроенный SSH-сервер paramiko) |
| `tests/test_dashboard.py` | тесты панели: DNS-журнал, агрегация, API, HTTP |
| `deploy/*.service, *.timer` | юниты systemd |
| `deploy/windows-task.ps1` | задача Планировщика Windows |
| `app_main.py` | режим приложения (`.exe`): мониторинг + панель + настройки, значок в трее, один экземпляр |
| `app_settings.py` | страница «Настройки»: проверка полей, сохранение `servers.yaml`, «Проверить подключение» |
| `secrets_store.py` | шифрование паролей: Windows DPAPI (текущий пользователь), иначе файл с правами 600 |
| `AmneziaMonitor.spec`, `build_exe.ps1` | сборка одного файла `AmneziaMonitor.exe` (PyInstaller) |
| `requirements-app.txt` | + pystray, Pillow (значок в трее) |
| `tests/test_app.py` | тесты настроек: токен/Origin/Host, пароли не утекают, DPAPI (с подменой), один экземпляр |

## Быстрый старт (Linux: Kali, VPS, любой постоянно включённый компьютер)

```bash
sudo mkdir -p /opt/amnezia-monitor && sudo chown "$USER" /opt/amnezia-monitor
cd /opt/amnezia-monitor
unzip ~/amnezia-monitor.zip -d /tmp && cp -r /tmp/amnezia-monitor/. .
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Демо без серверов
.venv/bin/python amnezia_monitor.py --demo check

# Настоящий конфиг
cp servers.yaml.example servers.yaml && chmod 600 servers.yaml
cp .env.example .env && chmod 600 .env
nano servers.yaml        # свои IP, пользователи и пароли (у каждого сервера свой)
.venv/bin/python amnezia_monitor.py -c servers.yaml -v check
.venv/bin/python amnezia_monitor.py -c servers.yaml summary
.venv/bin/python amnezia_monitor.py -c servers.yaml run     # постоянный цикл
.venv/bin/python -m unittest discover -s tests -v            # тесты
```

Команды:
- `check` — один опрос всех серверов, уведомления об изменениях.
- `run` — бесконечный цикл с интервалом `interval`. Если задан `daily_summary_hour`, раз в день отправляется сводка.
- `summary [--hours 24] [--send]` — сводка по истории. С `--send` она уходит в Telegram.
- `--demo` (он же `--dry-run`) — встроенные фальшивые данные, без сети и Telegram.

## Пароли и безопасность

- У **каждого сервера свой пароль**: `- {name: test1, host: 203.0.113.11, password: "CHANGE_ME_1"}`.
  Общий пароль в `defaults` запрещён, агент откажется стартовать. Если у двух серверов совпадают пароли,
  в лог пишется предупреждение (без самих паролей).
- Вместо `password` можно указать `password_env: SRV1_PASS`, а значение держать в `.env` рядом с конфигом или
  в окружении (`EnvironmentFile` в systemd). Так в yaml вообще не будет секретов.
- Вместо пароля можно входить по ключу: `key_path: ~/.ssh/amnezia_monitor`. Это надёжнее.
- **Обязательно** выполните `chmod 600 servers.yaml .env`.
- **Никогда не коммитьте** `servers.yaml` и `.env`. Они уже есть в `.gitignore`.
- Пароли не пишутся в лог, в базу и в уведомления. При ошибке входа выводится только тип ошибки,
  например `SSH: AuthenticationException`.
- Ключ хоста при первом подключении запоминается в `~/.ssh/known_hosts`. Для строгого режима сначала добавьте
  хосты сами (`ssh-keyscan`) и задайте `AMNEZIA_STRICT_HOSTKEY=1`.

### Какие права нужны на сервере

Читать `/proc` может любой пользователь. Для `docker ps` и `docker exec` нужен один из вариантов:
- пользователь `root`, как обычно и бывает на серверах, которые настраивало приложение Amnezia;
- пользователь в группе `docker`. Учтите, что эта группа фактически даёт права root;
- отдельный пользователь `monitor` с `docker_cmd: "sudo -n docker"` и узким правилом sudo в
  `/etc/sudoers.d/amnezia-monitor`, например:
  ```
  monitor ALL=(root) NOPASSWD: /usr/bin/docker ps -a --format *, /usr/bin/docker exec amnezia-* sh -c *
  ```
  `docker exec` по своей сути позволяет выполнить в контейнере что угодно. Поэтому для максимальной
  изоляции лучше вход по ключу и отдельная машина мониторинга. Сам агент шлёт только команды из белого списка.

## Telegram

1. Создайте бота у @BotFather и скопируйте токен.
2. Напишите боту любое сообщение. Ваш chat_id можно узнать через
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. В `.env` пропишите `TELEGRAM_BOT_TOKEN=...` и `TELEGRAM_CHAT_ID=...`.

## systemd (постоянная работа)

```bash
sudo useradd -r -s /usr/sbin/nologin -d /opt/amnezia-monitor amnezia-monitor
sudo chown -R amnezia-monitor: /opt/amnezia-monitor
sudo chmod 600 /opt/amnezia-monitor/servers.yaml /opt/amnezia-monitor/.env
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Вариант А: постоянный процесс
sudo systemctl enable --now amnezia-monitor.service
# Вариант Б: таймер раз в минуту (вместо А, не вместе)
# sudo systemctl enable --now amnezia-monitor-check.timer

# Ежедневная сводка в 09:00 (если не используете daily_summary_hour в режиме run)
sudo systemctl enable --now amnezia-monitor-summary.timer

journalctl -u amnezia-monitor -f
```

## Windows (ПК NAKO)

```powershell
cd C:\amnezia-monitor
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy servers.yaml.example servers.yaml   # заполните; доступ к файлу оставьте только своей учётке
.venv\Scripts\python amnezia_monitor.py --demo check
.venv\Scripts\python amnezia_monitor.py -c servers.yaml check
powershell -ExecutionPolicy Bypass -File deploy\windows-task.ps1   # задачи Планировщика
```

Задача `AmneziaMonitor` запускает `check` каждую минуту, `AmneziaMonitorSummary` присылает сводку в 9:00.
Минус Windows: когда ПК выключен или спит, мониторинга нет. Лучше держать агент на постоянно включённом
Linux, например на маленьком VPS, отдельном от VPN-серверов.

## Веб-панель (дашборд)

Лёгкая страница в браузере: без интернета, без CDN и без дополнительных библиотек (встроенный `http.server` + один HTML-файл). Панель только **читает** локальную базу `amnezia_monitor.db`, которую заполняют `check`/`run`. Сама она к серверам не подключается (если не указан `--with-monitor`).

Что показывает (вкладка на каждый сервер):
- **Шапка-карточки**: статус, задержка (TCP/ping), аптайм и load, сколько клиентов онлайн, текущая скорость сети сервера.
- **Ресурсы**: круговые индикаторы CPU и памяти, график CPU за 24 ч, контейнеры Amnezia (зелёный — работает, красный — остановлен).
- **Трафик интерфейса сервера за 24 ч**: столбики по 30 минут (исходящий и входящий), итоги.
- **Пользователи**: онлайн или офлайн (по handshake), когда был в сети, скорость сейчас (↓ к клиенту, ↑ от клиента), мини-график нагрузки, трафик за 24 ч и за всё время.
- **Сайты (по DNS-запросам)**: топ доменов на сервере и отдельно по каждому пользователю. Работает только если DNS-журнал уже включён (см. ниже).
- **События**: подключения и отключения, падения сервера и контейнеров, высокая нагрузка.

Страница обновляется сама каждые 15 секунд.

### Запуск — Linux
```bash
cd amnezia-monitor && . .venv/bin/activate
python amnezia_monitor.py -c servers.yaml run &          # сбор данных (или systemd-таймер, см. выше)
python amnezia_monitor.py -c servers.yaml dashboard      # открыть http://127.0.0.1:8765
# или всё в одном процессе:
python amnezia_monitor.py -c servers.yaml dashboard --with-monitor
```
Чтобы смотреть панель с другого компьютера, лучше использовать SSH-туннель: `ssh -L 8765:127.0.0.1:8765 user@этот-компьютер`. Вариант `--host 0.0.0.0` откроет панель всем в сети **без пароля**.

### Запуск — Windows
```powershell
cd amnezia-monitor
.venv\Scripts\python amnezia_monitor.py -c servers.yaml dashboard --with-monitor
# затем откройте в браузере http://127.0.0.1:8765
```
Если опрос уже идёт задачей Планировщика (`AmneziaMonitor`), достаточно просто `dashboard`. Автозапуск панели при входе в Windows (без окна консоли, через `pythonw`):
```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows-task.ps1 -Dashboard
```

### Демо и статичная копия
```bash
python amnezia_monitor.py --demo dashboard                    # тестовые данные за 24 ч, http://127.0.0.1:8765
python amnezia_monitor.py --demo dashboard --export-html demo.html
python amnezia_monitor.py -c servers.yaml dashboard --export-html snapshot.html   # снимок ваших данных в один файл
```
Внимание: в снимке `snapshot.html` есть имена клиентов, IP и домены. Не публикуйте его. Паролей в нём нет.

API (JSON): `/api/servers`, `/api/server?name=test1&hours=24`, `/api/health`.
С другого компьютера эти запросы проходят только если панель запущена не на `127.0.0.1` и (когда задан токен) с заголовком `X-Amnezia-Token`. Пароли SSH в JSON не попадают. Как открыть панель для телефона — в разделе «Приложение Android».

### Сайты пользователей: откуда берутся и как включить
VPN-туннель (WireGuard/AmneziaWG) сам по себе видит только **объём** трафика, но не адреса сайтов. Домены можно узнать только из журнала DNS-запросов контейнера `amnezia-dns`, если клиенты используют его как DNS (в Amnezia это настройка по умолчанию). Агент **ничего не включает и не меняет** на серверах. Он только читает журнал (`tail` файла или `docker logs`), если журнал уже ведётся. Если журнала нет, в панели будет надпись «DNS-журнал не включён на сервере».

Включить журнал можно только самостоятельно. Учтите, что это хранение истории посещений ваших пользователей, поэтому предупредите их.
- **Unbound** (стандартный `amnezia-dns`): в конфиг Unbound внутри контейнера добавить в секцию `server:` строки `verbosity: 1`, `log-queries: yes`, `use-syslog: no` и `logfile: ""` (вывод в лог контейнера) или `logfile: "/var/log/unbound/unbound.log"`, затем перезапустить контейнер (`docker restart amnezia-dns`). Изменения внутри контейнера могут пропасть, если Amnezia пересоздаст контейнер.
- **AdGuard Home** (если стоит вместо Unbound): «Настройки → Настройки журнала → Включить журнал». Агент читает `/opt/adguardhome/work/data/querylog.json`.

Ограничения раздела «Сайты»:
- Видны только домены, а не страницы. Многие запросы идут в фоне (обновления, реклама, CDN вроде `googlevideo.com`).
- Если на устройстве включён «Безопасный DNS» или DoH (браузер, Android Private DNS, iCloud Private Relay), запросы мимо `amnezia-dns` не попадут в журнал.
- Привязка к пользователю идёт по туннельному IP (allowed-ips). Если IP в журнале не туннельный (например, из-за NAT в Docker), домен попадёт в статистику сервера под IP-адресом, а не под именем.
- Берутся последние 3000 строк журнала при каждом опросе. При очень большом числе запросов между опросами часть запросов пропадёт.

## Программа для Windows: один файл AmneziaMonitor.exe

Без установки Python: двойной щелчок по `AmneziaMonitor.exe` — запускаются мониторинг и панель,
открывается браузер (`http://127.0.0.1:8765`, если порт занят — любой свободный).
Значок «A» в трее (возле часов): «Открыть панель», «Папка с данными», «Выход».
Повторный запуск не создаёт вторую копию, а просто открывает панель.

* Настройки и база: `%APPDATA%\AmneziaMonitor\` (`servers.yaml`, `amnezia_monitor.db`, журнал `amnezia-monitor.log`).
* При первом запуске серверов нет — сразу открывается страница **Настройки**: добавьте серверы
  (название, IP или домен, SSH-порт, пользователь, пароль этого сервера или путь к ключу, заметка),
  кнопка **«Проверить подключение»** проверит TCP, вход по SSH и `docker ps` (только чтение).
  Там же — интервал опроса и (необязательно) токен и chat id Telegram.
  Изменения применяются сразу, без перезапуска.
* Пароли и токен хранятся в `servers.yaml` **зашифрованными Windows DPAPI** (`password_enc: dpapi:…`):
  расшифровать их может только ваша учётная запись Windows на этом ПК. В браузер пароли не
  передаются — видно только «пароль сохранён»; пустое поле при изменении = оставить прежний пароль.
  Если перенести `servers.yaml` на другой ПК/учётную запись, пароли придётся ввести заново.
* Панель по умолчанию доступна только с этого компьютера (127.0.0.1). Изменять настройки может только открытая
  программой страница: у неё одноразовый секретный ключ сессии, плюс проверка Origin/Host
  (защита от чужих сайтов в соседней вкладке). Чтобы смотреть те же данные с телефона, включите
  «Телефон» в настройках — см. раздел ниже. Страница настроек и пароли SSH при этом остаются только на этом ПК.
* Остановить: значок в трее → «Выход» или «Настройки» → «Остановить программу».
* Автозапуск: положите ярлык на `.exe` в папку `shell:startup` (Win+R → `shell:startup`).

### Сборка .exe (один раз, на любом Windows-ПК с Python 3.9+)

```powershell
cd путь\к\amnezia-monitor
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
# ключи: -Clean (пересоздать окружение), -Test (сначала тесты), -Python C:\путь\python.exe
```

Скрипт создаёт `.venv-build`, ставит зависимости и PyInstaller и собирает `dist\AmneziaMonitor.exe`
(~30–40 МБ: внутри Python, paramiko/cryptography, PyYAML, requests, pystray, Pillow и `dashboard.html`).
Полный путь к файлу печатается в конце. Готовый `.exe` можно копировать куда угодно — он не зависит от папки проекта.

Без сборки то же самое: `python amnezia_monitor.py app` (или `python app_main.py`, демо: `python amnezia_monitor.py --demo app`).

**Особенности .exe:**
* Файл не подписан, поэтому Windows SmartScreen покажет «Windows защитила ваш компьютер» →
  «Подробнее» → «Выполнить в любом случае».
* Антивирусы иногда ложно ругаются на однофайловые сборки PyInstaller. Если так — добавьте файл
  в исключения или соберите заново у себя (исходники открыты, сборка воспроизводима).
* Первый запуск занимает несколько секунд: `.exe` распаковывает себя во временную папку.

## Приложение Android

Телефон показывает то же, что панель на компьютере: статус сервера, задержка, CPU, память, пиры
WireGuard/AmneziaWG, скорость RX/TX и топ DNS, если журнал уже есть. Приложение **только читает**
JSON API (`/api/servers`, `/api/server`, `/api/health`). Серверы можно также импортировать из резервной копии AmneziaVPN или ссылки `vpn://`: закрытую память того приложения прочитать нельзя. Пароль SSH из полной копии хранится в защищённом хранилище телефона и используется только для команд чтения. Подробности — в [android/README.md](android/README.md).

Панель по умолчанию слушает `127.0.0.1`, поэтому с телефона её не видно, пока не выполнен один из вариантов:

1. **Настройки → Телефон** в Amnezia Monitor (рекомендуется): включить чтение из локальной сети,
   скопировать в приложение адрес вида `http://192.168.1.104:8765` и токен. Программа сама начинает
   слушать сеть. В брандмауэре Windows разрешите её для частной сети. Телефон и ПК должны быть в одной
   сети или в одном VPN.
2. **CLI**, если панель запускается командой, а не окном программы:
   `AMNEZIA_API_TOKEN='длинный-случайный-токен' python amnezia_monitor.py -c servers.yaml dashboard --host 0.0.0.0 --with-monitor`
   (переменная окружения, чтобы токен не оставался в списке процессов).
3. **SSH-туннель** на уже запущенную локальную панель, без токена:
   `ssh -L 8765:127.0.0.1:8765 user@компьютер` и в телефоне адрес `http://127.0.0.1:8765` — только если
   туннель поднят на самом телефоне. Иначе укажите адрес машины, где открыт туннель.

Канал HTTP, без TLS. Не пробрасывайте порт `8765` в интернет: токен даёт чтение имён клиентов и доменов DNS.
Заголовок запроса: `X-Amnezia-Token`. Для нативного приложения CORS не нужен; если токен включён, панель
всё же отвечает на preflight `OPTIONS`, чтобы браузер с другого сайта мог прислать этот заголовок.

Сборка и установка APK (Android Studio или `./gradlew :app:assembleDebug`) — в [android/README.md](android/README.md).

## Приложение macOS

Тот же клиент для Mac: строка меню и окно, опрос панели (`X-Amnezia-Token`, только чтение) и импорт резервной копии AmneziaVPN, ссылки `vpn://` или `.conf`. Токен и секрет SSH лежат в Связке ключей. Ключи VPN не сохраняются. Если панель недоступна, а в копии есть пароль SSH, Mac сам читает сервер через `/usr/bin/ssh` теми же командами, что и телефон. Сборка — Xcode / `xcodebuild`, macOS 13+. Подробности — в [macos/README.md](macos/README.md).

## Ограничения

- «Онлайн» определяется по handshake. Клиент без трафика может выглядеть онлайн ещё до 3 минут после отключения.
- Для xray, openvpn и cloak видно только, работает ли контейнер. Подключения к ним не считаются.
- Первый опрос служит базой: CPU % и скорость сети появляются со второго опроса.
- Пинг выполняется с машины мониторинга. Если ICMP закрыт, используется только задержка TCP до SSH.
- Проверка TCP-порта оставляет в логе sshd строку вида «Did not receive identification string». Это безвредно.
- Формат `clientsTable` может меняться между версиями Amnezia. Если имена не определились, задайте их
  в секции `clients:`.
