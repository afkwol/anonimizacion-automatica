"""Capa 2: NER en español usando spaCy.

Esta capa detecta personas (PER), lugares (LOC) y organizaciones (ORG)
que las regex deterministas no pueden capturar. Es la fuente principal
de **candidatos a clasificar por el LLM** en PR 7: cada PER detectado
acá se convierte en una "ficha" que el clasificador examina para
asignarle un rol (parte/testigo/juez/autor doctrina/etc.).

**Decisión de diseño**: spaCy es una dependencia opcional (`pip install
.[ner]` + `python -m spacy download es_core_news_md`). Si no está
instalada, `detect_entities` lanza una excepción explicativa. Esto
permite que el resto del pipeline (regex, segmentación) funcione sin
spaCy para tests rápidos o para entornos donde sólo se quieren
identificadores.

**Modelo**: `es_core_news_md` (~40MB) — buen balance precision/recall
para personas en español. La versión `_lg` (~568MB) mejora ~2 puntos en
F1 pero tarda mucho más en cargar; la versión `_sm` no tiene vectores y
da peores resultados en nombres compuestos. `_md` es el sweet spot.

**Modelo singleton**: cargar spaCy es caro (1-2 segundos). Cacheamos la
instancia a nivel de módulo. El cache puede invalidarse explícitamente
con `_reset_model()` (útil para tests).
"""
from __future__ import annotations

from typing import List, Optional

from .span import Span

# Tipos canónicos que mapeamos desde las labels de spaCy.
# spaCy en español usa: PER, LOC, ORG, MISC.
_SPACY_TO_CANONICAL = {
    "PER": "PER",
    "PERSON": "PER",  # por si el modelo es entrenado en esquema EN
    "LOC": "LOC",
    "GPE": "LOC",
    "ORG": "ORG",
    # MISC se descarta: demasiado ruidoso para anonimización.
}

DEFAULT_MODEL = "es_core_news_md"

_NLP_CACHE: dict = {}


def is_available(model: str = DEFAULT_MODEL) -> bool:
    """¿El modelo está instalado y se puede cargar?

    Útil para que los tests se skipeen graciosamente si spaCy o el modelo
    no están presentes en el entorno.
    """
    try:
        _load_model(model)
        return True
    except Exception:
        return False


def _load_model(model: str) -> object:
    """Carga el modelo spaCy con caching. Lanza ImportError/OSError si falla."""
    if model in _NLP_CACHE:
        return _NLP_CACHE[model]
    try:
        import spacy  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "spaCy no está instalado. Instalar con: pip install spacy"
        ) from exc
    try:
        nlp = spacy.load(model)
    except OSError as exc:
        raise OSError(
            f"Modelo spaCy '{model}' no encontrado. Instalar con: "
            f"python -m spacy download {model}"
        ) from exc
    _NLP_CACHE[model] = nlp
    return nlp


def _reset_model() -> None:
    """Limpia el cache. Sólo para tests."""
    _NLP_CACHE.clear()


def detect_entities(
    text: str,
    *,
    model: str = DEFAULT_MODEL,
    types: Optional[tuple[str, ...]] = ("PER",),
) -> List[Span]:
    """Detecta entidades nombradas en `text` y las devuelve como `Span`s.

    Args:
        text: texto a analizar (puede ser el `full_text` completo o un
            chunk; spaCy maneja documentos largos sin problema).
        model: nombre del modelo spaCy. Default `es_core_news_md`.
        types: tipos canónicos a devolver. Default `("PER",)` porque para
            anonimización judicial los nombres de personas son lo único
            crítico que NER aporta sobre regex; LOC/ORG suelen generar
            ruido (juzgados, ciudades, organismos públicos que NO se
            anonimizan).

    Returns:
        Lista de `Span` con `source="ner"`, `confidence` derivado de la
        probabilidad del modelo cuando está disponible (en spaCy
        estándar no lo está, así que usamos 0.7 como prior bajo;
        el LLM clasificador puede sobreescribir).

    Raises:
        ImportError/OSError si spaCy o el modelo no están disponibles.
    """
    if not text:
        return []

    nlp = _load_model(model)
    # Desactivamos componentes que no necesitamos para acelerar.
    # NER es lo único que importa.
    with nlp.select_pipes(enable=["tok2vec", "morphologizer", "ner"]):
        doc = nlp(text)

    spans: List[Span] = []
    type_filter = set(types) if types is not None else None

    for ent in doc.ents:
        canonical = _SPACY_TO_CANONICAL.get(ent.label_)
        if canonical is None:
            continue
        if type_filter is not None and canonical not in type_filter:
            continue
        # Filtrar entidades de 1 sólo carácter o puro whitespace.
        ent_text = ent.text.strip()
        if len(ent_text) < 2:
            continue
        spans.append(
            Span(
                start=ent.start_char,
                end=ent.end_char,
                text=text[ent.start_char:ent.end_char],
                type=canonical,
                source="ner",
                confidence=0.7,
                metadata={"spacy_label": ent.label_},
            )
        )
    return spans
