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
