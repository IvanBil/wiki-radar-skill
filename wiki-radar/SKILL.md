---
name: wiki-radar
description: Compare Wikipedia reader interest across languages with Wikimedia pageviews and a one-page PDF. Use when the user asks which launch language fits a B2C topic, whether interest in a topic is growing, or for a pageviews trend. Triggers include Wikipedia, pageviews, Вікіпедія, інтерес до теми, мова запуску, B2C, and a PDF report.
---

# wiki-radar

Скрипт уже рахує метрики. Цитуйте JSON зі stdout. Не оцінюйте pageviews з пам'яті. Не пишіть новий код.

## Чекліст

1. Витягніть тему словами користувача, коди мов і період. Якщо період не названо, візьміть 24 місяці.
2. Якщо мова формулювання інша, ніж мови порівняння, передайте її в `--base`. Українське формулювання дає `uk`. Латиниця без діакритики дає `en`.
3. Запустіть одну команду `run`. Не розкладайте її на fetch, analyze, chart і report.
4. Кожне число у відповіді має бути в JSON або у файлі з `--out`.
5. `missing_in_language` або `title-not-found`: title немає. Не вигадуйте його.
6. Назвіть індекс, confidence, reasons, caveat і шляхи до PDF та PNG. Title беріть лише з JSON.

## Команда

OUT_DIR — порожній каталог. У чаті Claude Desktop шлях відносний до цієї теки. У Cowork і Claude Code замініть початок на `${CLAUDE_SKILL_DIR}/scripts/wiki_radar.py`.

```text
python3 scripts/wiki_radar.py run --topic "TOPIC" --langs pl,cs --months 24 --out OUT_DIR
```

## Прапорці

| Прапорець | Навіщо |
|---|---|
| `--topic` | Тема словами користувача |
| `--langs` | Коди мов через кому |
| `--base` | Мова формулювання теми |
| `--months` | Скільки місячних бакетів, типово 24 |
| `--out` | Куди писати PDF, PNG, SVG і JSON |
| `--pick` | Точний title після неоднозначної теми |
| `--resume` | Ідентифікатор study з попереднього JSON |
| `--add-langs` | Додати мови до наявного study |
| `--same-period-as` | Нова тема, той самий start і end |
| `--refresh` | Ігнорувати HTTP-кеш |

## Гілки помилок

| code | Що робити |
|---|---|
| `ambiguous-topic` | Показати `candidates` і `next_checks`. Зупинитись. Не будувати тренд. Повтор лише після вибору користувача, з `--pick`. |
| `title-not-found` | Title немає. Не вигадуйте його. |
| `rate-limited` | Зачекати і повторити `run --resume STUDY_ID --out OUT_DIR`. |
| `missing_in_language` | Title немає. Не вигадуйте його. |
| `api-error` | Показати `message` і `hint` як є. Не підставляти числа з пам'яті. |

`trend` зі значенням false означає, що малюнок не є графіком pageviews.

## Повтор

```text
python3 scripts/wiki_radar.py run --resume STUDY_ID --add-langs sk --out OUT_DIR
```

```text
python3 scripts/wiki_radar.py run --topic "TOPIC2" --langs uk --same-period-as STUDY_ID --out OUT_DIR
```

## Як цитувати

Беріть локальні title і `matched_via` з JSON. Порівнюйте мови за `index_last` і `share_change_ratio`. Повторіть `caveat`. Назвіть шляхи `pdf` і `png` з JSON.
