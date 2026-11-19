# LibertyAPI

[English](#english) | [Русский](#русский)

<a id="english"></a>

## English

A Telegram bot that gives chosen people access to local LLMs running in
[LM Studio](https://lmstudio.ai) (or its Bionic build). The models run on your own computer, and the
bot shares them through Telegram. No public IP or port forwarding is needed: the bot connects to
Telegram by itself.

### Features

- **Invite only.** The bot answers only people who were given access and stays silent to everyone else.
- **A personal chat for everyone** with its own context. `/new` starts a new conversation.
- **Queue.** There is one model, so requests are handled one at a time; everyone sees their place in the queue.
- **Live answers.** Text appears gradually; a long answer arrives as several messages split on
  sentence boundaries.
- **Attachments.** Text, photos (if the model supports vision), files: TXT/Markdown/code, PDF, DOCX.
- **Admin commands.** Choosing the model and granting access are for a single admin only.
- **Memory saving.** The model is unloaded after idle time and loaded back on demand.
- **Long conversations** are compacted automatically so they do not hit the model context limit.
- **Optional:** the admin chat is mirrored into an LM Studio/Bionic project and can be continued
  right on the computer.
- **Optional:** an OpenAI-compatible API for other bots, protected by keys and reachable only over
  Tailscale. Their requests join the same queue (see "API for other bots").

### Requirements

- Windows
- Python 3.12+
- [LM Studio](https://lmstudio.ai) or Bionic with at least one downloaded model
- A Telegram bot created with [@BotFather](https://t.me/BotFather)
- [Tailscale](https://tailscale.com) – only for the API for other bots (see "Tailscale setup")

### Installation

1. Clone the repository, create a virtual environment and install the dependencies:
   ```bash
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
2. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
3. Copy `.env.example` to `.env` and fill in two lines:
   - `BOT_TOKEN` – the bot token;
   - `ADMIN_USERNAME` – your Telegram username without `@` (this person becomes the admin).
4. Run `start.bat` and send `/start` to the bot.

The bot starts the LM Studio server by itself. The path to the `lms` tool is detected
automatically; if needed, set it in `.env`.

### Commands

| Command | Who | What it does |
|---|---|---|
| `/new` | everyone | New chat, the bot forgets the current conversation |
| `/stop` | everyone | Stops the answer |
| `/help` | everyone | Help |
| `/setmodel` | admin | Choose the model with buttons |
| `/giveaccess @username` | admin | Grant access (a numeric id also works) |
| `/revokeaccess @username` | admin | Revoke access and delete the person's history |
| `/users` | admin | Who has access |
| `/status` | admin | Model, queue, number of users |

After `/giveaccess` the person has to message the bot first: a bot cannot start a chat.

### Settings (`.env`)

Only `BOT_TOKEN` and `ADMIN_USERNAME` are required. Everything else has defaults:

| Setting | Purpose |
|---|---|
| `DEFAULT_MODEL` | Model on the first start (later chosen with `/setmodel`) |
| `CONTEXT_LENGTH` | Model context size in tokens |
| `REPLY_MAX_TOKENS` | Minimum room for the answer; usually the model gets all free context |
| `REASONING_EFFORT` | Model reasoning: `none` turns it off and makes answers faster |
| `SUMMARY_MAX_TOKENS` | Limit for the summary of the old part of a conversation |
| `IDLE_UNLOAD_MINUTES` | Minutes of idle time before the model is unloaded (`0` – never) |
| `BIONIC_PROJECT` | LM Studio/Bionic project for mirroring the admin chat (empty – off) |
| `LM_BASE_URL`, `LMS_PATH` | Server address and path to `lms`, if they differ from the defaults |
| `API_HOST` | Address the API for other bots listens on (this computer's Tailscale address). Empty – API off |
| `API_PORT` | API port, `8765` by default |
| `API_KEYS` | Bot keys separated by commas, as `name:key` (a key is at least 24 characters) |
| `API_TIMEOUT` | Seconds an API request waits for the answer, including the queue, `180` by default |

### API for other bots

Another bot can ask the same model over the network, for example a bot running on a different
computer. Code: `bot/api.py`.

**How it works.** The bot sends `POST http://<API_HOST>:<API_PORT>/v1/chat/completions` with the
header `Authorization: Bearer <key>` and an OpenAI-style body: `{"messages": [{"role":
"system"|"user"|"assistant", "content": "text"}], "max_tokens": 800}`. The request joins the shared
queue with the Telegram users, and the model chosen with `/setmodel` answers. The answer comes back
in one piece, also in the OpenAI format (`choices[0].message.content`). These requests are not
stored in the database: the asking bot sends its own history.

Error answers: `401` – wrong key, `400` – wrong body format (text only, no images), `413` – the
conversation does not fit into the model context, `503` – the model is unavailable, `502` – the
model returned an empty answer, `504` – no answer within `API_TIMEOUT` (the request is removed from
the queue so the model does not work for nothing).

**Turning it on.**
1. Set up Tailscale (see below) and get this computer's address: `tailscale ip -4` (looks like `100.x.x.x`).
2. Create a key for the bot, for example:
   ```powershell
   .venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
3. In `.env` set `API_HOST=<address>` and `API_KEYS=<botname>:<key>`. Put the same key into the
   settings of the asking bot.
4. Allow the port in Windows Firewall for the Tailscale network only. In PowerShell as administrator:
   ```powershell
   New-NetFirewallRule -DisplayName "LibertyAPI (Tailscale)" -Direction Inbound -Protocol TCP -LocalPort 8765 -RemoteAddress 100.64.0.0/10 -Action Allow
   ```
5. Restart the bot. The log shows the line "API слушает 100.x.x.x:8765" ("API is listening").

If Tailscale is not up yet when the bot starts, the bot writes "API пока не запущен" ("API is not
started yet") to the log and retries every 30 seconds by itself. **Turning it off:** remove or
comment out `API_HOST` and restart the bot. To revoke access for one bot, remove its entry from
`API_KEYS`.

### Tailscale setup

Tailscale connects this computer and the computer of the other bot into a private network. The API
listens only on the Tailscale address, so it is invisible from the home network and from the
internet, and no ports need to be opened on the router.

1. Install Tailscale on both computers ([download](https://tailscale.com/download)) and sign in to
   the **same** account on both. On Linux: `curl -fsSL https://tailscale.com/install.sh | sh`,
   then `sudo tailscale up`.
2. Check that both computers see each other: `tailscale status` lists both. The address from
   `tailscale ip -4` stays the same for a device, so it can be written into `.env` once.
3. In the admin console ([login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines))
   open the menu (three dots) of this computer and choose **Disable key expiry**. Otherwise the key
   expires in about six months, the computer drops out of the network, and the API becomes
   unreachable.
4. After the API is turned on, check the connection from the other computer:
   `curl http://100.x.x.x:8765/` (or the same request from Python). Answer `404` means the computer
   is reachable: the API only has `POST /v1/chat/completions`. A timeout means a problem with
   Tailscale or the firewall rule.

If the other bot runs in Docker, its containers reach Tailscale addresses through the host, no
extra Docker settings are needed. Every device in your Tailscale network can reach the port, but
without a key from `API_KEYS` it only gets `401`.

### How it works

`start.bat` runs the bot and restarts it after a crash. The bot listens to Telegram, and all
requests go through one queue: it takes them one by one, starts the server and loads the model when
needed, streams the answer and saves it to its database. There is one model in memory, so a request
unloads any other model loaded by hand.

Code layout: `handlers.py` receives messages and checks access, `api.py` receives requests from
other bots, `worker.py` runs the queue and history compaction, `llm.py` manages models, `bionic.py`
keeps the shared chat, `db.py` stores data, `render.py` and `telegram_reply.py` format the answer.

### Data and privacy

Everything is stored locally in the `data/` folder (the `bot.db` database with users and
conversations, logs, a backup of the Bionic project). The token is in `.env`. **These files are not
part of the repository** (see `.gitignore`): do not publish them or share them with anyone.

The bot itself is neutral and adds no censorship: its behaviour and answers depend entirely on the
model you connect. Choosing the model and responsibility for its use lie with the owner.

### Logs

The bot writes logs in two places: the `start.bat` window and the file `data/bot.log`. The file is
size-limited: at 2 MB it is rotated (`bot.log.1`, `bot.log.2`), older ones are deleted. Log lines
are in Russian.

What goes into the log (INFO level and above):
- bot start and admin username, model loading and unloading, unloading after idle time;
- Bionic mirroring turned on or off;
- warnings: a message could not be updated or sent, an empty conversation summary, an answer could
  not be formatted as HTML;
- errors with details: model failure, request processing failure, Bionic problems;
- API for other bots: "API слушает …" on start, and for each request the bot name, number of
  messages and answer time (`API: viakitbot, сообщений 3, ответ за 12.4с`), rejections for a wrong
  key with the address, expired time limit, unavailable model.

Questions and answers are not written to the log, they are stored only in `data/bot.db`. API
requests are not stored anywhere.

How to read: open `data/bot.log` in any text editor. Every line starts with the time and level
(`INFO`, `WARNING`, `ERROR`). If the bot does not answer, look for `ERROR` and `WARNING` lines near
the end of the file. The last lines can be shown with:

```powershell
Get-Content data\bot.log -Tail 50
```

### Tests and checks

Automated tests are in `tests/` and need neither Telegram nor LM Studio: the model server and
Telegram are replaced with fakes. Run:

```bash
.venv\Scripts\python -m pytest
```

What is checked:
- **Access** (`tests/test_handlers.py`, `tests/test_units.py`): a stranger gets complete silence,
  groups are ignored, admin commands are closed to regular users, after `/revokeaccess` the bot is
  silent again and the history is deleted.
- **Queue and answers** (`tests/test_units.py`): requests go in order, one active request per
  person, `/stop` keeps the partial answer, contexts of different people do not mix, a long history
  is compacted, the bot survives a model error.
- **Formatting** (`tests/test_units.py`): HTML markup, splitting long answers into messages.
- **Files** (`tests/test_units.py`): TXT, PDF, DOCX are read, unsupported files are rejected.
- **Bionic** (`tests/test_bionic.py`): shared admin chat, a Bionic failure does not lose the
  answer, an old database is upgraded.
- **API** (`tests/test_api.py`): without a valid key 401 and the queue is untouched, the answer is in
  OpenAI format and nothing is written to the database, a wrong body 400, a too long conversation
  413, a short `max_tokens` is accepted, the answer length is at most `REPLY_MAX_TOKENS`, a model
  failure 503, the request waits in the shared queue behind a person, after the time limit 504 and
  the request is removed from the queue, the key is not printed in an error.

Success: all tests pass and the final pytest line is green (`passed`, no `failed` or `error`). Run
the tests before every commit and after changing the code.

Manual check after installation (with real Telegram and a model):
1. Run `start.bat`: the window shows the line "Бот @... запущен" ("bot started").
2. The admin sends `/start` and then any question: an answer arrives, the text appears gradually.
3. The admin runs `/status`: model, queue and number of users are shown.
4. The admin grants access with `/giveaccess @username`, a second person messages the bot and gets an answer.
5. A third person without access messages the bot: no answer, this is expected.
6. If the API is on: the log has "API слушает …". The other bot asks a question through the API, it
   gets an answer, and the LibertyAPI log shows a line `API: <botname>, …`.

---

<a id="русский"></a>

## Русский

Телеграм-бот для доступа к локальным LLM-моделям через [LM Studio](https://lmstudio.ai)
(и его сборку Bionic). Модели работают на твоём компьютере, а бот через Telegram даёт к ним
доступ выбранным людям. Белый IP и проброс портов не нужны – бот сам подключается к Telegram.

### Возможности

- **Доступ по приглашению.** Бот отвечает только тем, кому выдан доступ, остальным молчит.
- **Личный чат у каждого** со своим контекстом. `/new` начинает новый разговор.
- **Очередь.** Модель одна, поэтому запросы идут по одному; каждый видит своё место в очереди.
- **Ответ на лету.** Текст появляется постепенно, длинный ответ приходит несколькими
  сообщениями с разбивкой по границам предложений.
- **Вложения.** Текст, фото (если модель умеет их смотреть), файлы: TXT/Markdown/код, PDF, DOCX.
- **Команды админа.** Выбор модели и выдача доступа – только для одного админа.
- **Экономия памяти.** Модель выгружается из памяти после простоя и грузится обратно по запросу.
- **Длинные диалоги** автоматически сжимаются, чтобы не упираться в контекст модели.
- **Необязательно:** чат админа дублируется в проект LM Studio/Bionic, и его можно продолжить
  прямо на компьютере.
- **Необязательно:** API для других ботов в формате OpenAI, по ключу и только через Tailscale.
  Их запросы встают в ту же очередь (см. «API для других ботов»).

### Требования

- Windows
- Python 3.12+
- [LM Studio](https://lmstudio.ai) или Bionic хотя бы с одной скачанной моделью
- Телеграм-бот, созданный у [@BotFather](https://t.me/BotFather)
- [Tailscale](https://tailscale.com) – только для API для других ботов (см. «Настройка Tailscale»)

### Установка

1. Склонируй репозиторий, создай окружение и установи зависимости:
   ```bash
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
2. Создай бота у [@BotFather](https://t.me/BotFather) и скопируй токен.
3. Скопируй `.env.example` в `.env` и заполни две строки:
   - `BOT_TOKEN` – токен бота;
   - `ADMIN_USERNAME` – твой тег в Telegram без `@` (этот человек станет админом).
4. Запусти `start.bat` и напиши боту `/start`.

Сервер LM Studio бот запускает сам. Путь к утилите `lms` определяется автоматически; при
необходимости его можно задать в `.env`.

### Команды

| Команда | Кто | Что делает |
|---|---|---|
| `/new` | все | Новый чат, бот забывает текущий разговор |
| `/stop` | все | Останавливает ответ |
| `/help` | все | Подсказка |
| `/setmodel` | админ | Выбор модели кнопками |
| `/giveaccess @тег` | админ | Выдать доступ (подойдёт и числовой id) |
| `/revokeaccess @тег` | админ | Забрать доступ и удалить историю человека |
| `/users` | админ | Кто имеет доступ |
| `/status` | админ | Модель, очередь, число пользователей |

После `/giveaccess` человек должен сам написать боту: первым бот написать не может.

### Настройки (`.env`)

Обязательны только `BOT_TOKEN` и `ADMIN_USERNAME`. Остальное имеет значения по умолчанию:

| Параметр | Назначение |
|---|---|
| `DEFAULT_MODEL` | Модель при первом запуске (дальше выбирается через `/setmodel`) |
| `CONTEXT_LENGTH` | Размер контекста модели в токенах |
| `REPLY_MAX_TOKENS` | Минимум места под ответ; обычно модель получает всё свободное место контекста |
| `REASONING_EFFORT` | Рассуждения модели: `none` выключает их и ускоряет ответы |
| `SUMMARY_MAX_TOKENS` | Предел для пересказа старой части диалога |
| `IDLE_UNLOAD_MINUTES` | Через сколько минут простоя выгружать модель (`0` – не выгружать) |
| `BIONIC_PROJECT` | Проект LM Studio/Bionic для синхронизации чата админа (пусто – выключено) |
| `LM_BASE_URL`, `LMS_PATH` | Адрес сервера и путь к `lms`, если отличаются от стандартных |
| `API_HOST` | Адрес, на котором слушает API для других ботов (адрес компьютера в Tailscale). Пусто – API выключен |
| `API_PORT` | Порт API, по умолчанию `8765` |
| `API_KEYS` | Ключи ботов через запятую, вида `имя:ключ` (ключ не короче 24 символов) |
| `API_TIMEOUT` | Сколько секунд запрос по API ждёт ответа вместе с очередью, по умолчанию `180` |

### API для других ботов

Другой бот может спрашивать ту же модель по сети, например бот, который работает на другом
компьютере. Код: `bot/api.py`.

**Как работает.** Бот отправляет `POST http://<API_HOST>:<API_PORT>/v1/chat/completions` с
заголовком `Authorization: Bearer <ключ>` и телом в формате OpenAI: `{"messages": [{"role":
"system"|"user"|"assistant", "content": "текст"}], "max_tokens": 800}`. Запрос встаёт в общую
очередь с людьми из Telegram, отвечает модель, выбранная через `/setmodel`. Ответ приходит
целиком, тоже в формате OpenAI (`choices[0].message.content`). В базу такие запросы не
пишутся: свою историю спрашивающий бот присылает сам.

Ответы с ошибкой: `401` – неверный ключ, `400` – тело не в том формате (только текст, без
картинок), `413` – разговор не помещается в контекст модели, `503` – модель недоступна,
`502` – модель ответила пустотой, `504` – не успела за `API_TIMEOUT` (запрос снимается с
очереди, чтобы модель не работала впустую).

**Включение.**
1. Настрой Tailscale (см. ниже) и узнай адрес этого компьютера: `tailscale ip -4` (вида `100.x.x.x`).
2. Придумай ключ для бота, например так:
   ```powershell
   .venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
3. В `.env` впиши `API_HOST=<адрес>` и `API_KEYS=<имя_бота>:<ключ>`. Тот же ключ впиши
   в настройки спрашивающего бота.
4. Разреши порт в брандмауэре Windows только для сети Tailscale. В PowerShell от имени
   администратора:
   ```powershell
   New-NetFirewallRule -DisplayName "LibertyAPI (Tailscale)" -Direction Inbound -Protocol TCP -LocalPort 8765 -RemoteAddress 100.64.0.0/10 -Action Allow
   ```
5. Перезапусти бота. В логе появится строка «API слушает 100.x.x.x:8765».

Если при запуске Tailscale ещё не поднялся, бот пишет в лог «API пока не запущен» и сам
пробует снова каждые 30 секунд. **Выключение:** удали или закомментируй `API_HOST` и
перезапусти бота. Отозвать доступ одному боту: убери его запись из `API_KEYS`.

### Настройка Tailscale

Tailscale связывает этот компьютер и компьютер другого бота в закрытую сеть. API слушает
только адрес в Tailscale, поэтому его не видно ни из домашней сети, ни из интернета, и порты
на роутере открывать не нужно.

1. Установи Tailscale на оба компьютера ([скачать](https://tailscale.com/download)) и войди на
   обоих в **один и тот же** аккаунт. На Linux: `curl -fsSL https://tailscale.com/install.sh | sh`,
   затем `sudo tailscale up`.
2. Проверь, что компьютеры видят друг друга: `tailscale status` показывает оба. Адрес из
   `tailscale ip -4` у устройства постоянный, его можно один раз вписать в `.env`.
3. В админке ([login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines))
   открой меню (три точки) этого компьютера и выбери **Disable key expiry**. Иначе примерно
   через полгода ключ истечёт, компьютер выпадет из сети, и API станет недоступен.
4. Когда API включён, проверь связь с другого компьютера: `curl http://100.x.x.x:8765/` (или
   такой же запрос из Python). Ответ `404` значит, что компьютер виден: у API есть только
   `POST /v1/chat/completions`. Таймаут значит, что дело в Tailscale или в правиле брандмауэра.

Если другой бот работает в Docker, его контейнеры достают до адресов Tailscale через сам
компьютер, отдельно настраивать Docker не нужно. Достучаться до порта может любое устройство
из твоей сети Tailscale, но без ключа из `API_KEYS` оно получит только `401`.

### Как это работает

`start.bat` запускает бота и перезапускает его при сбое. Бот слушает Telegram, а все запросы
проходит через одну очередь: берёт по одному, при необходимости поднимает сервер и грузит модель,
генерирует ответ потоком и сохраняет его в свою базу. Модель одна в памяти, поэтому запрос
выгрузит любую другую, загруженную вручную.

Устройство кода: `handlers.py` принимает сообщения и проверяет доступ, `api.py` принимает
запросы других ботов, `worker.py` ведёт очередь и сжатие истории, `llm.py` управляет моделями,
`bionic.py` держит общий чат, `db.py` хранит данные, `render.py` и `telegram_reply.py`
оформляют ответ.

### Данные и приватность

Всё хранится локально в папке `data/` (база `bot.db` с пользователями и перепиской, логи,
резервная копия проекта Bionic). Токен лежит в `.env`. **Эти файлы в репозиторий не попадают**
(см. `.gitignore`) – не публикуй их и никому не передавай.

Бот сам по себе нейтрален и не накладывает цензуру – поведение и ответы полностью зависят от той
модели, которую ты подключишь. Выбор модели и ответственность за использование лежат на владельце.

### Логи

Бот пишет логи в двух местах: в окно `start.bat` и в файл `data/bot.log`. Файл ограничен по
размеру: при 2 МБ он архивируется (`bot.log.1`, `bot.log.2`), старше этого удаляется.

Что попадает в лог (уровень INFO и выше):
- запуск бота и тег админа, загрузка и выгрузка модели, выгрузка по простою;
- включение или отключение синхронизации с Bionic;
- предупреждения: не удалось обновить или отправить сообщение, пустой пересказ диалога,
  не вышло оформить ответ в HTML;
- ошибки с подробностями: сбой модели, сбой при обработке запроса, проблемы с Bionic;
- API для других ботов: «API слушает …» при запуске, по каждому запросу имя бота, число
  сообщений и время ответа (`API: viakitbot, сообщений 3, ответ за 12.4с`), отказ по неверному
  ключу с адресом, истёкший срок, недоступная модель.

Тексты вопросов и ответов в лог не пишутся, они хранятся только в базе `data/bot.db`.
Запросы по API не хранятся нигде.

Как читать: открой `data/bot.log` в любом текстовом редакторе. Каждая строка начинается со
временем и уровнем (`INFO`, `WARNING`, `ERROR`). Если бот не отвечает, ищи строки `ERROR` и
`WARNING` ближе к концу файла. Последние строки можно посмотреть командой:

```powershell
Get-Content data\bot.log -Tail 50
```

### Тесты и проверка работы

Автоматические тесты лежат в папке `tests/` и не требуют ни Telegram, ни LM Studio: сервер
модели и Telegram в них подменены. Запуск:

```bash
.venv\Scripts\python -m pytest
```

Что проверяется:
- **Доступ** (`tests/test_handlers.py`, `tests/test_units.py`): чужой человек получает полное
  молчание, группы игнорируются, админские команды закрыты для обычных пользователей, после
  `/revokeaccess` бот снова молчит, а история удалена.
- **Очередь и ответы** (`tests/test_units.py`): запросы идут по порядку, у человека один активный
  запрос, `/stop` сохраняет частичный ответ, контексты разных людей не смешиваются, длинная
  история сжимается, бот переживает ошибку модели.
- **Оформление** (`tests/test_units.py`): HTML-разметка, разбивка длинных ответов на сообщения.
- **Файлы** (`tests/test_units.py`): TXT, PDF, DOCX читаются, неподдерживаемые отклоняются.
- **Bionic** (`tests/test_bionic.py`): общий чат админа, сбой Bionic не теряет ответ,
  старая база обновляется.
- **API** (`tests/test_api.py`): без верного ключа 401 и очередь не тронута, ответ в формате
  OpenAI и в базу ничего не пишется, неверное тело 400, слишком длинный разговор 413, короткий
  `max_tokens` принимается, длина ответа не больше `REPLY_MAX_TOKENS`, сбой модели 503, запрос
  ждёт в общей очереди за человеком, по истечении срока 504 и запрос снимается с очереди, ключ
  не печатается в ошибке.

Успех: все тесты проходят, итоговая строка pytest зелёная (`passed`, без `failed` и `error`).
Запускай тесты перед каждым коммитом и после изменения кода.

Ручная проверка после установки (с настоящими Telegram и моделью):
1. Запусти `start.bat`: в окне должна появиться строка «Бот @... запущен».
2. Админ пишет боту `/start`, затем любой вопрос: приходит ответ, текст появляется постепенно.
3. Админ выполняет `/status`: видны модель, очередь и число пользователей.
4. Админ выдаёт доступ через `/giveaccess @тег`, второй человек пишет боту и получает ответ.
5. Третий человек без доступа пишет боту: ответа нет, это норма.
6. Если включён API: в логе есть «API слушает …». Другой бот задаёт вопрос через API, получает
   ответ, а в логе LibertyAPI появляется строка `API: <имя_бота>, …`.
