# LEX — указатель документов

Обновлено 27.11.2026. В каталоге `docs/` **56 файлов `.md`**: этот указатель и
47 документов ниже. Остальные — промежуточные материалы спринта, которые
перечислять в указателе незачем (черновики, снимки состояния). Прежние числа
(37, 41, 54) устарели: файлов стало больше. Для знакомства начните с корневого
[`README.md`](../README.md), затем прочитайте архитектуру, модель и
ограничения.

| Документ | Назначение |
|---|---|
| [`ACCEPTANCE-CRITERIA.md`](ACCEPTANCE-CRITERIA.md) | Критерии приёмки поведения, репозитория и демонстрации. |
| [`API-CONTRACT.md`](API-CONTRACT.md) | Контракт HTTP API, поток событий и форматы данных. |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Схема LEX и путь от вопроса до ответа: 9 шагов, точки отказа, модель, права. |
| [`COMPONENTS.md`](COMPONENTS.md) | Компоненты проекта, повторное использование и лицензии. |
| [`DEMO-CHECKLIST.md`](DEMO-CHECKLIST.md) | Проверки перед записью демонстрации. |
| [`DEMO-FIXTURES-EXT.md`](DEMO-FIXTURES-EXT.md) | Расширенный набор демонстрационных сценариев с эталонами. |
| [`DEMO-FIXTURES.md`](DEMO-FIXTURES.md) | Основные классы демонстрационных примеров. |
| [`DEMO-SCRIPT.md`](DEMO-SCRIPT.md) | Реплики и последовательность демонстрационного видео. |
| [`DEMO-TIMING.md`](DEMO-TIMING.md) | Покадровый тайминг демонстрационного видео. |
| [`DEMO-VIDEO-PROMPT.md`](DEMO-VIDEO-PROMPT.md) | Производственная спецификация видео. |
| [`DEPLOY-NEXUSLINK.md`](DEPLOY-NEXUSLINK.md) | Подключение модели через NexusLink и переменные окружения. |
| [`DEPLOY-RESOURCES.md`](DEPLOY-RESOURCES.md) | Требования к железу стенда: RSS и пики сервисов, порог OOM, swap, минимальная RAM. |
| [`DIALOGUE-INTEGRATION.md`](DIALOGUE-INTEGRATION.md) | Интеграция диалогового состояния с обработкой запросов. |
| [`ERROR-HANDLING.md`](ERROR-HANDLING.md) | Поведение при недоступности моделей, корпуса и источников. |
| [`EVAL-HARNESS.md`](EVAL-HARNESS.md) | Протокол оценки поиска и качества ответов. |
| [`FAQ.md`](FAQ.md) | Ответы на частые вопросы о продукте и его границах. |
| [`IIKUZNITSA-ARCHITECTURE.md`](IIKUZNITSA-ARCHITECTURE.md) | Ранняя версия архитектурного описания проекта. |
| [`IIKUZNITSA-PITCH-OUTLINE.md`](IIKUZNITSA-PITCH-OUTLINE.md) | Черновая структура презентации. |
| [`IIKUZNITSA-PITCH-RAG.md`](IIKUZNITSA-PITCH-RAG.md) | Текст презентации поискового продукта. |
| [`INTEGRATION-CONFLUENCE.md`](INTEGRATION-CONFLUENCE.md) | Сценарий внедрения: источник корпуса — вики заказчика на Confluence (проектно, без реализации). |
| [`LEGACY-CLEANUP.md`](LEGACY-CLEANUP.md) | История очистки репозитория от устаревших материалов. |
| [`LEX-LIMITS.md`](LEX-LIMITS.md) | Границы продукта для жюри: что LEX не делает и почему это нормально, с числами. |
| [`LEX-ONE-PAGER.md`](LEX-ONE-PAGER.md) | Сводка для жюри: три критерия приёмки, метрики стенда, честные ограничения. |
| [`LEX-RERANK-ONNX.md`](LEX-RERANK-ONNX.md) | Решение по ONNX для реранкера: почему не делаем, с замерами. |
| [`LEX-RISKS-DEFENSE.md`](LEX-RISKS-DEFENSE.md) | Риски демонстрации и план Б: технические и организационные. |
| [`LIMITS.md`](LIMITS.md) | Реализованные функции, неподтверждённые места и ограничения. |
| [`METRICS.md`](METRICS.md) | Метрики оценки и критерии ручной проверки. |
| [`MODEL.md`](MODEL.md) | NexusLink для генерации и локальные модели поиска. |
| [`ONE-PAGE.md`](ONE-PAGE.md) | Краткое описание продукта для читателя или жюри. |
| [`ORGANIZER-ANSWERS-20261126.md`](ORGANIZER-ANSWERS-20261126.md) | Ответы организаторов и принятые продуктовые решения. |
| [`PITCH-SCRIPT.md`](PITCH-SCRIPT.md) | Текст устного выступления и вопросы к нему. |
| [`PITCH-VIDEO.md`](PITCH-VIDEO.md) | Сценарий короткого видео-питча. |
| [`PLAN-48H.md`](PLAN-48H.md) | План спринта с контрольными точками. |
| [`QA-FINAL.md`](QA-FINAL.md) | Итоговые проверки перед передачей проекта. |
| [`QA-QUESTIONS.md`](QA-QUESTIONS.md) | Вопросы для уточнения требований и ответов на них. |
| [`QA.md`](QA.md) | Шаблон оценки готового решения. |
| [`REPO-SUBMIT.md`](REPO-SUBMIT.md) | Чек-лист подготовки репозитория к публикации. |
| [`RISKS.md`](RISKS.md) | Основные риски и способы их снижения. |
| [`SAMPLE-QUESTIONS.md`](SAMPLE-QUESTIONS.md) | Вопросы для синтетического корпуса и ручной оценки. |
| [`SELF-REVIEW.md`](SELF-REVIEW.md) | Самопроверка полноты и качества реализации. |
| [`SPRINT-DAY1.md`](SPRINT-DAY1.md) | Состояние проекта и решения первого дня. |
| [`SPRINT-KICKOFF.md`](SPRINT-KICKOFF.md) | Проверки готовности и порядок начала спринта. |
| [`SPRINT-READINESS-20261126.md`](SPRINT-READINESS-20261126.md) | Снимок готовности перед началом спринта. |
| [`VIDEO-CHECKLIST.md`](VIDEO-CHECKLIST.md) | Техническая и содержательная проверка видео. |
| [`VIDEO-EXPORT.md`](VIDEO-EXPORT.md) | Формат и параметры экспорта итогового видео. |
| [`VIDEO-RENDER.md`](VIDEO-RENDER.md) | Сборка видео, озвучки и субтитров. |
| [`VIDEO-STORYBOARD.md`](VIDEO-STORYBOARD.md) | Основная раскадровка демонстрационного видео. |

## Куда идти по задаче

- **Первый запуск:** [`README.md`](../README.md), затем `MODEL.md`.
- **Изменение API или потока:** `API-CONTRACT.md`, `ARCHITECTURE.md`,
  `DIALOGUE-INTEGRATION.md`.
- **Оценка качества:** `EVAL-HARNESS.md`, `METRICS.md`, `LIMITS.md`.
- **Демонстрация и видео:** документы `DEMO-*` и `VIDEO-*`.
- **Публикация:** `COMPONENTS.md`, `REPO-SUBMIT.md`, корневой `LICENSE`.
