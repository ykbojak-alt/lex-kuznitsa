"""Формы документов и проверка заполнения (контракт §13.3-§13.4, v1.5).

Форма — не придуманный бланк, а **то, что уже описано в регламенте**. Поля
берутся из разделов «Требования к оформлению результата» и «Приёмочные
критерии» плюс из правил SR-* (срок, сумма, порог). Если требование к полю в
корпусе не описано — поле помечается ``hint = "формат не описан в регламенте"``,
и никакой формат не выдумывается. Это то же правило §12, применённое к формам.

Пять проверок §13.4:
  1. обязательность;
  2. формат (pattern/тип);
  3. значение из справочника;
  4. согласованность с регламентом — главная: сверка с другим документом,
     со ссылкой на него;
  5. полнота — чего не хватает до готовности формы.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .corpus import Corpus
from .models import Rule

FieldType = str  # text | date | money | choice | number

# Форматы, которые в корпусе реально описаны. Всё остальное — «не описано».
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONEY_HEAD_RE = re.compile(r"(\d[\d\s ]*(?:[.,]\d{1,2})?)\s*(?:руб|₽|рублей|р\.)", re.IGNORECASE)


@dataclass
class FormField:
    key: str
    label: str
    type: FieldType = "text"
    required: bool = False
    pattern: str | None = None
    example: str | None = None
    hint: str = ""
    options: list[str] = field(default_factory=list)
    min: float | None = None
    max: float | None = None
    # §12: чем обосновано требование — иначе это выдумка.
    source_doc: str = ""
    source_quote: str = ""

    def to_dict(self) -> dict:
        out = {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "hint": self.hint,
        }
        if self.pattern:
            out["pattern"] = self.pattern
        if self.example:
            out["example"] = self.example
        if self.options:
            out["options"] = list(self.options)
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        if self.source_doc:
            out["source_doc"] = self.source_doc
            out["source_quote"] = self.source_quote
        return out


@dataclass
class Form:
    form_id: str
    title: str
    doc_id: str
    fields: list[FormField] = field(default_factory=list)
    format: str = "md"
    business_line: str = ""

    def to_dict(self) -> dict:
        return {
            "form_id": self.form_id,
            "title": self.title,
            "doc_id": self.doc_id,
            "fields": [f.to_dict() for f in self.fields],
            "format": self.format,
            "business_line": self.business_line,
        }


# ------------------------------------------------------------------ реестр


def _forms_path(root: Path) -> Path:
    return Path(root) / "forms" / "forms.json"


def load_forms(root: str | Path) -> list[Form]:
    """Формы из ``forms/forms.json`` рядом с корпусом.

    Формы лежат данными, а не кодом: их правит владелец формы, а не программист.
    Отсутствие файла — не ошибка: корпус может прийти без форм.
    """
    path = _forms_path(Path(root))
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    out: list[Form] = []
    for item in raw if isinstance(raw, list) else raw.get("forms", []):
        out.append(
            Form(
                form_id=str(item.get("form_id") or ""),
                title=str(item.get("title") or ""),
                doc_id=str(item.get("doc_id") or ""),
                format=str(item.get("format") or "md"),
                business_line=str(item.get("business_line") or ""),
                fields=[
                    FormField(
                        key=str(f.get("key") or ""),
                        label=str(f.get("label") or ""),
                        type=str(f.get("type") or "text"),
                        required=bool(f.get("required")),
                        pattern=f.get("pattern") or None,
                        example=f.get("example") or None,
                        hint=str(f.get("hint") or ""),
                        options=[str(o) for o in (f.get("options") or [])],
                        min=f.get("min"),
                        max=f.get("max"),
                        source_doc=str(f.get("source_doc") or ""),
                        source_quote=str(f.get("source_quote") or ""),
                    )
                    for f in (item.get("fields") or [])
                ],
            )
        )
    return [f for f in out if f.form_id]


def get_form(root: str | Path, form_id: str) -> Form | None:
    return next((f for f in load_forms(root) if f.form_id == form_id), None)


# ------------------------------------------------------------ согласованность


@dataclass
class ConsistencyRule:
    """Сверка значения с правилом из ДРУГОГО документа (§13.4 п.4)."""

    field_key: str
    rule_key: str
    compare: str  # "gt" | "ge" | "lt" | "le"
    threshold: float
    doc_id: str
    message: str


def threshold_from_corpus(corpus: Corpus, rule_key: str) -> tuple[float | None, str, str]:
    """Числовой порог из значения правила («150 000 рублей» -> 150000.0)."""
    rule: Rule | None = corpus.rules.get(rule_key)
    if rule is None:
        return None, "", ""
    rev = rule.revisions[-1] if rule.revisions else None
    if rev is None:
        return None, "", ""
    match = MONEY_HEAD_RE.search(rev.value)
    if not match:
        return None, rev.doc_id, rev.value
    number = re.sub(r"[\s ]", "", match.group(1)).replace(",", ".")
    try:
        return float(number), rev.doc_id, rev.value
    except ValueError:
        return None, rev.doc_id, rev.value


def consistency_rules(corpus: Corpus) -> list[ConsistencyRule]:
    """Правила согласованности, выведенные из корпуса, а не из головы.

    Связка: поле формы «сумма» сверяется с порогом правила SR-03 (порог
    автоматического одобрения) — это ровно тот случай из контракта: «если сумма
    выше порога из другого документа, предупредить со ссылкой на него».
    """
    rules: list[ConsistencyRule] = []
    value, doc_id, quote = threshold_from_corpus(corpus, "SR-03")
    if value is not None:
        rules.append(
            ConsistencyRule(
                field_key="amount",
                rule_key="SR-03",
                compare="gt",
                threshold=value,
                doc_id=doc_id,
                message=(
                    f"сумма выше порога автоматического одобрения {quote} — "
                    f"согласование пойдёт по другому порядку"
                ),
            )
        )
    return rules


# ------------------------------------------------------------------ проверка


@dataclass
class ValidationResult:
    ok: bool
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    filled: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    progress: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "filled": self.filled,
            "missing": self.missing,
            "progress": self.progress,
        }


def _norm_money(raw: str) -> float | None:
    cleaned = re.sub(r"[^\d.,\-]", "", str(raw)).replace(",", ".")
    if cleaned.count(".") > 1:
        # «1.234.567» — разделители тысяч, последняя точка может быть дробной.
        head, _, tail = cleaned.rpartition(".")
        cleaned = head.replace(".", "") + ("." + tail if tail else "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def validate(form: Form, values: dict, *, corpus: Corpus | None = None) -> ValidationResult:
    """Проверяет заполнение формы по пяти правилам §13.4."""
    errors: list[dict] = []
    warnings: list[dict] = []
    filled: dict = {}
    missing: list[str] = []

    {f.key: f for f in form.fields}
    consistency = consistency_rules(corpus) if corpus is not None else []

    for f in form.fields:
        raw = values.get(f.key)
        empty = raw is None or (isinstance(raw, str) and not raw.strip())
        if empty:
            filled[f.key] = None
            if f.required:
                missing.append(f.key)
                errors.append({
                    "field": f.key,
                    "message": "поле обязательное",
                    "expected": _expected_hint(f),
                })
            continue

        # 2. Формат — только если он описан в регламенте (иначе не выдумываем).
        text = str(raw).strip()
        if f.type == "money":
            amount = _norm_money(text)
            if amount is None:
                errors.append({
                    "field": f.key,
                    "value": text,
                    "message": "сумма не распознана как число",
                    "expected": f.example or "число",
                })
                filled[f.key] = None
                continue
            filled[f.key] = amount
            if f.min is not None and amount < f.min:
                errors.append({"field": f.key, "value": text,
                               "message": f"ниже минимума {f.min}", "expected": str(f.min)})
            if f.max is not None and amount > f.max:
                errors.append({"field": f.key, "value": text,
                               "message": f"выше максимума {f.max}", "expected": str(f.max)})
            # 4. Согласованность с регламентом — со ссылкой на документ.
            for rule in consistency:
                if rule.field_key != f.key:
                    continue
                if (rule.compare == "gt" and amount > rule.threshold) or \
                   (rule.compare == "ge" and amount >= rule.threshold):
                    warnings.append({
                        "field": f.key,
                        "message": rule.message,
                        "doc_id": rule.doc_id,
                    })
            continue

        if f.type == "date":
            if not DATE_RE.match(text):
                errors.append({
                    "field": f.key, "value": text,
                    "message": "дата не в формате ГГГГ-ММ-ДД",
                    "expected": f.example or "2026-03-01",
                })
                filled[f.key] = None
                continue
            filled[f.key] = text
            continue

        if f.pattern:
            if not re.match(f.pattern, text):
                errors.append({
                    "field": f.key, "value": text,
                    "message": f"не соответствует формату: {f.hint or f.pattern}",
                    "expected": f.example or f.pattern,
                })
                filled[f.key] = None
                continue
            filled[f.key] = text
            continue

        # 3. Значение из справочника.
        if f.type == "choice" and f.options:
            if text not in f.options:
                errors.append({
                    "field": f.key, "value": text,
                    "message": "значение вне справочника",
                    "expected": ", ".join(f.options[:4]) + (" …" if len(f.options) > 4 else ""),
                })
                filled[f.key] = None
                continue
            filled[f.key] = text
            continue

        filled[f.key] = text

    total = len(form.fields)
    done = sum(1 for f in form.fields if filled.get(f.key) not in (None, ""))
    return ValidationResult(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        filled=filled,
        missing=missing,
        progress=f"{done} из {total} полей",
    )


def _expected_hint(f: FormField) -> str:
    if f.example:
        return f.example
    if f.type == "date":
        return "дата в формате ГГГГ-ММ-ДД"
    if f.hint:
        return f.hint
    return "значение"
