"""Мультимодальный корпус: тип содержимого и OCR (контракт §9, v1.1).

Задача: БД заказчика разной формы — текст, картинки, сканы. Агент обязан
**различать** их и говорить об этом в консоли (шаг ``inspect``), а не делать вид,
что всё текст.

Честная граница: OCR — внешний движок, мы его не обучаем. Если движка в
окружении нет, сканы помечаются ``ocr_unavailable``, и в ``inspect`` пишется
«сканы не обрабатываются». Выдуманный «распознанный текст» хуже, чем его
отсутствие: на нём строится цитата в юридическом ответе.

Что реально работает без OCR: определение типа по расширению и по наличию
текстового слоя в PDF (pymupdf), рендер страницы для ``preview_url``, извлечение
текста из PDF-страницы с текстовым слоем. Это ровно то, что контракт называет
``mixed``: часть страниц текстовые, часть — картинки.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
TEXT_EXT = {".md", ".txt", ".rst", ".csv", ".json", ".yaml", ".yml", ".html"}
PDF_EXT = {".pdf"}
DOCX_EXT = {".docx", ".doc"}

# Порог контракта §9: ниже 0.6 цитата помечается «распознан неуверенно».
LOW_CONFIDENCE = 0.6
# Порог «скан распознан плохо»: меньше 100 символов со страницы.
DEGRADED_CHARS = 100


@dataclass
class OCRResult:
    """Результат распознавания одной страницы/картинки."""

    text: str = ""
    confidence: float | None = None
    engine: str = "none"
    available: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.available and bool(self.text.strip())

    @property
    def degraded(self) -> bool:
        return self.ok and len(self.text.strip()) < DEGRADED_CHARS

    @property
    def low_confidence(self) -> bool:
        return self.confidence is not None and self.confidence < LOW_CONFIDENCE


class OCR:
    """Интерфейс распознавания. Реализации — ниже.

    Контракт фиксирует сигнатуру: ``extract(path, page) -> (text, confidence)``.
    Возвращаем объект, а не кортеж: кроме двух полей нужен признак «движка нет»,
    иначе вызывающий не отличит «пусто» от «не умеем».
    """

    name = "none"

    @property
    def available(self) -> bool:  # pragma: no cover - тривиально
        return False

    def extract(self, path: str | Path, page: int | None = None) -> OCRResult:
        raise NotImplementedError

    def describe(self) -> str:
        return "OCR недоступен" if not self.available else f"OCR {self.name}"


class NullOCR(OCR):
    """Заглушка: честно говорит, что распознавать нечем."""

    name = "none"

    @property
    def available(self) -> bool:
        return False

    def extract(self, path: str | Path, page: int | None = None) -> OCRResult:
        return OCRResult(
            text="", confidence=None, engine=self.name, available=False,
            reason="ocr_unavailable",
        )

    def describe(self) -> str:
        return "сканы не обрабатываются (OCR не подключён)"


class TesseractOCR(OCR):
    """Рабочий движок, если он есть в окружении.

    Подключается сам, когда стоит ``pytesseract`` и бинарник ``tesseract``.
    Контракт требует «готовый движок, не пиши свой» — поэтому своей реализации
    распознавания здесь нет и не будет.
    """

    name = "tesseract"

    def __init__(self) -> None:
        self._pt = None
        try:
            import pytesseract  # type: ignore

            pytesseract.get_tesseract_version()
            self._pt = pytesseract
        except Exception:
            self._pt = None

    @property
    def available(self) -> bool:
        return self._pt is not None

    def extract(self, path: str | Path, page: int | None = None) -> OCRResult:
        if not self.available:
            return NullOCR().extract(path, page)
        assert self._pt is not None
        try:
            from PIL import Image  # type: ignore
        except Exception:
            return OCRResult(available=False, engine=self.name, reason="ocr_unavailable")
        try:
            img = Image.open(path)
            if page is not None and getattr(img, "n_frames", 1) > 1:
                img.seek(page)
            data = self._pt.image_to_data(img, output_type=self._pt.Output.DICT, lang="rus+eng")
            words = [w for w in data.get("text", []) if w and w.strip()]
            confs = [float(c) for c in data.get("conf", []) if str(c).lstrip("-").isdigit() and float(c) >= 0]
            text = " ".join(words)
            conf = (sum(confs) / len(confs) / 100.0) if confs else None
            return OCRResult(text=text, confidence=conf, engine=self.name, available=True)
        except Exception as exc:  # распознавание не должно ронять запрос
            return OCRResult(available=False, engine=self.name, reason=f"ocr_error: {exc}")


def default_ocr() -> OCR:
    tess = TesseractOCR()
    return tess if tess.available else NullOCR()


# ------------------------------------------------------------------ pdf/pdf-текст


def _pymupdf():
    try:
        import pymupdf  # type: ignore

        return pymupdf
    except Exception:
        try:
            import fitz  # type: ignore

            return fitz
        except Exception:
            return None


@dataclass
class PageInfo:
    page: int
    chars: int
    has_image: bool = False
    text: str = ""

    @property
    def is_text(self) -> bool:
        return self.chars >= DEGRADED_CHARS

    @property
    def content_type(self) -> str:
        if self.chars >= DEGRADED_CHARS:
            return "text"
        return "scan" if not self.has_image else "image"


@dataclass
class DocShape:
    """Форма одного элемента корпуса, как её увидел inspect."""

    path: Path
    content_type: str
    pages: list[PageInfo] = field(default_factory=list)
    ocr: OCRResult | None = None
    note: str = ""

    @property
    def text_chars(self) -> int:
        return sum(p.chars for p in self.pages)


def inspect_file(path: str | Path, ocr: OCR | None = None) -> DocShape:
    """Определяет тип содержимого файла.

    Правила (контракт §9):
    * расширение картинки -> ``image``;
    * текст по расширению -> ``text``;
    * PDF -> постранично: все страницы с текстовым слоем -> ``text``,
      ни одной -> ``scan``, вперемешку -> ``mixed``;
    * docx без извлечения — ``text`` (текст есть, структуру не разбираем).
    """
    p = Path(path)
    ext = p.suffix.lower()
    ocr = ocr or default_ocr()

    if ext in IMAGE_EXT:
        res = ocr.extract(p)
        note = "" if res.ok else ("ocr_unavailable" if not res.available else "")
        return DocShape(path=p, content_type="image", ocr=res, note=note)

    if ext in TEXT_EXT:
        text = p.read_text(encoding="utf-8", errors="replace")
        return DocShape(
            path=p, content_type="text",
            pages=[PageInfo(page=1, chars=len(text), has_image=False, text=text)],
        )

    if ext in DOCX_EXT:
        return DocShape(path=p, content_type="text", note="текст без постраничного разбора")

    if ext in PDF_EXT:
        mupdf = _pymupdf()
        if mupdf is None:
            return DocShape(path=p, content_type="scan", note="pdf без библиотеки разбора")
        doc = mupdf.open(str(p))
        pages: list[PageInfo] = []
        try:
            for i, page in enumerate(doc):
                text = page.get_text("text") or ""
                imgs = page.get_images(full=False) or []
                pages.append(
                    PageInfo(page=i + 1, chars=len(text.strip()), has_image=bool(imgs), text=text)
                )
        finally:
            doc.close()

        kinds = {pg.content_type for pg in pages}
        if kinds == {"text"}:
            ctype = "text"
        elif "text" in kinds and len(kinds) > 1:
            ctype = "mixed"
        elif kinds == {"image"}:
            ctype = "image"
        else:
            ctype = "scan"

        # Без текстового слоя пробуем OCR — честно, через интерфейс.
        ocr_res: OCRResult | None = None
        if ctype in ("scan", "image", "mixed"):
            ocr_res = ocr.extract(p, pages[0].page if pages else None)
        note = "" if (ocr_res is None or ocr_res.ok) else str(ocr_res.reason or "")
        return DocShape(path=p, content_type=ctype, pages=pages, ocr=ocr_res, note=note)

    return DocShape(path=p, content_type="text", note=f"неизвестное расширение {ext}, считаю текстом")


def render_page(path: str | Path, page: int = 1, *, zoom: float = 2.0) -> bytes | None:
    """PNG-байты страницы для ``GET /api/doc/{id}/preview``. None — не умеем."""
    p = Path(path)
    if p.suffix.lower() in IMAGE_EXT:
        try:
            return p.read_bytes()
        except OSError:
            return None
    if p.suffix.lower() not in PDF_EXT:
        return None
    mupdf = _pymupdf()
    if mupdf is None:
        return None
    try:
        doc = mupdf.open(str(p))
        try:
            if page < 1 or page > doc.page_count:
                return None
            pg = doc[page - 1]
            pix = pg.get_pixmap(matrix=mupdf.Matrix(zoom, zoom))
            return pix.tobytes("png")
        finally:
            doc.close()
    except Exception:
        return None


@dataclass
class Inspection:
    """Итог шага ``inspect`` по корпусу."""

    text: int = 0
    images: int = 0
    scans: int = 0
    mixed: int = 0
    degraded: int = 0
    ocr_available: bool = False
    ocr_note: str = ""
    shapes: list[DocShape] = field(default_factory=list)

    @property
    def detail(self) -> str:
        parts = [
            f"текст {self.text}",
            f"картинки {self.images}",
            f"сканы {self.scans}",
            f"смешанные {self.mixed}",
        ]
        body = " · ".join(parts)
        tail = ""
        if self.scans or self.images or self.mixed:
            if not self.ocr_available:
                tail = f" · {self.ocr_note or 'сканы не обрабатываются'}"
            elif self.degraded:
                tail = f" (плохо распознано: {self.degraded})"
        return body + tail


def inspect_corpus(root: str | Path, ocr: OCR | None = None) -> Inspection:
    """Разбор всех элементов корпуса — источник данных шага ``inspect``."""
    base = Path(root)
    ocr = ocr or default_ocr()
    insp = Inspection(ocr_available=ocr.available, ocr_note=ocr.describe())
    if not base.exists():
        return insp

    skip_dirs = {".git", "__pycache__", "var", "node_modules"}
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        # Служебные файлы корпуса — не элементы БД заказчика.
        if path.name in {"manifest.json", "questions.jsonl", "INDEX.md"}:
            continue
        if path.suffix.lower() not in (IMAGE_EXT | TEXT_EXT | PDF_EXT | DOCX_EXT):
            continue
        shape = inspect_file(path, ocr)
        insp.shapes.append(shape)
        if shape.content_type == "image":
            insp.images += 1
        elif shape.content_type == "scan":
            insp.scans += 1
        elif shape.content_type == "mixed":
            insp.mixed += 1
        else:
            insp.text += 1
        if shape.ocr is not None and (shape.ocr.degraded or shape.ocr.low_confidence):
            insp.degraded += 1
    return insp
