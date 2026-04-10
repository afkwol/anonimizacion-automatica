"""Generador de variantes de nombre + buscador de ocurrencias.

Dado un nombre como aparece en la carátula (ej: "SONZINI, NAHUEL ANTONIO"),
genera todas las formas en que razonablemente puede aparecer en el documento
y busca cada una en el texto completo, devolviendo Spans con offsets exactos.

**Decisión de diseño**: NO buscamos apellido suelto nunca. "Sonzini" solo
no afecta la privacidad y podría matchear ambiguamente si hay un juez o
letrado con el mismo apellido.

Las formas que generamos son combinaciones de:
- APELLIDO, NOMBRE (original)
- Nombre Apellido (invertido)
- Nombre [Segundos nombres] Apellido
- Sr./Sra./Dr./Dra. + Apellido (con tratamiento)
- Todas en MAYÚSCULAS, Title Case y lowercase
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Set

from app.detect.span import Span


# Tratamientos comunes que pueden preceder al apellido.
_TRATAMIENTOS = ("Sr.", "Sra.", "Srta.", "Dr.", "Dra.", "Lic.", "Ing.", "Prof.")

# Sufijos societarios: (sin puntos, con puntos).
_SOCIETY_VARIANTS = (
    ("SA", "S.A."), ("SRL", "S.R.L."), ("SAS", "S.A.S."),
    ("SACI", "S.A.C.I."), ("SACIF", "S.A.C.I.F."),
    ("SC", "S.C."), ("SH", "S.H."), ("SE", "S.E."),
)


def _normalize(s: str) -> str:
    """Colapsa espacios y strip."""
    return re.sub(r"\s+", " ", s).strip()


def _remove_accents(s: str) -> str:
    """Quita acentos para búsqueda tolerante."""
    nfkd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn")


def _split_caratula_name(name: str) -> tuple[str, str]:
    """Separa 'APELLIDO, NOMBRE' → (apellido, nombre).

    Si no tiene coma, intenta 'NOMBRE APELLIDO' (última palabra = apellido).
    """
    if "," in name:
        parts = name.split(",", 1)
        return _normalize(parts[0]), _normalize(parts[1])
    # Sin coma: asumir última palabra = apellido.
    words = name.split()
    if len(words) >= 2:
        return words[-1], " ".join(words[:-1])
    return name, ""


def generate_variants(name: str) -> List[str]:
    """Genera las formas completas (nunca apellido solo) de un nombre.

    Args:
        name: nombre como lo devolvió el LLM o el detector de carátula.
            Puede ser "APELLIDO, NOMBRE" o "Nombre Apellido".

    Returns:
        Lista de variantes únicas, de más específica a menos.
    """
    name = _normalize(name)
    if not name:
        return []

    variants: list[str] = []
    seen: Set[str] = set()

    def _add(v: str) -> None:
        v = _normalize(v)
        if v and v not in seen and len(v) > 3:
            seen.add(v)
            variants.append(v)

    # 0. Persona jurídica: variantes de sufijo societario (SA ↔ S.A.).
    upper = name.upper().rstrip()
    for short, dotted in _SOCIETY_VARIANTS:
        if upper.endswith(short) or upper.endswith(dotted):
            if upper.endswith(dotted):
                base = name[:len(name) - len(dotted)].strip()
            else:
                base = name[:len(name) - len(short)].strip()
            for b in (base, base.upper(), base.title()):
                _add(f"{b} {short}")
                _add(f"{b} {dotted}")
            return variants

    # Persona física.
    # Cuando no hay coma, no sabemos si es "APELLIDO NOMBRE" o "NOMBRE APELLIDO".
    # Generamos variantes para ambas interpretaciones.
    interpretations: list[tuple[str, str]] = []

    if "," in name:
        apellido, nombres = _split_caratula_name(name)
        if not nombres:
            return [name]
        interpretations.append((apellido, nombres))
    else:
        words = name.split()
        if len(words) < 2:
            return [name]
        # Interpretación 1: primera palabra = apellido (DÍAZ MARÍA DEL CARMEN).
        interpretations.append((words[0], " ".join(words[1:])))
        # Interpretación 2: última palabra = apellido (MARÍA DEL CARMEN DÍAZ).
        if words[-1] != words[0]:
            interpretations.append((words[-1], " ".join(words[:-1])))

    # 1. Forma original.
    _add(name)

    for apellido, nombres in interpretations:
        # 2. APELLIDO, NOMBRE → varias casings.
        forma_caratula = f"{apellido}, {nombres}"
        _add(forma_caratula)
        _add(forma_caratula.upper())
        _add(forma_caratula.title())

        # 3. NOMBRE APELLIDO (invertido).
        forma_invertida = f"{nombres} {apellido}"
        _add(forma_invertida)
        _add(forma_invertida.upper())
        _add(forma_invertida.title())

        # 4. Con tratamientos.
        for t in _TRATAMIENTOS:
            _add(f"{t} {forma_invertida.title()}")
            _add(f"{t} {apellido.title()}, {nombres.title()}")

        # 5. Primer nombre + apellido (si hay más de un nombre).
        nombres_parts = nombres.split()
        if len(nombres_parts) > 1:
            primer_nombre = nombres_parts[0]
            forma_corta = f"{primer_nombre} {apellido}"
            _add(forma_corta)
            _add(forma_corta.upper())
            _add(forma_corta.title())

    # 6. Sin acentos (para textos OCR con mala codificación).
    for v in list(variants):
        sin_acentos = _remove_accents(v)
        if sin_acentos != v:
            _add(sin_acentos)

    return variants


def find_all_occurrences(text: str, variants: List[str]) -> List[Span]:
    """Busca todas las ocurrencias de las variantes en el texto.

    Devuelve Spans con `source="text_search"`, `type="PER"`,
    `confidence=0.99`. Sin duplicados por posición (si dos variantes
    matchean en el mismo offset, gana la más larga).

    Args:
        text: texto completo del documento.
        variants: lista de variantes a buscar (de generate_variants).

    Returns:
        Lista de Spans ordenados por offset, sin solapamientos.
    """
    if not text or not variants:
        return []

    # Buscar cada variante y recoger hits.
    hits: dict[tuple[int, int], Span] = {}

    for variant in variants:
        if not variant or len(variant) < 4:
            continue

        # Búsqueda literal.
        start = 0
        while True:
            idx = text.find(variant, start)
            if idx == -1:
                break
            end = idx + len(variant)
            key = (idx, end)
            if key not in hits or len(variant) > len(hits[key].text):
                hits[key] = Span(
                    start=idx,
                    end=end,
                    text=text[idx:end],
                    type="PER",
                    source="text_search",
                    confidence=0.99,
                )
            start = idx + 1

        # Búsqueda flexible: \s+ entre palabras + case-insensitive para
        # tolerar saltos de línea y diferencias de casing ("Del" vs "del").
        pattern = r"\s+".join(re.escape(w) for w in variant.split())
        for m in re.finditer(pattern, text, re.IGNORECASE):
            key = (m.start(), m.end())
            matched_text = m.group()
            if key not in hits or len(matched_text) > len(hits[key].text):
                hits[key] = Span(
                    start=m.start(),
                    end=m.end(),
                    text=matched_text,
                    type="PER",
                    source="text_search",
                    confidence=0.99,
                )

    # Ordenar y resolver solapamientos: si un span está contenido en
    # otro más largo, descartar el corto.
    sorted_spans = sorted(hits.values(), key=lambda s: (s.start, -(s.end - s.start)))
    result: List[Span] = []
    for span in sorted_spans:
        if result and span.start < result[-1].end:
            # Solapamiento: quedarse con el más largo (ya está en result).
            if (span.end - span.start) > (result[-1].end - result[-1].start):
                result[-1] = span
        else:
            result.append(span)

    return result
