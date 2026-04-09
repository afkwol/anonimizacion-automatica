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

**Modelo**: cadena de preferencia `es_core_news_lg` → `es_core_news_md`.
NOTA: NO usamos `es_dep_news_trf` porque ese modelo no incluye componente
NER (solo morphologizer/parser/attribute_ruler/lemmatizer). Para español
spaCy no tiene un modelo transformer-based con NER oficial; el mejor
NER spaCy-nativo es `_lg` (568MB), que mejora visiblemente sobre `_md`
(40MB) en nombres compuestos y mayúsculas. Para algo mejor habría que
salir de spaCy (Flair `aymurai/flair-ner-spanish-judicial`, HF, etc.).

**Filtros post-NER**: aplicamos dos limpiezas que reducen ~80% del ruido:

1. **Stoplist** de sustantivos comunes que el NER en español frecuentemente
   etiqueta como PER por aparecer capitalizados (Juez, Jueces, Cámara,
   Sr, Dr, etc.). Filtro determinista por texto normalizado.
2. **Recorte de separadores judiciales** pegados al final del nombre
   ("Patricia Lilian c" → "Patricia Lilian"). Esto pasa porque el NER
   absorbe el `c` del separador `c/` cuando no hay espacio limpio.

**Modelo singleton**: cargar spaCy es caro (1-2 segundos). Cacheamos la
instancia a nivel de módulo. El cache puede invalidarse explícitamente
con `_reset_model()` (útil para tests).
"""
from __future__ import annotations

import re
import unicodedata
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

DEFAULT_MODEL = "es_core_news_lg"
# Cadena de fallback: si el preferido no está, intenta los siguientes.
_MODEL_FALLBACK_CHAIN = (
    "es_core_news_lg",
    "es_core_news_md",
)

_NLP_CACHE: dict = {}


# Stoplist: tokens que el NER en español suele etiquetar como PER por
# aparecer capitalizados pero NO son personas. Comparamos con el texto
# del span normalizado a minúsculas y sin signos.
_NER_PER_STOPLIST = frozenset(
    {
        # títulos / cargos sueltos
        "sr", "sra", "srta", "dr", "dra", "lic", "ing", "prof",
        "sres", "sras", "señor", "señora", "don", "doña",
        # roles judiciales
        "juez", "jueza", "jueces", "juezas",
        "fiscal", "fiscales", "defensor", "defensora",
        "secretario", "secretaria", "secretarios", "secretarias",
        "actuario", "actuaria",
        "ministro", "ministra", "ministros", "ministras",
        "presidente", "presidenta", "vocal", "vocales",
        "camarista", "camaristas",
        # entidades estructurales
        "cámara", "camara", "cam", "sala", "juzgado", "tribunal",
        "corte", "secretaría", "secretaria",
        # ordinales / latín / palabras sueltas frecuentes
        "primero", "segundo", "tercero", "cuarto", "quinto",
        "sexto", "séptimo", "septimo", "octavo", "noveno", "décimo", "decimo",
        "ordinario", "ordinaria", "extraordinario",
        "considerando", "vistos", "autos", "resulta", "resultando",
        "confluye", "brevitatis", "causa", "fojas", "foja", "res",
        "ll", "ja", "ed", "lll",
    }
)

# Caracteres / sufijos espurios que el NER absorbe del separador judicial
# `c/`, `s/`, `e/`, etc. cuando lo pega al final del nombre.
_TRAILING_JUNK = re.compile(
    r"\s+(?:[csyeo]/?|c\.|s\.|y\s+otros?|y\s+otra)\s*$",
    re.IGNORECASE,
)


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
    """Carga el modelo spaCy con caching y cadena de fallback.

    Si `model` es uno de los modelos de la cadena preferida y falla,
    intentamos los siguientes (más livianos). Si `model` no está en la
    cadena, lo intentamos solo y lanzamos OSError si no carga.
    """
    if model in _NLP_CACHE:
        return _NLP_CACHE[model]
    try:
        import spacy  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "spaCy no está instalado. Instalar con: pip install spacy"
        ) from exc

    # Determinar la cadena a probar.
    if model in _MODEL_FALLBACK_CHAIN:
        idx = _MODEL_FALLBACK_CHAIN.index(model)
        chain = _MODEL_FALLBACK_CHAIN[idx:]
    else:
        chain = (model,)

    last_err: Optional[Exception] = None
    for candidate in chain:
        if candidate in _NLP_CACHE:
            _NLP_CACHE[model] = _NLP_CACHE[candidate]
            return _NLP_CACHE[candidate]
        try:
            nlp = spacy.load(candidate)
            _NLP_CACHE[candidate] = nlp
            _NLP_CACHE[model] = nlp
            return nlp
        except OSError as exc:
            last_err = exc
            continue

    raise OSError(
        f"Ningún modelo spaCy disponible en la cadena {chain}. "
        f"Instalar con: python -m spacy download {chain[0]} "
        f"(o uno de los fallbacks). Último error: {last_err}"
    )


def _normalize_for_stoplist(text: str) -> str:
    """Lowercase + sin acentos + sin puntuación, para matchear stoplist."""
    s = unicodedata.normalize("NFD", text)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w\s]", "", s)
    return s.lower().strip()


def _strip_trailing_junk(text: str) -> str:
    """Quita separadores judiciales pegados al final del nombre."""
    prev = None
    cur = text
    while prev != cur:
        prev = cur
        cur = _TRAILING_JUNK.sub("", cur).rstrip(" ,.;:")
    return cur


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

        ent_text = ent.text.strip()
        if len(ent_text) < 2:
            continue

        start = ent.start_char
        end = ent.end_char
        raw = text[start:end]

        # Recorte de separadores judiciales pegados al final del nombre.
        # Solo aplica a PER (los nombres de personas son los que sufren
        # este problema con `c/`, `s/`, etc.).
        if canonical == "PER":
            cleaned = _strip_trailing_junk(raw)
            if not cleaned:
                continue
            # Recortar `end` para que coincida con el texto limpio.
            if cleaned != raw:
                # Buscar dónde termina el texto limpio dentro del raw.
                new_len = len(cleaned)
                # raw puede tener whitespace al inicio que ent.text no
                # necesariamente refleja; preferimos buscar el `cleaned`
                # como prefijo del raw después de strip izquierdo.
                lstripped = raw.lstrip()
                lpad = len(raw) - len(lstripped)
                if lstripped.startswith(cleaned):
                    start = ent.start_char + lpad
                    end = start + new_len
                    raw = text[start:end]
                else:
                    # Fallback conservador: descartar el span antes que
                    # corromper offsets.
                    continue

            # Stoplist: descartar sustantivos comunes capitalizados.
            normalized = _normalize_for_stoplist(raw)
            if normalized in _NER_PER_STOPLIST:
                continue
            # También descartar si es UNA sola palabra y es stoplist
            # (cubre casos como "Juez" sin contexto).
            tokens = normalized.split()
            if len(tokens) == 1 and tokens[0] in _NER_PER_STOPLIST:
                continue

        if end - start < 2:
            continue

        spans.append(
            Span(
                start=start,
                end=end,
                text=text[start:end],
                type=canonical,
                source="ner",
                confidence=0.7,
                metadata={"spacy_label": ent.label_},
            )
        )
    return spans
