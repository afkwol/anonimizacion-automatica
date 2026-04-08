"""Segmentación semántica de oraciones en español.

Reemplazo del tokenizer por espacios del legacy monolith. Usa `pysbd`
porque maneja correctamente abreviaturas legales comunes en español
(art., inc., fs., Dr., Dra., etc.) y no corta a mitad de una entidad
nominal.

Contrato: `segment_sentences(text)` devuelve una lista de `Sentence` con
offsets exactos dentro del texto original (inclusive para `start`,
exclusive para `end`), de manera que `text[s.start:s.end] == s.text` se
cumple siempre. Esto es crítico para que los detectores de PR 4/5/6
puedan mapear spans de regex/NER de vuelta al texto global.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import pysbd


@dataclass(frozen=True)
class Sentence:
    """Una oración con su posición en el texto original.

    Attributes:
        text: contenido exacto (incluye puntuación final; NO incluye el
            whitespace que sigue).
        start: offset inicial en el texto fuente (inclusive).
        end: offset final en el texto fuente (exclusive).
    """

    text: str
    start: int
    end: int


_SEGMENTER_CACHE: dict[str, pysbd.Segmenter] = {}


def _get_segmenter(language: str = "es") -> pysbd.Segmenter:
    """pysbd.Segmenter tiene costo de construcción; cacheamos por idioma."""
    if language not in _SEGMENTER_CACHE:
        _SEGMENTER_CACHE[language] = pysbd.Segmenter(language=language, clean=False, char_span=True)
    return _SEGMENTER_CACHE[language]


def segment_sentences(text: str, language: str = "es") -> List[Sentence]:
    """Segmenta `text` en oraciones preservando offsets absolutos.

    Usa `char_span=True` y `clean=False` para que pysbd devuelva objetos
    con `.start` y `.end`, garantizando round-trip exacto. Las oraciones
    vacías o que son puro whitespace se descartan (no aportan al pipeline
    de anonimización y ensuciarían los chunks).

    Para documentos largos con saltos `\\n\\n`, pysbd ya trata las líneas
    en blanco como límite duro, así que no necesitamos pre-partir por
    párrafos.
    """
    if not text:
        return []

    seg = _get_segmenter(language)
    raw = seg.segment(text)

    sentences: List[Sentence] = []
    for item in raw:
        # pysbd devuelve TextSpan con .sent, .start, .end cuando char_span=True
        start = int(item.start)
        end = int(item.end)
        sentence_text = text[start:end]
        if not sentence_text.strip():
            continue
        sentences.append(Sentence(text=sentence_text, start=start, end=end))

    return sentences
