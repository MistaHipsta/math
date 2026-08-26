# SlotAutoPlay

.NET 8 console application для автоматизации browser-сессий через
Microsoft.Playwright. Каждая сессия получает отдельные `BrowserContext` и
`Page` с фиксированным viewport.

> **Важно:** `BigMoneyConfig` содержит намеренно примерные URL и координаты.
> Перед запуском замените их на реальные значения игры. Фиктивные секреты,
> cookies и credentials в проект не добавляются.

## Структура

- `Models/AutomationModels.cs` — `ScreenRect`, `PlayConfig`, `PlayOptions`,
  `BrowserAutomation.WAIT_FOR_PAGE_LOAD` и pure click-point logic.
- `Configuration/GameConfigurations.cs` — provider base class и
  `BigMoneyConfig`.
- `Automation/HttpBrowserAutomation.cs` — Playwright lifecycle, navigation,
  clicks, screenshots и request listener.
- `Automation/SlotSessionRunner.cs` — цикл сессии и автоматический restart.
- `Infrastructure/SessionJsonlLogger.cs` — потокобезопасная запись JSONL.
- `../SlotAutoPlay.Tests` — unit tests для pure-логики.

## Требования

- .NET SDK 8.x;
- Chromium, установленный Playwright;
- доступ к URL игры.

## Локальная установка

Из корня workspace:

```powershell
dotnet restore .\SlotAutoPlay\SlotAutoPlay.csproj
dotnet build .\SlotAutoPlay\SlotAutoPlay.csproj
pwsh .\SlotAutoPlay\bin\Debug\net8.0\playwright.ps1 install chromium
```

На Windows также можно выполнить скрипт через `powershell`, если `pwsh` не
установлен:

```powershell
powershell -ExecutionPolicy Bypass -File `
  .\SlotAutoPlay\bin\Debug\net8.0\playwright.ps1 install chromium
```

## Запуск

`BASE_PATH` берётся из переменной окружения `SLOT_AUTOPLAY_BASE_PATH`.
Если переменная не задана, используется абсолютный путь
`output/slotautoplay` относительно текущего каталога.

Full mode:

```powershell
dotnet run --project .\SlotAutoPlay -- --jobs 1 --headed
```

Debug mode открывает страницу, ждёт load и `PageLoadDuration`, выполняет только
`OpenPageClicks`, создаёт screenshots и не запускает spin loop:

```powershell
dotnet run --project .\SlotAutoPlay -- --debug --headed
```

Несколько независимых worker-ов:

```powershell
$env:SLOT_AUTOPLAY_BASE_PATH = "D:\slotautoplay-data"
dotnet run --project .\SlotAutoPlay -- --jobs 3 --random-click --headed
```

Параметры:

- `--jobs N` — число worker-ов, default `1`; если ключ указан, `N` должен быть
  положительным целым числом, иначе приложение завершится с ошибкой и ненулевым
  кодом;
- `--debug` — только открытие и `OpenPageClicks`;
- `--headed` — показать браузер; default — headless;
- `--random-click` — случайная точка внутри прямоугольника; default — центр.

Остановка выполняется через `Ctrl+C`. После штатного или ошибочного завершения
сессии worker автоматически создаёт новую. Ошибка одного worker-а не завершает
остальные.

## Screenshots и JSONL

Для каждой сессии создаётся отдельный файл:

```text
<BASE_PATH>/<game>-worker-<NN>-<guid>.jsonl
```

Примеры screenshots:

```text
<session-id>-init.png
<session-id>-close.png
<session-id>-click-open-page.png
<session-id>-click-spin.png
```

Каждая строка JSONL — самостоятельный JSON-объект:

```json
{"timestamp":"2026-01-01T12:00:00+00:00","type":"session_started","sessionId":"bigmoney-worker-01-..."}
{"timestamp":"2026-01-01T12:00:05+00:00","type":"spin_request","sessionId":"...","url":"https://game.example/api/spin","method":"POST","postData":"...","requestUrl":"https://game.example/api/spin"}
{"timestamp":"2026-01-01T12:00:35+00:00","type":"error","sessionId":"...","message":"..."}
```

Для spin-запросов логируются timestamp, URL, HTTP method и условные
`postData`/`requestUrl`. Null-поля не записываются. Request listener обновляет
сетевую activity и передаёт запись в фоновую async-задачу, а response listener
обновляет response activity, поэтому click loop не блокируется. Запись защищена
async semaphore.

`LastResponseTimeout` отсчитывается после первого ответа `Page.Response` и
сбрасывается при каждом последующем response. Поэтому initial page load без
ответа не вызывает мгновенный timeout. Request и response listeners снимаются
до закрытия страницы, а pending request-log tasks завершаются до закрытия
logger.

`OpenPageActions` выполняется последовательно при открытии страницы. Элемент
`BrowserAutomation.WAIT_FOR_PAGE_LOAD` означает только дополнительную паузу
`PageLoadDuration` и не выполняет клик; обычный spin/check loop sentinel не
обрабатывает.

## Defaults и open questions

- URL и координаты в `BigMoneyConfig` — placeholders и требуют замены.
- `SpinRequestMarker = "spin"` — примерный фильтр URL; настройте уникальную
  часть endpoint API. Пустой marker считает spin любой request.
- viewport: `1280x720`;
- `PageLoadDuration`: 3 секунды дополнительной паузы после `load`;
- `IdleDuration`: 2 секунды;
- `CheckClick`: 1 секунда;
- `LastResponseTimeout`: 30 секунд;
- `SessionMaxDuration`: 30 минут;
- restart delay: 1 секунда.
- `WAIT_FOR_PAGE_LOAD` используется только внутри `OpenPageActions` как sentinel
  для дополнительной паузы, а не как замена Playwright load-state ожиданию.
- `UseIPhone` намеренно отсутствует.
- Клавиатурный ввод не используется; клики выполняются через absolute mouse
  coordinates.

## Добавление игры

1. Создайте класс-наследник `GameConfigProvider`.
2. Укажите `Url`, `OpenPageClicks`, `SpinButton` и `SpinRequestMarker`.
3. При необходимости переопределите viewport и тайминги.
4. Выберите provider в `Program.cs`.
5. Проверьте координаты командой `--debug --headed`.
6. После проверки запускайте full mode.

## Тесты

```powershell
dotnet test .\SlotAutoPlay.Tests\SlotAutoPlay.Tests.csproj
```

## Docker

Сборка:

```powershell
docker build -t slotautoplay .
```

Headless запуск с сохранением данных в `output`:

```powershell
docker run --rm `
  -e SLOT_AUTOPLAY_BASE_PATH=/data `
  -v "${PWD}\output:/data" `
  slotautoplay --jobs 1
```

