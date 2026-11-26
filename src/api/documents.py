"""Документы целиком, скачивание и превью (§13.2, §9).

Права проверяются здесь же: документ вне области роли — 403 с причиной,
не 404. Сотрудник должен знать, что документ существует, но не положен.

Рендер markdown — через ``markdown_it``, если он есть; иначе честный ``<pre>``.
Никакой логики поиска тут нет.
"""
from __future__ import annotations

import html
import mimetypes
import re
from pathlib import Path

from . import core

TEXT_EXT = {".md", ".txt", ".rst", ".csv", ".json", ".yaml", ".yml", ".html"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
PDF_EXT = {".pdf"}
DOCX_EXT = {".docx", ".doc"}


class AccessDenied(Exception):
    """Документ существует, но роли не положен — это 403, не 404."""

    def __init__(self, doc_id: str, reason: str) -> None:
        super().__init__(reason)
        self.doc_id = doc_id
        self.reason = reason


def resolve(doc_id: str) -> Path | None:
    return core.doc_path(doc_id)


def content_type_for(path: Path) -> str:
    ct, _ = mimetypes.guess_type(path.name)
    if ct:
        return ct
    if path.suffix.lower() in TEXT_EXT:
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


def check_access(doc_id: str, role_id: str) -> dict:
    """Метаданные документа, если роль имеет к нему доступ. Иначе — AccessDenied."""
    meta = core.doc_meta(doc_id)
    if meta is None:
        raise FileNotFoundError(doc_id)
    department = str(meta.get("department") or "")
    if department and not core.role_can_read(role_id, department):
        raise AccessDenied(
            doc_id,
            f"документ относится к отделу «{department}»; роли «{role_id}» он не положен",
        )
    return meta


def _md_to_html(text: str) -> str:
    try:
        from markdown_it import MarkdownIt

        return MarkdownIt("commonmark", {"html": False, "linkify": False}).render(text)
    except Exception:
        return f"<pre>{html.escape(text)}</pre>"


def _anchor(title: str) -> str:
    low = title.lower().strip()
    low = re.sub(r"[^\w\s-]", "", low, flags=re.UNICODE)
    return re.sub(r"[\s_]+", "-", low)


def document_html(doc_id: str, role_id: str) -> str:
    """Документ целиком: оглавление + текст. §13.2."""
    meta = check_access(doc_id, role_id)
    path = resolve(doc_id)
    if path is None:
        raise FileNotFoundError(doc_id)

    title = html.escape(str(meta.get("title") or doc_id))
    if path.suffix.lower() in TEXT_EXT:
        raw = path.read_text(encoding="utf-8", errors="replace")
        body = _md_to_html(raw)
        toc = _toc_from_html(body)
    elif path.suffix.lower() in IMAGE_EXT:
        body = (f'<p><img src="/api/doc/{doc_id}/preview?page=1" '
                f'alt="{title}" style="max-width:100%"></p>')
        toc = ""
    elif path.suffix.lower() in PDF_EXT:
        body = (f'<p><object data="/api/doc/{doc_id}/download" type="application/pdf" '
                f'width="100%" height="800"></object></p>')
        toc = ""
    else:
        body = ('<p>Предпросмотр этого формата недоступен. '
                f'<a href="/api/doc/{doc_id}/download">Скачать файл</a>.</p>')
        toc = ""

    meta_rows = []
    for key, label in (("department", "Отдел"), ("system", "Система"),
                       ("kind", "Тип"), ("owner", "Владелец"),
                       ("version", "Версия"), ("issued", "Издан"),
                       ("rule_key", "Правило")):
        value = meta.get(key)
        if value:
            meta_rows.append(f"<dt>{label}</dt><dd>{html.escape(str(value))}</dd>")

    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>{title} — LEX</title>
<style>
 body{{font:16px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;max-width:860px;
      margin:2rem auto;padding:0 1rem;color:#1b1f24}}
 h1{{font-size:1.6rem;margin:0 0 .3rem}} h2{{font-size:1.2rem;margin-top:2rem}}
 nav{{background:#f5f6f8;border-radius:10px;padding:1rem 1.2rem;margin:1.5rem 0}}
 nav ol{{margin:.3rem 0 0 1.2rem;padding:0}} nav a{{color:#2b5fd9;text-decoration:none}}
 dl{{display:grid;grid-template-columns:max-content 1fr;gap:.25rem 1rem;font-size:.9rem}}
 dt{{color:#667}} dd{{margin:0}}
 .bar{{margin:1rem 0}} .bar a{{margin-right:1rem;color:#2b5fd9}}
 pre{{background:#f5f6f8;padding:1rem;border-radius:8px;overflow:auto}}
 table{{border-collapse:collapse}} td,th{{border:1px solid #ddd;padding:.35rem .6rem}}
</style></head><body>
<div class="bar"><a href="/api/doc/{doc_id}/download">Скачать исходный файл</a>
<a href="/api/doc/{doc_id}/preview?page=1">Превью</a></div>
<h1>{title}</h1>
<dl>{''.join(meta_rows)}</dl>
{('<nav><strong>Оглавление</strong><ol>' + toc + '</ol></nav>') if toc else ''}
<article>{body}</article>
</body></html>"""


def _toc_from_html(body: str) -> str:
    items = []
    for _level, title in re.findall(r"<h([1-3])[^>]*>(.*?)</h\1>", body, re.S):
        text = re.sub(r"<[^>]+>", "", title).strip()
        if text:
            items.append(f'<li><a href="#{_anchor(text)}">{html.escape(text)}</a></li>')
    return "".join(items)


def download_name(doc_id: str) -> str:
    path = resolve(doc_id)
    return path.name if path else f"{doc_id}.md"


def pdf_page_png(path: Path, page: int) -> tuple[bytes, str] | None:
    """Страница PDF в PNG силами ядра (`lex.media.render_page`)."""
    m = core.core_modules()["media"]
    fn = getattr(m, "render_page", None) if m is not None else None
    if fn is None:
        return None
    try:
        data = fn(path, page)
    except Exception:
        return None
    return (data, "image/png") if data else None


def preview_bytes(doc_id: str, page: int) -> tuple[bytes, str] | None:
    """Байты превью: картинка как есть, PDF-страница — рендером."""
    path = resolve(doc_id)
    if path is None:
        return None
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXT:
        try:
            return path.read_bytes(), content_type_for(path)
        except OSError:
            return None
    if suffix in PDF_EXT:
        return pdf_page_png(path, page)
    return None


def ocr_available() -> bool:
    m = core.core_modules()["media"]
    fn = getattr(m, "default_ocr", None) if m is not None else None
    if fn is None:
        return False
    try:
        ocr = fn()
        langs = getattr(ocr, "langs", None) or getattr(ocr, "languages", None)
        if langs is not None:
            return "rus" in langs or "rus" in str(langs)
        return bool(getattr(ocr, "available", True))
    except Exception:
        return False


def ocr_engine() -> str:
    m = core.core_modules()["media"]
    if m is None:
        return "none"
    try:
        ocr = m.default_ocr()
        return type(ocr).__name__.replace("OCR", "").lower() or "tesseract"
    except Exception:
        return "none"
