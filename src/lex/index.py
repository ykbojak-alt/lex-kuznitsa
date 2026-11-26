"""Индекс: BM25 в памяти + векторный поиск (bge-m3, :8081) + гибрид.

Контракт §5: эмбеддинги — ``POST http://127.0.0.1:8081/v1/embeddings``, модель
bge-m3, dim 1024. Векторная БД не нужна: 300 чанков — это numpy-матрица,
которая умножается на вектор вопроса за микросекунды.

Почему гибрид: BM25 ловит точные термины («трёх рабочих дней», номера правил,
аббревиатуры), вектор — перефразировки («сколько времени на согласование»).
Слияние — reciprocal rank fusion: оно не требует соизмерять несопоставимые
шкалы (BM25 не нормирован, косинус лежит в [-1, 1]).
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .corpus import Corpus
from .models import Chunk

EMBED_URL = os.environ.get("LEX_EMBED_URL", "http://127.0.0.1:8081/v1/embeddings")
EMBED_MODEL = os.environ.get("LEX_EMBED_MODEL", "bge-m3")
EMBED_DIM = int(os.environ.get("LEX_EMBED_DIM", "1024"))

# BM25: k1=1.5, b=0.75 — стандарт для коротких текстов; словарь тут маленький,
# подбирать параметры на 27 документах смысла нет.
BM25_K1 = 1.5
BM25_B = 0.75

_TOKEN_RE = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ]+(?:[-/][0-9a-zA-Zа-яёА-ЯЁ]+)*")
_STOP = frozenset(
    ["и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то", "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще", "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли", "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь", "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей", "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя", "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже", "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому", "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти", "мой", "тем", "чтобы", "нее", "сейчас", "были", "куда", "зачем", "всех", "никогда", "можно", "при", "об", "хоть", "после", "над", "больше", "тот", "через", "эти", "нас", "про", "всего", "них", "какая", "много", "разве", "три", "эту", "моя", "впрочем", "хорошо", "свою", "этой", "перед", "иногда", "лучше", "чуть", "том", "нельзя", "такой", "им", "более", "всегда", "конечно", "всю", "между"]
)


def tokenize(text: str) -> list[str]:
    """Русская токенизация под BM25: нижний регистр + отсечение стоп-слов.

    Стемминга нет намеренно: имена правил («SR-01»), даты и номера приказов
    важны как есть, а «срок/срока/сроки» для 27 документов уже сойдутся по
    префиксу через усечение до 6 символов.
    """
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        if raw in _STOP or len(raw) < 2:
            continue
        out.append(raw)
        # лёгкое усечение: «согласования»/«согласование» -> общий префикс
        if len(raw) > 6 and raw.isalpha():
            out.append(raw[:6])
    return out


@dataclass
class Hit:
    chunk: Chunk
    score: float
    bm25: float = 0.0
    cosine: float = 0.0

    @property
    def doc_id(self) -> str:
        return self.chunk.doc_id


class EmbeddingError(RuntimeError):
    pass


def embed_texts(texts: list[str], *, timeout: float = 600.0, batch: int = 8) -> list[list[float]]:
    """Векторы для списка текстов; порядок результата = порядок входа.

    Батч 8 и таймаут 600 с, а не 32/180: замер на реальном корпусе заказчика
    дал ~14 с на чанк при 2 потоках CPU, и пачка из 32 длинных чанков не
    укладывалась в 180 с — индексация падала с таймаутом на ровном месте.
    Один запрос на 8 текстов — это ~2 минуты, с запасом.
    """
    if not texts:
        return []
    out: list[list[float]] = []
    for start in range(0, len(texts), batch):
        out.extend(_embed_batch(texts[start : start + batch], timeout))
    return out


def _embed_batch(texts: list[str], timeout: float) -> list[list[float]]:
    body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        EMBED_URL, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.URLError as exc:
        raise EmbeddingError(f"эмбеддер недоступен ({EMBED_URL}): {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EmbeddingError(f"эмбеддер вернул не JSON: {exc}") from exc
    except (TimeoutError, OSError) as exc:
        # Таймаут чтения приходит как socket.timeout (TimeoutError), а не как
        # URLError. Без этой ветки зависший эмбеддер валил весь поиск вместо
        # того, чтобы честно деградировать до BM25 (см. search()).
        raise EmbeddingError(f"эмбеддер не ответил за {timeout:.0f} с: {exc}") from exc

    data = payload.get("data")
    if not isinstance(data, list) or len(data) != len(texts):
        got = len(data) if isinstance(data, list) else "?"
        raise EmbeddingError(f"эмбеддер вернул {got} векторов на {len(texts)} текстов")
    if all(isinstance(x, dict) and "index" in x for x in data):
        data = sorted(data, key=lambda x: x["index"])
    return [[float(v) for v in item["embedding"]] for item in data]


def embeddings_reachable(timeout: float = 5.0) -> bool:
    """Живая проверка для /api/health — не константа."""
    try:
        vec = embed_texts(["проверка"], timeout=timeout)
        return bool(vec) and len(vec[0]) > 0
    except Exception:
        return False


class SearchIndex:
    """BM25 + векторный индекс поверх чанков корпуса."""

    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus
        self.chunks: list[Chunk] = list(corpus.chunks)
        self._tokenized: list[list[str]] = [tokenize(c.text) for c in self.chunks]
        self._lengths = np.array([len(t) or 1 for t in self._tokenized], dtype=np.float64)
        self._avgdl = float(self._lengths.mean()) if len(self.chunks) else 1.0
        self._tf: list[Counter[str]] = [Counter(t) for t in self._tokenized]
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = max(1, len(self.chunks))
        self._idf: dict[str, float] = {
            term: math.log(1.0 + (n - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()
        }
        self._vecs: np.ndarray | None = None
        self._cache_path = corpus.root / ".lex-index.npz"

    # ---------------------------------------------------------------- vector

    def build_vectors(self, *, force: bool = False) -> int:
        """Считает эмбеддинги чанков. Кэш в ``.lex-index.npz`` рядом с корпусом."""
        if self._vecs is not None and not force:
            return len(self._vecs)
        if not force and self._cache_path.exists():
            loaded = self._load_cache()
            if loaded is not None:
                self._vecs = loaded
                return len(loaded)

        vecs = embed_texts([c.text for c in self.chunks])
        arr = np.asarray(vecs, dtype=np.float64)
        if arr.ndim != 2 or arr.shape[0] != len(self.chunks):
            raise EmbeddingError(f"форма матрицы {arr.shape} не совпала с {len(self.chunks)} чанками")
        arr /= np.linalg.norm(arr, axis=1, keepdims=True) + 1e-12
        self._vecs = arr
        self._save_cache(arr)
        return len(arr)

    def _save_cache(self, arr: np.ndarray) -> None:
        try:
            # Имя файла-кэша не должно попасть в git: корпус маленький, индекс
            # дешевле пересчитать, чем коммитить бинарь.
            np.savez_compressed(
                self._cache_path,
                vecs=arr.astype(np.float32),
                chunk_ids=np.array([c.chunk_id for c in self.chunks], dtype=object),
            )
        except Exception:
            pass  # кэш — оптимизация, не контракт

    def _load_cache(self) -> np.ndarray | None:
        try:
            with np.load(self._cache_path, allow_pickle=True) as z:
                ids = [str(x) for x in z["chunk_ids"]]
                if ids != [c.chunk_id for c in self.chunks]:
                    return None
                return z["vecs"].astype(np.float64)
        except Exception:
            return None

    # ----------------------------------------------------------------- bm25

    def bm25(self, query: str, top: int = 20) -> list[Hit]:
        terms = tokenize(query)
        if not terms or not self.chunks:
            return []
        scores = np.zeros(len(self.chunks), dtype=np.float64)
        for term in terms:
            idf = self._idf.get(term)
            if idf is None:
                continue
            for i, tf in enumerate(self._tf):
                freq = tf.get(term)
                if not freq:
                    continue
                norm = freq * (BM25_K1 + 1.0) / (
                    freq + BM25_K1 * (1.0 - BM25_B + BM25_B * self._lengths[i] / self._avgdl)
                )
                scores[i] += idf * norm
        order = np.argsort(-scores)[:top]
        return [Hit(chunk=self.chunks[i], score=float(scores[i]), bm25=float(scores[i]))
                for i in order if scores[i] > 0]

    # --------------------------------------------------------------- vector

    def vector(self, query: str, top: int = 20) -> list[Hit]:
        if self._vecs is None:
            self.build_vectors()
        if self._vecs is None or len(self._vecs) == 0:
            # Эмбеддер лёг на этапе индексации: векторного канала нет, но BM25
            # работает. Пустой список — честный ответ «вектор не дал ничего»,
            # в отличие от исключения, которое убьёт весь поиск.
            raise EmbeddingError("векторный канал недоступен: векторы не построены")
        qv = np.asarray(embed_texts([query])[0], dtype=np.float64)
        qv /= np.linalg.norm(qv) + 1e-12
        sims = self._vecs @ qv
        order = np.argsort(-sims)[:top]
        return [Hit(chunk=self.chunks[i], score=float(sims[i]), cosine=float(sims[i])) for i in order]

    # ---------------------------------------------------------------- hybrid

    def search(self, query: str, *, top: int = 5, candidates: int = 20, k_rrf: int = 60) -> list[Hit]:
        """Гибридный поиск: RRF по BM25 и вектору.

        RRF выбран потому, что шкалы несопоставимы (BM25 не нормирован, косинус
        в [-1,1]); складывать их напрямую — произвол, который на чужом корпусе
        развалится. RRF смотрит только на ранги.
        """
        bm = self.bm25(query, top=candidates)
        try:
            ve = self.vector(query, top=candidates)
        except EmbeddingError:
            # Без эмбеддера каскад не должен падать целиком: BM25 — рабочая
            # база, а недоступность вектора видна в /api/health.
            ve = []

        fused: dict[str, Hit] = {}
        for rank, hit in enumerate(bm):
            cur = fused.setdefault(hit.chunk.chunk_id, Hit(chunk=hit.chunk, score=0.0))
            cur.bm25 = hit.bm25
            cur.score += 1.0 / (k_rrf + rank + 1)
        for rank, hit in enumerate(ve):
            cur = fused.setdefault(hit.chunk.chunk_id, Hit(chunk=hit.chunk, score=0.0))
            cur.cosine = hit.cosine
            cur.score += 1.0 / (k_rrf + rank + 1)

        ranked = sorted(fused.values(), key=lambda h: h.score, reverse=True)
        return ranked[:top]

    def search_many(self, queries: Iterable[str], *, top_per_query: int, **kw) -> list[Hit]:
        """Поиск по нескольким подвопросам: результаты сливаются по chunk_id."""
        merged: dict[str, Hit] = {}
        for q in queries:
            for hit in self.search(q, top=top_per_query, **kw):
                cur = merged.get(hit.chunk.chunk_id)
                if cur is None:
                    merged[hit.chunk.chunk_id] = hit
                else:
                    cur.score += hit.score
                    cur.bm25 = max(cur.bm25, hit.bm25)
                    cur.cosine = max(cur.cosine, hit.cosine)
        return sorted(merged.values(), key=lambda h: h.score, reverse=True)


_CACHE: dict[str, SearchIndex] = {}


def get_index(corpus_root: str | Path) -> SearchIndex:
    """Индекс по корню корпуса, с кэшем в памяти процесса."""
    key = str(Path(corpus_root).resolve())
    idx = _CACHE.get(key)
    if idx is None:
        from .corpus import load_corpus

        idx = SearchIndex(load_corpus(corpus_root))
        _CACHE[key] = idx
    return idx


def reset_index(corpus_root: str | Path | None = None) -> None:
    """Сброс кэша — нужен POST /api/ingest (перечитать corpus и индекс)."""
    if corpus_root is None:
        _CACHE.clear()
    else:
        _CACHE.pop(str(Path(corpus_root).resolve()), None)
