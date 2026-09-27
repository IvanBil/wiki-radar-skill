# Контракт HTTP і CLI

Скрипт: `scripts/wiki_radar.py`. Залежностей поза стандартною бібліотекою Python 3 немає.

## User-Agent

```text
WikiRadar-Py/1.0 (Wikipedia topic-interest research; mailto:wiki-radar@example.com) python/3
```

Порожній `--user-agent` дає `missing-user-agent` і не відкриває мережу.

## Pageviews

`agent` за замовчуванням `user`, `access` — `all-access`, лише `monthly`.

```text
GET https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{project}/{access}/{agent}/{title}/{granularity}/{start}/{end}
GET https://wikimedia.org/api/rest_v1/metrics/pageviews/aggregate/{project}/{access}/{agent}/{granularity}/{start}/{end}
```

Title: пробіли стають `_`, потім URL-encode. `Jupiter (Planet)` дає `Jupiter_%28Planet%29`.

Дати — `yyyyMMdd00`. `start` — перше число початкового місяця. `end` — останній день останнього повного місяця. Якщо сьогодні 1–2 число, останній повний місяць — позаминулий.

`429` повторюється з паузою. Після вичерпання код відповіді — `rate-limited`.

## Резолв

Базова мова: `--base`, інакше за письмом. Немаркована латиниця дає `en`. Українські `іїєґ` дають `uk`.

Запити йдуть на `api.wikimedia.org`, не на `wikipedia.org/w/api.php`: пісочниця Claude Desktop отримує 403 від сайта Вікіпедії.

```text
GET https://api.wikimedia.org/core/v1/wikipedia/{lang}/page/{title}/bare
GET https://api.wikimedia.org/core/v1/wikipedia/{lang}/search/page?q=&limit=8
GET https://api.wikimedia.org/core/v1/wikipedia/{lang}/page/{title}/links/language
```

Точний title, потім пошук, потім мовні посилання. Дизамбіг не обирається. Немає мовної версії — `status: missing_in_language` і порожній title.

## Кеш

SQLite у робочій директорії запуску: `.wiki-radar/cache.sqlite`, або `--cache`. Повтор того самого URL без `--refresh` дає `cache: hit` і `http_requests: 0`.

## stdout

Успіх і помилка — один JSON. Помилка має `ok: false`, `code`, `message`, `hint`. `ambiguous-topic` завершується з кодом 2, `title-not-found` з кодом 3, `rate-limited` з кодом 4.
