# deploy — стенд LEX на удалённом хосте

Юнит `lex-stand.service` + `deploy-sagepl.sh`. Стенд слушает **127.0.0.1:8090**
(наружу не выставлен; доступ — через ssh-туннель или реверс-прокси хостера).

**Копируется:** код (`src/`, `demo/`, `scripts/`, `schema/`, `deploy/`, `docs/`,
`sample-corpus/`) и метаданные (`pyproject.toml`, `README.md`, `LICENSE`).
**НЕ копируется:** `dataset/`, `corpus-real/`, `sample-corpus-real/` — документы
заказчика, данные третьей стороны: на чужую машину они не уходят ни в какую
сторону; `.env` — секреты, на стенде свой файл; `var/` — рабочие записи стенда;
`.git/`, `.venv/`, `__pycache__/` — пересобираются на месте.

**Запуск:** `./deploy/deploy-sagepl.sh root@<хост> /opt/lex-kuznitsa`.
**Проверка после деплоя:** `ssh <хост> 'curl -sf http://127.0.0.1:8090/api/health'`
(скрипт делает это сам и печатает ответ); при провале —
`journalctl -u lex-stand -n 50 --no-pager`.
**Откат:** `ssh <хост> 'systemctl disable --now lex-stand'`, затем `git revert`
нужного коммита и повторный прогон скрипта. `.env` и `var/` откат не трогает.

**Требования к хосту.** Минимум 4 ГБ RAM и **swap не меньше 4 ГБ** — два
локальных модельных сервиса (`bge-m3` + `bge-reranker-base`) в пике занимают
~3 ГиБ, и без swap ядро убивает процесс посреди запроса. Рекомендуется 8 ГБ
RAM. Замеры, порог OOM и минимальные лимиты юнитов —
[`../docs/DEPLOY-RESOURCES.md`](../docs/DEPLOY-RESOURCES.md).

**Проверка здоровья стенда (три команды, 5 секунд):**

```bash
ssh <хост> 'systemctl is-active lex-stand lex-embed lex-rerank; free -m; swapon --show'
```

Ожидаемое: три `active`, `Swap` не меньше 4 ГБ, в `swapon` — один файл на 4 ГБ.
Сторож `lex-watchdog.timer` зовёт `/api/health` каждые 2 минуты; лог —
`/var/log/lex-watchdog.log`.
