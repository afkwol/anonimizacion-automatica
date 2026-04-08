"""Detector dedicado de doctrina y jurisprudencia (PR 13).

**Motivación**: las citas son la fuente más común de falsos positivos
en anonimización legal. Si el sistema "anonimiza" `Alterini` o
`Bidart Campos`, el documento queda mutilado: el lector pierde la
referencia legal. Peor todavía: el detector borra el aporte intelectual
de un autor académico que **no es PII**.

**Estrategia**: a diferencia de `structure.py` (que sólo emite zonas),
este módulo emite `Span`s concretos para los nombres dentro de las
citas, con `source="structure"`. Eso los hace ganar la disputa de
solapamiento contra spans NER (NER vería "Alterini" como PER y querría
clasificarlo). El span lleva `metadata["preserve"]=True` y tipo
`AUTOR_DOCTRINA`/`AUTOR_JURISPRUDENCIA`, que el orquestador interpreta
como "no anonimizar".

**Patrones cubiertos**:
- Fallos CSJN: `Fallos: 333:1234`, `CSJN, "X c/ Y", 12/3/20`
- Cámaras: `CNCiv. Sala A`, `CNCom.`, etc.
- Doctrina por trigger: `ver doctrina de NOMBRE`, `conf. NOMBRE`,
  `cfr. NOMBRE`, `según enseña NOMBRE`, `en palabras de NOMBRE`
- Doctrina formal: `AUTOR, Obra, Editorial, año, p. NN`
"""
from __future__ import annotations

import re
from typing import List

from .span import Span


# Mismos triggers que structure.py pero acá los reusamos para localizar
# el NOMBRE específico que sigue al trigger.
_DOCTRINA_TRIGGERS = re.compile(
    r"(?i)\b("
    r"ver\s+doctrina\s+de"
    r"|conf(?:orme)?\."
    r"|cfr?\."
    r"|seg[uú]n\s+ense[ñn]a"
    r"|en\s+palabras\s+de"
    r")\s+",
)

# Después del trigger, capturamos un nombre propio: 1-4 tokens iniciados
# en mayúscula (incluyendo `de`/`del`/`la` minúsculas como conectores).
# Evitamos capturar oraciones enteras: cortamos en coma, punto, paréntesis.
_NAME_AFTER_TRIGGER = re.compile(
    r"((?:[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+\.?\s*)"  # primer token capitalizado
    r"(?:(?:de\s+|del\s+|la\s+|y\s+)?[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+\.?\s*){0,3})",
)

# Citas de jurisprudencia entre comillas: `"NOMBRE c/ NOMBRE"` o
# `"NOMBRE s/ NOMBRE"`. El nombre completo de la causa se preserva.
_CASE_NAME = re.compile(
    r'["“]([^"”]{4,120}?\s+(?:c/|s/|vs\.?)\s+[^"”]{2,120}?)["”]',
)

# Markers de fallos puros (sin nombre dentro), generan span sobre el marcador
# para que la zona quede registrada en el audit pero no anonimizan nada.
_FALLO_MARKERS = re.compile(
    r"(?i)\b(?:Fallos:\s*\d+:\d+|CSJN[,\s])",
)

# Doctrina formal: APELLIDO en mayúsculas seguido de coma y año (1900-2099).
_FORMAL_DOCTRINA = re.compile(
    r"\b([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑa-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){0,3})"
    r"(?:,\s*[^,]{0,40}){0,3},\s*(?:19|20)\d{2}\b",
)


def _make_span(start: int, end: int, text: str, role: str) -> Span:
    return Span(
        start=start,
        end=end,
        text=text,
        type=role,
        source="structure",
        confidence=0.95,
        metadata={"preserve": True, "default_role": role},
    )


def detect_doctrina_authors(text: str) -> List[Span]:
    """Spans de autores de doctrina citados.

    Marcados como `preserve=True` con `default_role=AUTOR_DOCTRINA`.
    """
    spans: List[Span] = []

    # 1. Triggers explícitos: capturar el NOMBRE que sigue.
    for trig in _DOCTRINA_TRIGGERS.finditer(text):
        after = text[trig.end():trig.end() + 80]
        m = _NAME_AFTER_TRIGGER.match(after)
        if not m:
            continue
        raw = m.group(1).rstrip()
        # Cortar en coma/punto si quedaron incluidos
        for sep in (",", ".", ";", "(", ")"):
            idx = raw.find(sep)
            if idx > 0:
                raw = raw[:idx]
        raw = raw.rstrip()
        if not raw or len(raw) < 3:
            continue
        start = trig.end()
        end = start + len(raw)
        spans.append(_make_span(start, end, text[start:end], "AUTOR_DOCTRINA"))

    # 2. Doctrina formal: APELLIDO ..., AÑO
    for m in _FORMAL_DOCTRINA.finditer(text):
        name = m.group(1)
        start = m.start(1)
        end = start + len(name)
        spans.append(_make_span(start, end, name, "AUTOR_DOCTRINA"))

    return spans


def detect_jurisprudencia_authors(text: str) -> List[Span]:
    """Spans de causas y firmas en jurisprudencia citada.

    Captura nombres de causa entre comillas (`"X c/ Y"`). Marcados
    como `preserve=True` con `default_role=AUTOR_JURISPRUDENCIA`.
    """
    spans: List[Span] = []
    for m in _CASE_NAME.finditer(text):
        case = m.group(1)
        start = m.start(1)
        end = start + len(case)
        spans.append(_make_span(start, end, case, "AUTOR_JURISPRUDENCIA"))
    return spans


def detect_citations(text: str) -> List[Span]:
    """Devuelve todos los spans de doctrina y jurisprudencia.

    Estos spans tienen prioridad `structure` (80) sobre `ner` (60),
    así que en el `resolve_overlaps` ganan a NER. Como llevan
    `metadata.preserve=True`, el orquestador los excluye de la
    anonimización aun si el LLM dice lo contrario.
    """
    return detect_doctrina_authors(text) + detect_jurisprudencia_authors(text)
