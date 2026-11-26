"""LEX — ядро RAG: ingest → retrieve → resolve → compose.

Пакет не знает про HTTP: события отдаются данными (см. ``models.StepEvent``),
превращение в SSE — дело ``api``.
"""

from .models import STEP_ORDER, STEP_TITLES, CorpusStats

__all__ = ["STEP_ORDER", "STEP_TITLES", "CorpusStats"]
