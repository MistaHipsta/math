# Moon Sisters HTTP collector

## Браузерный коллектор (рекомендуется)

`collect_moon_browser.py` на вход получает только URL игры. Каждый воркер
сам открывает Chromium-контекст, загружает страницу игры (только HTML, без
клиента и ассетов), берёт из неё demo-токен и endpoint, выполняет `login` →
`start` и отправляет `play` через `fetch` с той же страницы, то есть с
браузерными TLS, cookies и origin. Бонус доигрывается до конца. Любой сбой
(отказ, протухшая сессия, упавшая страница или браузер) приводит к новой
сессии, а воркер продолжает работу.

```powershell
python -m pip install playwright zstandard
python -m playwright install chromium

# один IP (свой), медленно
python .\tools\collect_moon_browser.py --workers 10 --rounds 10000 --rate 1

# через бесплатные прокси proxifly, свой IP к игре не обращается
python .\tools\collect_moon_browser.py --proxies proxifly --workers 20 --rounds 10000 --rate 0.5
```

### Прокси

`--proxies` принимает `proxifly` (бесплатный список
[proxifly/free-proxy-list](https://github.com/proxifly/free-proxy-list)), путь к
файлу или URL. Формат: JSON proxifly, JSON-массив строк или текст по строке на
прокси (`socks5://host:port`, `http://host:port`, `host:port`).

- список перечитывается каждые `--proxy-refresh` секунд (600);
- прокси проверяются в фоне (`--proxy-check-concurrency` 50) и только пока
  воркерам не хватает рабочих;
- каждый прокси — отдельный IP со своим `--rate` и своей паузой на 429;
- одна сессия идёт через один прокси (`--workers-per-proxy` 1), пока он жив;
- после `--proxy-max-failures` (2) сетевых сбоев подряд прокси выбрасывается;
- прокси, на котором игра ответила `PLAYER_LOCKOUT`, выбрасывается сразу.

### Бонусы не теряются

Сервер закрывает сессию примерно через **минуту** без запросов и дальше
отвечает `GAME_REOPENED`. Новый логин даёт нового demo-игрока, поэтому
незаконченный бонус после этого не вернуть. Чтобы успеть:

- если прокси умер посреди раунда, воркер переносит ту же сессию на другой
  прокси и повторяет **тот же** запрос с тем же `request_id`. Сервер на повтор
  отдаёт исходный ответ, шаг не пропускается и не дублируется;
- на перенос есть `--resume-window` (45 с) после последнего ответа.
  Пробуются `--resume-parallel` (4) прокси сразу, сначала уже проверенные;
  если свободных нет, сессия временно делит проверенный прокси с другим
  воркером (лимит на IP соблюдается);
- когда начинается бонус, заранее открываются `--bonus-hedges` (2) запасные
  страницы на других прокси. Если основной прокси умирает, запасная
  страница подхватывает сессию сразу, без загрузки страницы;
- шаги бонуса идут строго по одному. Отправка одного шага через несколько
  прокси сразу ломает сессию: медленная копия старого шага доходит после
  нового, и сервер отвечает `GAME_REOPENED`.

Каждый шаг проверяется по `context.last_action`, у каждого шага записан
`egress`. Переносы видны как события `migration` (`midBonus: true` для бонуса).
Раунд, который всё же умер, пишется только как `session_error` с шагами в
`abandonedSteps` и считается в `roundsAbandoned`; как `round_response` он не
записывается.

В артефакты незаконченные бонусы не попадают даже из старых или оборванных
файлов. `build_moon_artifacts.py` принимает бонус, только если цепочка
начинается со спина, каждый шаг совпадает с предложенным сервером действием,
последний шаг завершён и респины дошли до 0. Остальные раунды пропускаются
(id в артефактах остаются сплошными), их число и причины записываются в
`audit-report.json` → `rejectedIncompleteRounds`, `rejectedReasons`. Обрезанная
последняя строка файла (коллектор убит во время записи) тоже пропускается.

Во время бонуса живое поле, монеты и счётчик респинов лежат в
`context.bonus`, а `context.spins.board` остаётся полем запуска бонуса.
`build_moon_artifacts.py` берёт состояние шага из `context.bonus` и добавляет
в событие `phase: "bonus"`, `respinsLeft`, `coinCount`, `newCoins`.

TLS проверяется до самого сервера игры, так что прокси не может прочитать или
подменить ответы. Бесплатные прокси живут недолго: в пробном прогоне из 850
проверенных до игры дошли 63, а 20 из них потом отвалились. Сбои видны как
`session_error` в JSONL и не теряют раунды. Через какой прокси собран раунд,
записано в поле `egress`.

Важно про лимит: backend стоит за Cloudflare rate limit, который считается
**на IP для всего хоста** (`error code: 1015`, HTTP 429 с `Retry-After`
порядка 50 минут). Поэтому:

- `--rate` — бюджет запросов в секунду на один IP. Без прокси он общий на
  все воркеры, с прокси — на каждый прокси отдельно;
- открытие сессии стоит `--session-cost` (3) единицы бюджета;
- клиент игры, ассеты и телеметрия не загружаются, чтобы не тратить лимит;
- при 429 IP ставится на паузу на полный `Retry-After`, а сессия сразу
  переносится на другой IP. Ждать нельзя: за это время сессия умрёт.

### Мониторинг и долгий прогон

```powershell
# текущий отчёт по прогону (по умолчанию run-1m)
powershell -ExecutionPolicy Bypass -File .\status.ps1
powershell -ExecutionPolicy Bypass -File .\status.ps1 my-run

# то же без PowerShell, с автообновлением
python .\tools\run_status.py .\output\browser-runs\run-1m --watch 60
```

Отчёт: жив ли процесс, раунды, скорость за 10 минут и ETA, доля бонусов,
RTP, `abandoned` (бонусы, потерянные посреди игры), переносы сессий, число
рабочих прокси, размер на диске и время последней записи. `run_status.py`
дочитывает только новые строки, поэтому быстро работает на гигабайтах.

Долгий прогон лучше запускать через Планировщик Windows: так он не зависит от
окна терминала. Команда запуска лежит в `start_run.cmd` в корне проекта:

```powershell
$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$PWD\start_run.cmd`""
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 7) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName MoonSisters-run-1m -Action $a -Settings $s -Force
Start-ScheduledTask -TaskName MoonSisters-run-1m

# остановить
Stop-ScheduledTask -TaskName MoonSisters-run-1m
```

Задача работает, пока пользователь залогинен в Windows. Перезапуск с тем же
`--run-name` запрещён, если в папке уже есть данные.

Каждый раунд пишется как `round_response`, совместимый с
`build_moon_artifacts.py`. В `response.steps` сохраняются все запросы и
полные ответы `/play` (spin, bonus_init, respin…). Кроме этого пишутся
события `session_error`, `migration` и `throttled`. Прогон лежит в
`output/browser-runs/<run>/`, сводка в `run.json`.

## Stake и Artube выходы

Обычный запуск `collect_moon_http.py` теперь сохраняет каждый ответ бонусного
раунда в `response.playHistory` и по завершении строит два самостоятельных
артефакта:

```text
<run>/converted-artifact/stake/
  index.json
  books_base.jsonl.zst
  lookUpTable_base_0.csv
  manifest.json
<run>/converted-artifact/artube/
  index.json
  books_base.jsonl.zst
  lookUpTable_base_0.csv
  manifest.json
<run>/converted-artifact/audit-report.json
```

Перед первым полным запуском установите единственную зависимость конвертации:

```powershell
python -m pip install -r .\tools\requirements-artifacts.txt
```

Затем запускайте сбор как обычно:

```powershell
python .\tools\collect_moon_http.py `
  --url "<play-url>" `
  --token "<demo-token>" `
  --workers 10 `
  --rounds 3000000 `
  --run-name mooncoin-3m
```

Конвертация начинается после записи `run.json`, принимает только завершённые
раунды и сверяет число книг с `roundsCollected`. CSV содержит `id,1,payout`
для каждой книги в исходных целочисленных кредитах. Оба каталога используют
одинаковый `index.json` с одной базовой игрой; различаются их `events`:
Stake хранит стандартную запись раунда и состояния всех шагов, Artube —
Mooncoin payload с базовым полем, линиями, матрицами монет и историей действий.
Выплата в книге остаётся целым числом игровых кредитов, не суммой валюты GBR.

Если нужно только собрать RAW для другого анализа, укажите `--raw-only`.
Чтобы отдельно перестроить оба каталога для уже собранного запуска:

```powershell
python .\tools\build_moon_artifacts.py `
  --root .\output\http-runs\mooncoin-3m `
  --workers 10
```

Скрипт не заменяет непустые каталоги артефактов. Выходные папки будут созданы
целиком после сверки строк, CSV и суммы выплат. У старых сборов, где
`playHistory` отсутствует, бонус помечается как неполный; выдуманных состояний
респинов конвертер не создаёт. Для таких RAW скрипт всё равно может сформировать
две папки, но `manifest.json` обозначит неполную бонусную историю.

Сбор математики раундов напрямую через HTTP-API игры, без браузера и
Playwright. Каждый воркер открывает собственную игровую сессию, крутит спины
и доигрывает бонусные раунды до конца.

Скорость — около **75–90 раундов/с** на 10 воркерах против ~1 раунда/с
через браузерную автоматизацию.

## Требования

- Python 3.10+ (используются только модули стандартной библиотеки);
- сетевой доступ к backend игры.

Установка зависимостей не требуется.

## Что нужно перед запуском

Из DevTools браузера на странице игры возьмите два значения:

1. **URL** запроса `?gsc=play`, например:

   ```text
   https://betman-demo.head.3oaks.com/betman-demo/gs/moon_sisters/desktop/<session>/demo/?gsc=play
   ```

2. **token** из тела запроса `?gsc=login`.

Сегмент `<session>` в URL и `token` — временный session material. Не
коммитьте их в репозиторий и не публикуйте.

## Протокол

Один только `?gsc=play` не работает. Коллектор выполняет полную
последовательность автоматически:

| Шаг | Команда | Назначение |
| --- | --- | --- |
| 1 | `?gsc=login` | получить `session_id` и `huid` по токену |
| 2 | `?gsc=start` | активировать сессию с `mode` и `huid` |
| 3 | `?gsc=play` | спин со свежим `request_id` |
| 4 | `?gsc=play` | доигрывание бонуса: `bonus_init` → `respin` → `bonus_spins_stop` |

Без шага 2 каждый спин отклоняется с `SERVER_ERROR`. Без шага 4 теряются
бонусные выигрыши, а незавершённый раунд ломает сессию.

## 1. Сбор данных

### Пробный запуск

```powershell
python .\tools\collect_moon_http.py `
  --url "https://betman-demo.head.3oaks.com/betman-demo/gs/moon_sisters/desktop/<session>/demo/?gsc=play" `
  --token <token> `
  --workers 3 `
  --rounds 100 `
  --progress-every 50 `
  --run-name smoke
```

### 10 000 раундов на 10 воркерах

```powershell
python .\tools\collect_moon_http.py `
  --url "https://betman-demo.head.3oaks.com/betman-demo/gs/moon_sisters/desktop/<session>/demo/?gsc=play" `
  --token <token> `
  --workers 10 `
  --rounds 10000 `
  --progress-every 1000 `
  --run-name run-10000
```

### 3 000 000 раундов на 10 воркерах

```powershell
python .\tools\collect_moon_http.py `
  --url "https://betman-demo.head.3oaks.com/betman-demo/gs/moon_sisters/desktop/<session>/demo/?gsc=play" `
  --token <token> `
  --workers 10 `
  --rounds 3000000 `
  --progress-every 10000 `
  --run-name run-3m
```

Ориентировочно 11 часов и около 3.5 ГБ сырых JSONL. Прогресс печатается с
фактической скоростью и ETA. Остановка по `Ctrl+C` корректна: записанные
файлы остаются валидными.

Если сервер начнёт ограничивать частоту запросов, снизьте нагрузку:

```powershell
  --workers 6 --delay 0.05
```

### Параметры

| Параметр | По умолчанию | Назначение |
| --- | --- | --- |
| `--url` | — | URL `?gsc=play` |
| `--token` | — | токен для `?gsc=login` |
| `--workers` | `10` | число параллельных воркеров |
| `--rounds` | `10000` | **общий** лимит на все воркеры |
| `--run-name` | timestamp | имя каталога прогона |
| `--out-root` | `output/http-runs` | родительский каталог |
| `--bet-per-line` | `4` | ставка на линию |
| `--lines` | `25` | число линий |
| `--delay` | `0` | пауза между спинами, сек |
| `--timeout` | `20` | таймаут запроса, сек |
| `--max-retries` | `3` | повторы при сетевой ошибке |
| `--max-consecutive-failures` | `25` | стоп после N отказов подряд |
| `--max-logins` | `0` | лимит пересозданий сессии; `0` — без лимита |
| `--min-balance-spins` | `20` | обновить сессию, когда на балансе осталось меньше N ставок |
| `--max-bonus-steps` | `60` | предел действий на один бонусный раунд |
| `--progress-every` | `100` | частота вывода прогресса |
| `--sessions-file` | — | JSON-массив `{"url":..., "token":...}` для нескольких endpoint |

`--rounds` — общий лимит: при `--rounds 3000000 --workers 10` каждый воркер
соберёт примерно по 300 000 раундов.

### Устойчивость

- любой отклонённый спин (`GAME_REOPENED`, исчерпан баланс, `SERVER_ERROR`)
  вызывает открытие новой сессии на том же воркере;
- сессия обновляется заранее, до того как баланса перестанет хватать;
- отклонённые спины не засчитываются в собранные раунды;
- повторный запуск с существующим `--run-name` запрещён, чтобы два прогона
  не смешались в одном каталоге.

## 2. Результат сбора

```text
output/http-runs/<run-name>/
├── MoonSisters-worker-01-<runid>.jsonl
├── ...
├── MoonSisters-worker-10-<runid>.jsonl
└── run.json
```

Каждая строка JSONL — событие. Сырой ответ игры лежит в
`response.RawJson`. Типы событий: `round_response` (валидный раунд),
`unparsed_response` (отклонён), `round_error` (сетевая ошибка).

В `run.json` — сводка прогона: собрано раундов, отказы, скорость и
статистика по каждому воркеру. Session material редактируется.

## 3. Конвертация в MathArtifact books

По умолчанию конвертер создаёт
`output/http-runs/<run>/artifact-books.jsonl`. Каждая непустая строка — одна
компактная JSON-книга без внешней обёртки:

```json
{"board":[[1,7,8],[4,4,7]],"winLines":[{"id":"7","amount":25,"positions":[[0,1],[2,0]],"symbol":"Ж"}],"payout":25}
```

Ключи книги всегда идут в порядке `board`, `winLines`, `payout`. `board` —
исходный `list[list[int]]`; `winLines` содержит только `id`, `amount`,
`positions` и `symbol`, где позиция имеет вид `[reel,row]`; `payout` — только
целое число `>= 0`. JSON сериализуется компактно, с UTF-8 без экранирования
Unicode. `stake` используется как обязательная проверка исходного раунда, но
в книгу не записывается. Поле `weight` не добавляется (для обычного раунда
эффективный вес равен `1`).

```powershell
python .\tools\convert_moon_raw_to_round_results.py `
  --root .\output\http-runs\run-10000 `
  --workers 10
```

Опции `--output` и `--format` остаются совместимыми с прежним сценарием:

```powershell
python .\tools\convert_moon_raw_to_round_results.py `
  --root .\output\http-runs\run-10000 `
  --format round_result `
  --output .\output\http-runs\run-10000\round-results.jsonl
```

`--format artifact_book` (по умолчанию) пишет книги, а
`--format round_result` пишет прежний контейнер `round_result`, который
используется существующим анализатором. В artifact-режиме строки размером
более **96 000 байт в UTF-8** пропускаются и учитываются в счётчике
`oversized`. Malformed JSON, не-`play` события и записи без `board`, `stake`
или выплаты также пропускаются. CSV для artifact-книг не создаётся.

Минимальный C# streaming adapter читает книгу построчно, не загружая весь
JSONL в память:

```csharp
await foreach (var line in File.ReadLinesAsync(path))
{
    using var document = JsonDocument.Parse(line);
    var root = document.RootElement;
    var board = root.GetProperty("board");
    var payout = root.GetProperty("payout").GetInt32();
    var winLines = root.GetProperty("winLines");
    ConsumeBook(board, winLines, payout);
}
```

Для анализа используйте legacy-выход:

```powershell
python .\tools\analyze_moon_jsonl.py `
  .\output\http-runs\run-10000\round-results.jsonl `
  --json .\output\http-runs\run-10000\analysis.json `
  --csv .\output\http-runs\run-10000\rounds.csv
```

Анализатор намеренно сохраняет поддержку legacy `round_result`; для
агрегации книг конвертируйте тот же исходный сбор с
`--format round_result`. В `analysis.json` попадают агрегаты
(`total_stake`, `total_payout`, `rtp`, статистика символов) и массив `rounds`,
где у каждого раунда есть `stake`, `payout` и `symbols` с расположением на
гриде.

Для очень больших прогонов не используйте `--json`: анализатор держит все
раунды в памяти и включает их в отчёт. На 3 млн записей это потребует
десятки гигабайт RAM. Запускайте только консольный вывод агрегатов:

```powershell
python .\tools\analyze_moon_jsonl.py .\output\http-runs\run-3m\round-results.jsonl
```

## 5. Проверка целостности

```powershell
python .\tools\inspect_run.py .\output\http-runs\run-10000
python .\tools\inspect_rounds.py .\output\http-runs\run-10000
python .\tools\count_unique.py .\output\http-runs\run-10000
python .\tools\verify_balance.py .\output\http-runs\run-10000
python .\tools\inspect_wins.py .\output\http-runs\run-10000
```

Что означают инструменты:

| Скрипт | Проверяет | Признак корректного прогона |
| --- | --- | --- |
| [`inspect_run.py`](inspect_run.py) | структуру ответов | `missing={}` |
| [`inspect_rounds.py`](inspect_rounds.py) | завершённость раундов | `unfinished_rounds=0` |
| [`count_unique.py`](count_unique.py) | уникальность исходов | `unique_request_ids` равно числу раундов |
| [`verify_balance.py`](verify_balance.py) | учёт выплат по балансу сервера | `net` совпадает с `balance_drop` |
| [`inspect_wins.py`](inspect_wins.py) | поля выигрышей | `round_win` и `total_win` совпадают |

## Полный цикл одной командой

```powershell
$run = "run-10000"
python .\tools\collect_moon_http.py --url "<play-url>" --token "<token>" --workers 10 --rounds 10000 --progress-every 1000 --run-name $run
python .\tools\inspect_run.py .\output\http-runs\$run
python .\tools\convert_moon_raw_to_round_results.py --root .\output\http-runs\$run --workers 10 --format round_result --output .\output\http-runs\$run\round-results.jsonl
python .\tools\analyze_moon_jsonl.py .\output\http-runs\$run\round-results.jsonl --json .\output\http-runs\$run\analysis.json
```

При штатном запуске первый шаг уже создаёт оба игровых артефакта. Следующая
строка здесь строит только отдельный legacy-файл для анализатора.

## Известные ограничения

- Токен и session-сегмент URL живут ограниченное время. Если все воркеры
  падают на `handshake failed`, получите свежие значения из DevTools.
- Каталог `output/` исключён из Git, собранные данные не коммитятся.
- Инструмент предназначен только для demo-режима игры. Используйте его
  там, где это разрешено владельцем сервиса.
