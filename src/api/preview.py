"""Превью источника для кликабельных цитат (контракт §9).

GET /api/doc/{doc_id}/preview?page=N отдаёт картинку страницы или сам файл.
Пока корпус текстовый (.md) — отдаём сам файл как text/markdown с заголовком
Content-Disposition: inline, чтобы UI мог показать источник. Для image/scan/mixed
файлов (png/jpg/pdf) отдаём файл с его Content-Type; PDF-страница рендерится в
PNG, только если в окружении есть pymupdf (иначе 501 с честным объяснением).
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from . import config

DOC_EXTS = {".md", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".docx"}


def _manifest_index() -> dict[str, dict]:
    try:
        import json
        data = json.loads((config.CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
        return {d["doc_id"]: d for d in data.get("documents", []) if d.get("doc_id")}
    except Exception:
        return {}


def resolve(doc_id: str) -> Path | None:
    """Найти файл документа по doc_id: из манифеста, затем перебором по префиксу."""
    meta = _manifest_index().get(doc_id)
    if meta and meta.get("file"):
        p = config.CORPUS_DIR / meta["file"]
        if p.is_file():
            return p
    # запасной путь: файл начинается с doc_id
    for p in sorted(config.CORPUS_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in DOC_EXTS and p.name.startswith(doc_id):
            return p
    return None


def content_type_for(path: Path) -> str:
    ct, _ = mimetypes.guess_type(path.name)
    if ct:
        return ct
    return "application/octet-stream"


def pdf_page_png(path: Path, page: int) -> tuple[bytes, str] | None:
    """Отрисовать страницу PDF в PNG, если есть pymupdf. None — движка нет."""
    try:
        import fitz  # pymupdf
    except Exception:
        return None
    try:
        doc = fitz.open(str(path))
        idx = max(0, page - 1)
        if idx >= doc.page_count:
            return None
        pix = doc.load_page(idx).get_pixmap(dpi=150)
        return pix.tobytes("png"), "image/png"
    except Exception:
        return None


def ocr_available() -> bool:
    import shutil
    exe = shutil.which("tesseract")
    if not exe:
        for cand in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",):
            if Path(cand).is_file():
                exe = cand
                break
    if not exe:
        return False
    # русский язык обязателен: без него OCR для корпуса бесполезен
    import os
    import subprocess
    env = dict(os.environ)
    tessdata = os.environ.get("TESSDATA_PREFIX") or r"D:\models\tessdata"
    if Path(tessdata).is_dir():
        env["TESSDATA_PREFIX"] = tessdata
    try:
        out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True,
                             timeout=10, env=env)
        return "rus" in (out.stdout or "")
    except Exception:
        return False
