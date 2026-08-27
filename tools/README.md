# Moon Sisters HTTP collector

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

## 3. Конвертация в нормализованный формат

```powershell
python .\tools\convert_moon_raw_to_round_results.py `
  --root .\output\http-runs\run-10000 `
  --workers 10 `
  --output .\output\http-runs\run-10000\round-results.jsonl
```

Конвертер раскладывает `context.spins.board` в массив символов с
координатами `Reel`, `Row`, `Column`, `Index` и берёт выплату из
`round_win` → `total_win` → `last_win`.

## 4. Анализ и отчёт

```powershell
python .\tools\analyze_moon_jsonl.py `
  .\output\http-runs\run-10000\round-results.jsonl `
  --json .\output\http-runs\run-10000\analysis.json `
  --csv .\output\http-runs\run-10000\rounds.csv
```

В `analysis.json` попадают агрегаты (`total_stake`, `total_payout`, `rtp`,
статистика символов) и массив `rounds`, где у каждого раунда есть `stake`,
`payout` и `symbols` с расположением на гриде.

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
python .\tools\convert_moon_raw_to_round_results.py --root .\output\http-runs\$run --workers 10 --output .\output\http-runs\$run\round-results.jsonl
python .\tools\analyze_moon_jsonl.py .\output\http-runs\$run\round-results.jsonl --json .\output\http-runs\$run\analysis.json
```

## Известные ограничения

- Токен и session-сегмент URL живут ограниченное время. Если все воркеры
  падают на `handshake failed`, получите свежие значения из DevTools.
- Каталог `output/` исключён из Git, собранные данные не коммитятся.
- Инструмент предназначен только для demo-режима игры. Используйте его
  там, где это разрешено владельцем сервиса.