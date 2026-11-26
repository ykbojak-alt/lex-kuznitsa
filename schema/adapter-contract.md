# Контракт адаптера источника документов

> Цель: дать агенту единый способ получать документы независимо от того, где они лежат (файлы, PostgreSQL, MongoDB, будущая БД заказчика).

## 1. Интерфейс

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Protocol


@dataclass(frozen=True)
class DocumentRef:
    doc_id: str
    title: str
    doc_kind: str
    department: str
    issued_at: date | None
    effective_at: date | None = None
    rule_key: str | None = None
    version: str | None = None
    source_uri: str | None = None
    acl_departments: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DocumentRecord(DocumentRef):
    text: str = ""
    sections: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)


class DocumentSource(Protocol):
    """Единый read-only интерфейс для RAG-контура."""

    def list_docs(self, role: str, *, as_of: date | None = None) -> Iterable[DocumentRef]:
        """Вернуть документы, видимые роли. Никогда не возвращать чужие ACL-документы."""

    def get_doc(self, doc_id: str, role: str) -> DocumentRecord | None:
        """Вернуть метаданные + секции + текст, если роль имеет доступ; иначе None."""

    def get_text(self, doc_id: str, role: str) -> str | None:
        """Вернуть нормализованный текст документа для индексации/цитирования."""

    def search_fields(self, query: str, role: str, *, fields: list[str] | None = None,
                      as_of: date | None = None, limit: int = 20) -> list[DocumentRef]:
        """Поиск по полям (title/kind/department/rule_key/text...). Реализация может быть SQL, Mongo, BM25, vector."""
```

## 2. Правила контракта

1. **ACL deny-by-default.** Если у документа нет `acl_departments`/маппинга, адаптер не отдаёт его обычной роли.
2. **`department` обязателен.** Запись без department попадает в quarantine/import report; в `list_docs/get_doc` для обычной роли не отдаётся.
3. **Стабильный `doc_id`.** Один и тот же логический документ/редакция имеет тот же `doc_id` между импортами.
4. **Нормализованные даты.** Адаптер преобразует даты в `date`/ISO `YYYY-MM-DD`.
5. **Текст изолирован от ACL.** `get_text` проверяет ACL так же, как `get_doc`.
6. **No write.** Адаптер RAG-контура read-only. Импорт/ETL — отдельный процесс.
7. **Auditability.** Каждый ответ должен позволять восстановить `source_uri`, `doc_id`, `hash/version`.

## 3. Реализации

### 3.1 FileSource (текущая, готова для demo)

Источник: `sample-corpus/manifest.json` + Markdown файлы.

```python
class FileSource:
    def __init__(self, corpus_dir: str, manifest_name: str = "manifest.json"):
        self.corpus_dir = corpus_dir
        self.manifest_name = manifest_name

    # list_docs: читает manifest.documents, фильтрует по role->departments
    # get_doc:   читает .md по file, возвращает text + sections + metadata
    # get_text:  возвращает текст .md
    # search_fields: простой BM25/substring по title/text/kind/department/rule_key
```

Особенности текущего corpus:
- `acl_departments` явного нет: временный fallback `acl_departments=[department]`.
- `effective_at` для SR-редакций берётся из `issued`/`Действует с`.
- `status` вычисляется по `conflict_rules` и `as_of`.
- `rule_key` пустой для не-конфликтных документов.

### 3.2 PostgresSource (заготовка под БД заказчика)

```python
class PostgresSource:
    def __init__(self, dsn: str, *, schema: str = "public"):
        self.dsn = dsn
        self.schema = schema

    def list_docs(self, role: str, *, as_of: date | None = None) -> Iterable[DocumentRef]:
        # SELECT ... FROM documents d
        # JOIN document_versions v ON v.id = d.current_version_id
        # JOIN document_acl a ON a.document_id = d.id
        # JOIN role_departments rd ON rd.department_id = a.department_id
        # WHERE rd.role = :role AND (v.effective_from IS NULL OR v.effective_from <= :as_of)
        raise NotImplementedError

    def get_doc(self, doc_id: str, role: str) -> DocumentRecord | None:
        raise NotImplementedError

    def get_text(self, doc_id: str, role: str) -> str | None:
        raise NotImplementedError

    def search_fields(self, query: str, role: str, *, fields=None, as_of=None, limit=20):
        # MVP: SQL ILIKE/tsvector по разрешённым полям
        # Next: hybrid BM25/vector через отдельный индекс
        raise NotImplementedError
```

Ожидаемые таблицы: `documents`, `document_versions`, `sections`, `document_acl`, `role_departments` (см. `schema/source-schema.md`).

### 3.3 MongoSource (заготовка)

```python
class MongoSource:
    def __init__(self, uri: str, db: str, collection: str = "documents"):
        self.uri = uri
        self.db = db
        self.collection = collection

    def list_docs(self, role: str, *, as_of: date | None = None) -> Iterable[DocumentRef]:
        # filter: {"acl.departments": {"$in": departments_for_role(role)}}
        # as_of: effectiveFrom <= as_of
        raise NotImplementedError

    def get_doc(self, doc_id: str, role: str) -> DocumentRecord | None:
        raise NotImplementedError

    def get_text(self, doc_id: str, role: str) -> str | None:
        raise NotImplementedError

    def search_fields(self, query: str, role: str, *, fields=None, as_of=None, limit=20):
        # MVP: $text / regex по разрешённым полям; Next: Atlas Search/vector
        raise NotImplementedError
```

## 4. ACL-модель

Минимальный контракт для роли:

```python
@dataclass(frozen=True)
class RolePolicy:
    role: str
    departments: tuple[str, ...]  # пустой tuple => deny all
```

Правила:
- `list_docs` возвращает только документы, где пересечение `acl_departments ∩ role.departments` непустое.
- `get_doc/get_text` при отсутствии доступа возвращают `None` (не ошибку утечки).
- Админский bypass — только отдельный явный режим, не дефолтный путь агента.

## 5. Контракт поиска

`search_fields` принимает `fields`, чтобы агент мог целиться:

| Field | Назначение |
|---|---|
| `title` | быстрый поиск по названию/теме |
| `text` | полнотекстовый поиск |
| `doc_kind`, `doc_subtype` | фильтр по типу |
| `department` | фильтр по подразделению |
| `rule_key` | группа редакций одного правила |
| `issued_at`, `effective_at` | фильтр актуальности |

Агент не должен искать «по всему подряд», если вопрос указывает на тип/подразделение/правило — это снижает ложные срабатывания.

## 6. Ошибки и карантин

Адаптер не «чинит молча» плохие данные. Он возвращает отчёт импорта:

```python
@dataclass
class ImportIssue:
    source_uri: str
    severity: str  # error|warning
    code: str      # MISSING_DEPARTMENT|BAD_DATE|NO_TEXT|DUP_DOC_ID|...
    message: str
```

Критические ошибки (`MISSING_DEPARTMENT`, `NO_TEXT`, `DUP_DOC_ID`) блокируют публикацию записи в поисковый индекс до исправления.
