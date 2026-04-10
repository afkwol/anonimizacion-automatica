"""Detector estructural de partes en la carátula (PR 16, fix #1).

**Problema que resuelve**: el NER `es_core_news_md` no detecta nombres en
formato `APELLIDO, NOMBRE` cuando vienen en mayúsculas, que es exactamente
como aparecen las partes en las carátulas judiciales argentinas:

    DIAZ, GUILLERMO C/ BANCO PATAGONIA S.A. S/ PRO GRATUIDAD
    PEREZ, JUAN CARLOS C/ BANCO XYZ S/ COBRO EJECUTIVO

Esto causaba que la parte principal del expediente —el actor— ¡quedara
sin anonimizar! Es una falla crítica.

**Estrategia**:

1. Buscar la zona de carátula (primeros ~1500 caracteres o hasta el
   primer marcador "VISTOS:"/"RESULTANDO:"/etc.).
2. Dentro de esa zona, buscar el patrón `APELLIDO, NOMBRE c/ DEMANDADO
   s/ OBJETO` con regex tolerante a variantes (`c.`, `C/`, `contra`).
3. Para cada parte detectada (actor y demandado), buscar **todas las
   ocurrencias** del nombre en el documento entero y emitir un Span por
   cada una. Esto garantiza que cuando el actor se menciona 8 veces,
   las 8 menciones reciben el mismo placeholder vía coreferencia.
4. Los spans tienen `source="structure"` (prioridad 80) → ganan sobre
   NER (60) en `resolve_overlaps`. Se envían igual al LLM como fichas
   con `zone_hint=CARATULA` y `pista_rol=PARTE_ACTORA/PARTE_DEMANDADA`,
   así el clasificador confirma el rol y la coreference asigna placeholder.

**Falsos positivos**: si el regex matchea algo que no es una persona
(ej: "BANCO PATAGONIA" como demandado), el LLM lo verá como ficha con
`tipo_ner=None` y `pista_rol=PARTE_DEMANDADA`. Si es razón social, el
LLM debería igualmente clasificarlo como `RAZON_SOCIAL` o `PARTE_DEMANDADA`
y anonimizarse. No es un problema.
"""
from __future__ import annotations

import re
from typing import List

from .span import Span


# Marcadores que indican el FIN de la carátula. Reusamos los mismos que
# `structure._detect_caratula` para consistencia.
_CARATULA_END = re.compile(
    r"(?im)^\s*(?:Y\s+VISTOS?|VISTO|RESULTA|RESULTANDO|"
    r"I\s*[\.\-]\s*DEMANDA|I\s*[\.\-]\s*ANTECEDENTES|"
    r"AUTOS\s+Y\s+VISTOS|CONSIDERANDO)\s*[:.]",
)
_CARATULA_FALLBACK = 1500


# Patrón principal: `APELLIDO[S], NOMBRE[S] c/ DEMANDADO`.
# - Apellido(s) en MAYÚSCULAS, posiblemente compuesto (DE LA CRUZ, VAN GOGH).
# - Nombres en MAYÚSCULAS o Title Case.
# - Separador `c/`, `C/`, `c.`, `C.`, `contra`, `c./`.
# Lo extraemos en grupos para luego buscar las ocurrencias del actor y
# del demandado por separado.
_PARTY_VS = re.compile(
    r"""
    (?P<actor>
        [A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ\s'`´]{1,80}?   # apellidos en mayúsculas
        ,\s*
        [A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ\s'`´]{1,80}?   # nombres en mayúsculas
    )
    \s+
    (?:c/|C/|c\.|C\.|contra\s+)
    \s*
    (?P<demandado>
        [A-ZÁÉÍÓÚÑÜ][\wÁÉÍÓÚÑÜáéíóúñü\s\.&'-]{2,120}?
    )
    \s*
    (?:s/|S/|s\.|S\.|sobre\s+|\s-\s|\n|$)
    """,
    re.VERBOSE,
)


def _caratula_end_offset(text: str) -> int:
    if not text:
        return 0
    m = _CARATULA_END.search(text)
    if m:
        return m.start()
    return min(_CARATULA_FALLBACK, len(text))


def _clean_party_name(raw: str) -> str:
    """Normaliza un nombre de parte: trim, colapsa espacios, remueve
    sufijos como 'Y OTROS', 'Y OTRO' que no son parte del nombre."""
    s = re.sub(r"\s+", " ", raw).strip(" ,.;:")
    s = re.sub(r"\s+y\s+otros?$", "", s, flags=re.IGNORECASE).strip()
    return s


def detect_caratula_parties(text: str) -> List[Span]:
    """Detecta partes en la carátula y emite Spans en TODAS sus ocurrencias.

    Returns:
        Lista de Spans con `source="structure"`, `type="PER"`,
        `confidence=0.95`, y `metadata={"role_hint": "PARTE_ACTORA" |
        "PARTE_DEMANDADA", "from_caratula": True}`.
    """
    if not text:
        return []

    end = _caratula_end_offset(text)
    caratula = text[:end]
    spans: List[Span] = []

    for match in _PARTY_VS.finditer(caratula):
        actor_raw = match.group("actor")
        demandado_raw = match.group("demandado")

        for raw, role_hint in (
            (actor_raw, "PARTE_ACTORA"),
            (demandado_raw, "PARTE_DEMANDADA"),
        ):
            name = _clean_party_name(raw)
            if len(name) < 4:
                continue
            # Buscar TODAS las ocurrencias del nombre en el doc completo.
            # Usamos búsqueda case-sensitive porque las carátulas son
            # mayúsculas y queremos evitar matchear menciones casuales.
            # Si no encontramos ninguna en mayúscula, intentamos una
            # variante case-insensitive limitada a la carátula.
            occurrences = _find_all_occurrences(text, name)
            if not occurrences:
                # Fallback: case-insensitive solo dentro de la carátula.
                occurrences = _find_all_occurrences(
                    text[:end], name, case_insensitive=True
                )
            for start in occurrences:
                spans.append(
                    Span(
                        start=start,
                        end=start + len(name),
                        text=text[start : start + len(name)],
                        type="PER",
                        source="structure",
                        confidence=0.95,
                        metadata={
                            "role_hint": role_hint,
                            "from_caratula": True,
                        },
                    )
                )

    return spans


def _find_all_occurrences(
    text: str, needle: str, *, case_insensitive: bool = False
) -> List[int]:
    """Devuelve los offsets de todas las ocurrencias de `needle` en `text`."""
    if not needle:
        return []
    if case_insensitive:
        pat = re.compile(re.escape(needle), re.IGNORECASE)
        return [m.start() for m in pat.finditer(text)]
    out: List[int] = []
    i = 0
    while True:
        idx = text.find(needle, i)
        if idx == -1:
            break
        out.append(idx)
        i = idx + len(needle)
    return out