Для остановки используйте `Ctrl+C` или `docker stop`. Headed режим в Docker
требует настроенного display/X server.

## Ограничения

Используйте automation только там, где это разрешено владельцем сайта и
правилами сервиса. Проект не реализует обход авторизации, CAPTCHA или
ограничений доступа.
## Сбор математики раундов

Захват раундов выключен по умолчанию и включается только явным флагом.
Приложение не отправляет ручные `POST`: pending spin создаётся перед реальным
Playwright `Spin` click, а request/response связываются по объекту
`response.Request`.

### Diagnostic-first workflow для Moon Sisters

Сначала запускайте одну headed-сессию на короткий срок, не более 60 секунд:

```powershell
dotnet run --project .\SlotAutoPlay -- `
  --game moon-sisters --headed --jobs 1 `
  --diagnostic-network --duration-seconds 30
```

В diagnostic mode после реальных Spin clicks записываются безопасные network
metadata и JSON candidates. Asset URLs (JavaScript, CSS, изображения, шрифты,
sourcemaps и favicon) исключаются. Пока host/path authoritative endpoint не
настроены, события называются `network_candidate` и `unparsed_response`, а не
`round_request`, `round_response` или `round_result`.

Сессия с `--debug` останавливается после navigation/splash setup и потому не
подходит для поиска response после Spin:

```powershell
dotnet run --project .\SlotAutoPlay -- `
  --game moon-sisters --debug --headed --jobs 1
