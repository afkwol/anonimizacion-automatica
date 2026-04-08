"""Modelo común de Span para todas las capas de detección.

Todos los detectores (regex, NER, estructura, LLM) producen `Span`s con
el mismo shape. Esto permite que `coreference` y `replace` trabajen
sobre una colección homogénea sin saber quién detectó qué.

`start`/`end` son offsets absolutos dentro del texto fuente (el
`full_text` del `DocxDocument`). Son inclusive/exclusive respectivamente
como los slices de Python, de modo que `text[span.start:span.end] == span.text`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class Span:
    """Una entidad detectada con metadatos de origen.

    Attributes:
        start: offset inicial absoluto (inclusive).
        end: offset final absoluto (exclusive).
        text: texto exacto del span (debe coincidir con source[start:end]).
        type: tipo de la entidad. Los detectores deterministas usan
            strings canónicos ("DNI", "CUIT", "CBU", "EMAIL", "TELEFONO",
            "PATENTE", "PASAPORTE"). NER usa "PER", "LOC", "ORG".
            Luego el clasificador LLM asigna un rol del enum de taxonomía.
        source: qué capa detectó este span ("regex", "ner", "structure",
            "llm"). Importante para la política de resolución de
            solapamientos: regex > structure > ner > llm.
        confidence: [0.0, 1.0]. Los regex con checksum validado usan 1.0.
        metadata: campo libre para info extra (ej: rol asignado por LLM,
            valor normalizado, cluster_id de coreferencia).
    """

    start: int
    end: int
    text: str
    type: str
    source: str
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def overlaps(self, other: "Span") -> bool:
        """Dos spans se solapan si sus rangos [start, end) se intersectan."""
        return not (self.end <= other.start or self.start >= other.end)

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError(f"Span inválido: start={self.start}, end={self.end}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence fuera de rango: {self.confidence}")


# Prioridad para resolver solapamientos entre capas.
# Mayor = más prioridad. El regex determinista con checksum siempre gana.
SOURCE_PRIORITY = {
    "regex": 100,
    "structure": 80,
    "ner": 60,
    "llm": 40,
}


def resolve_overlaps(spans: list[Span]) -> list[Span]:
    """Dada una lista de spans (posiblemente solapados), devuelve una
    lista sin solapamientos eligiendo por prioridad de source y luego
    por longitud (más largo gana).

    Esto implementa la política del plan: **regex siempre gana** sobre
    NER o LLM. Si dos spans tienen la misma prioridad, gana el más
    específico (span más largo, que suele ser más informativo).
    """
    if not spans:
        return []

    # Orden: por (start, -prioridad, -longitud) para procesar izquierda a
    # derecha eligiendo el mejor candidato cuando hay solapamiento.
    def _key(s: Span) -> tuple:
        return (s.start, -SOURCE_PRIORITY.get(s.source, 0), -(s.end - s.start))

    ordered = sorted(spans, key=_key)
    result: list[Span] = []
    for span in ordered:
        if result and span.overlaps(result[-1]):
            prev = result[-1]
            prev_pri = SOURCE_PRIORITY.get(prev.source, 0)
            cur_pri = SOURCE_PRIORITY.get(span.source, 0)
            if cur_pri > prev_pri:
                result[-1] = span
            elif cur_pri == prev_pri and (span.end - span.start) > (prev.end - prev.start):
                result[-1] = span
            # En otro caso, descartamos el nuevo.
        else:
            result.append(span)
    return result