```

### Определение authoritative endpoint

Откройте JSONL в
`output/slotautoplay/moon-sisters/<session>.jsonl` и для нескольких кликов
сопоставьте одинаковый `correlationId` у `spin_click`, `network_candidate` и
`unparsed_response`. Authoritative response должен:

1. иметь стабильные host, exact path и HTTP method;
2. быть JSON response с успешным HTTP status;
3. приходить после каждого реального Spin;
4. содержать фактическое состояние раунда, а не asset, config или telemetry.

Не используйте один только substring `spin` как доказательство endpoint.

### Mapping и включение capture

После получения обезличенного fixture добавьте в
`MoonSistersRoundParser` только подтверждённые allowlist paths для round/
transaction id, stake, payout, balance, currency, reels/symbols/positions и
bonus/free-spin/feature state. Для неподтверждённых или неполных данных parser
должен возвращать `Unparsed` или `Partial`; значения нельзя выдумывать.
Увеличивайте parser version при изменении mapping.

`MoonSistersConfig` сохраняет исходные URL/output subfolder и координаты splash/spin.
Для текущей схемы Moon Sisters authoritative route классифицируется по:

- host `betman-demo.head.3oaks.com`;
- path template `/betman-demo/gs/moon_sisters/desktop/{session}/demo/`, где
  `{session}` — динамический opaque-сегмент, который не сохраняется в конфигурации
  и логах;
- method `POST`;
- request content type `text/plain`;
- response content type `application/json`.

`MoonSistersRoundParser` имеет status **implemented / partial schema**, version
`1.0`. Подтверждённый `play` mapping содержит `context.spins.round_bet`,
`context.last_win`, `context.round_finished`, `user.balance`,
`user.currency` и `context.spins.board`. В response не подтверждены `roundId`,
win lines и feature/free-spin state, поэтому они остаются `null`/пустыми и не
используются для расчёта. `sync` responses классифицируются как `Unparsed`.

Короткий нормализованный smoke capture выполняется одним headed worker:

```powershell
dotnet run --project .\SlotAutoPlay -- `
  --game moon-sisters --headed --jobs 1 `
  --capture-round-data --duration-seconds 60
```

Analyzer выводит только агрегаты и не печатает URL, query values, cookies,
authorization или raw body:

```powershell
python .\tools\analyze_moon_jsonl.py `
  .\output\slotautoplay\moon-sisters\<smoke-session>.jsonl
```

Не публикуйте исходный session URL: его динамический сегмент и query-параметры
являются временным session material. Используйте только обезличенный route
template и redacted JSONL metadata.
Его `RoundParser` уже явно указывает extension point, но endpoint host/path
намеренно не заданы, поэтому capture по умолчанию не классифицирует assets или
неизвестные responses как раунды.

После подтверждения endpoint задайте exact host/path в provider и запускайте
только явно:

```powershell
dotnet run --project .\SlotAutoPlay -- `
  --game moon-sisters --headed --jobs 1 `
  --capture-round-data --max-response-bytes 1048576 `
  --duration-seconds 30
```

Параметры `--capture-round-data`, `--diagnostic-network`,
`--max-response-bytes N` (1024..16777216, default 1 MiB) не изменяют прежние
`--duration-seconds`, `--game`, `--jobs`, `--headed` и `--debug`. Следующий spin
не начинается, пока предыдущий pending request/response не завершён или не
получен `round_timeout`.

JSONL содержит `schemaVersion` в каждой строке и события:

- `session_started`, `spin_click`;
- `round_request`, `round_response`, `round_result`;
- `network_candidate`, `unparsed_response`;
- `round_timeout`, `round_error`.

Raw JSON читается только для подходящего JSON response и ограничивается
`--max-response-bytes`. Перед записью рекурсивно redacted поля с именами
`token`, `session`, `authorization`, `auth`, `cookie`, `secret`, `signature`,
`mgckey`, `password`, `credential`, `apikey`. URL сохраняется только до path;
cookies, Authorization, session query values и post body не записываются.

### Analyzer

`RoundAnalyzer.ReadJsonl(path)` и `RoundAnalyzer.Analyze(rounds)` в
`Models/RoundData.cs` — отдельный analyzer, не требующий браузера. Он учитывает
только `round_result` со статусом `Parsed` и неотрицательными stake/payout,
пропуская malformed JSON, `Partial`/`Unparsed`, timeout и error. Результат
содержит count, total stake, total payout, RTP (`payout / stake`, либо `null`
при нулевой суммарной ставке), а также распределения symbols/features.

```csharp
var rounds = RoundAnalyzer.ReadJsonl(
    @"output\slotautoplay\moon-sisters\session.jsonl");
var summary = RoundAnalyzer.Analyze(rounds);
Console.WriteLine(
    $"{summary.Count} rounds; stake={summary.TotalStake}; " +
    $"payout={summary.TotalPayout}; RTP={summary.Rtp}");
```

Не коммитьте raw JSONL, screenshots или fixtures с session URL, cookies,
токенами либо другими секретами. Session URL может истечь.