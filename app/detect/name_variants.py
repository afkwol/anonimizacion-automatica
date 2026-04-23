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


# Clases regex para tolerar vocales acentuadas y ñ/n. Usadas con IGNORECASE,
# por eso alcanza con listar minúsculas: re de Python hace case-folding Unicode.
_CHAR_CLASS = {
    "a": "[aáàâä]", "e": "[eéèêë]", "i": "[iíìîï]",
    "o": "[oóòôö]", "u": "[uúùûü]",
    "n": "[nñ]",
}


def _regex_char_class(ch: str) -> str:
    """Devuelve la clase regex para un carácter (tolerante a acentos/ñ)."""
    base = unicodedata.normalize("NFD", ch)[0].lower()
    if base in _CHAR_CLASS:
        return _CHAR_CLASS[base]
    return re.escape(ch)


# Clase fuzzy: cualquier vocal (acentuada o no). Se usa en posición interna
# de palabras largas para tolerar typos comunes ("Esteban" ↔ "Estaban").
_ANY_VOWEL = "[aeiouáéíóúàèìòùâêîôûäëïöü]"


def _regex_char_class_in_word(ch: str, pos: int, word_len: int) -> str:
    """Como _regex_char_class, pero vocales internas de palabras largas
    son intercambiables (typo-tolerant: 'Esteban' ↔ 'Estaban').

    Solo activo en palabras de >=5 chars y posición ni inicial ni final.
    """
    base = unicodedata.normalize("NFD", ch)[0].lower()
    if base in "aeiou" and word_len >= 5 and 0 < pos < word_len - 1:
        return _ANY_VOWEL
    if base in _CHAR_CLASS:
        return _CHAR_CLASS[base]
    return re.escape(ch)


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
    # El sufijo debe estar separado del resto (no "JOSE" → "JO + S.E.").
    upper = name.upper().rstrip()
    for short, dotted in _SOCIETY_VARIANTS:
        for suf in (dotted, short):
            if not upper.endswith(suf):
                continue
            prev_idx = len(upper) - len(suf) - 1
            # Requiere separador (espacio, coma, punto) o que sea el principio del string.
            if prev_idx >= 0 and upper[prev_idx] not in " ,.;":
                continue
            base = name[:len(name) - len(suf)].rstrip(" ,.;")
            if not base:
                continue
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

        # 3. APELLIDO NOMBRE (sin coma).
        forma_sin_coma = f"{apellido} {nombres}"
        _add(forma_sin_coma)
        _add(forma_sin_coma.upper())
        _add(forma_sin_coma.title())

        # 4. NOMBRE APELLIDO (invertido).
        forma_invertida = f"{nombres} {apellido}"
        _add(forma_invertida)
        _add(forma_invertida.upper())
        _add(forma_invertida.title())

        # 5. Primer nombre + apellido (si hay más de un nombre).
        nombres_parts = nombres.split()
        if len(nombres_parts) > 1:
            primer_nombre = nombres_parts[0]
            # Nombre Apellido (corto).
            forma_corta = f"{primer_nombre} {apellido}"
            _add(forma_corta)
            _add(forma_corta.upper())
            _add(forma_corta.title())
            # Apellido Nombre (corto, sin coma).
            forma_corta_inv = f"{apellido} {primer_nombre}"
            _add(forma_corta_inv)
            _add(forma_corta_inv.upper())
            _add(forma_corta_inv.title())

    # 6. Sin acentos (para textos OCR con mala codificación).
    for v in list(variants):
        sin_acentos = _remove_accents(v)
        if sin_acentos != v:
            _add(sin_acentos)

    return variants


def expand_variants_from_text(name: str, text: str) -> List[str]:
    """Busca formas extendidas del nombre en el texto.

    Si el LLM devolvió "VÁZQUEZ, ELSA A." y el cuerpo del documento usa
    "Elsa Alicia Vázquez", esta función detecta esa forma completa y la
    devuelve como variante adicional. Cubre el caso típico de "el LLM
    devolvió la forma abreviada de la carátula y no la del cuerpo".

    Heurística: <primer_nombre> (1-4 tokens intermedios) <apellido>,
    case-insensitive y accent-insensitive vía _regex_char_class.
    """
    # Probar varias interpretaciones de apellido/nombre y juntar todas las extensiones.
    interpretations: list[tuple[str, str]] = []
    if "," in name:
        ap_, no_ = _split_caratula_name(name)
        interpretations.append((ap_, no_))
    else:
        words = name.split()
        if len(words) >= 2:
            interpretations.append((words[0], " ".join(words[1:])))
            if words[-1] != words[0]:
                interpretations.append((words[-1], " ".join(words[:-1])))

    found: set[str] = set()
    name_norm = _normalize(name).lower()
    for apellido, nombres in interpretations:
        # Primer nombre real: el primer token de >=3 chars (saltea iniciales como "E.")
        nombres_tokens = [t.rstrip(".") for t in nombres.split() if t.rstrip(".")]
        primer = next((t for t in nombres_tokens if len(t) >= 3), None)
        if not primer or len(apellido) < 3:
            continue
        pn = "".join(_regex_char_class(c) for c in primer)
        ap = "".join(_regex_char_class(c) for c in apellido)
        # primer_nombre + 1-4 palabras intermedias + apellido.
        # Los tokens intermedios deben ser: (a) palabra Capitalizada (nombre propio)
        # con >=2 chars, o (b) conector común en nombres compuestos (de/del/la/los/etc).
        # Esto evita matches espurios como "Revol a sus hijos Alfredo".
        # `(?-i:...)` desactiva IGNORECASE para que [A-Z] sí discrimine mayúsculas.
        intermediate = (r"(?:(?-i:[A-ZÁÉÍÓÚÜÑ])[A-Za-zÁÉÍÓÚÜÑáéíóúüñ\.]{1,24}"
                        r"|de|del|la|las|los|y|e)")
        pattern = pn + r"(?:\s+" + intermediate + r"){1,4}\s+" + ap
        for m in re.finditer(pattern, text, re.IGNORECASE):
            match = _normalize(m.group())
            if match.lower() != name_norm and len(match) > 5:
                found.add(match)
    return sorted(found, key=len, reverse=True)


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
        # Tolerancia ortográfica (todas aplicadas por _regex_char_class):
        #   - vocales con/sin acento (NEHUEN ↔ Nehuén, común cuando el LLM
        #     normaliza é a e o cuando el OCR pierde la tilde).
        #   - n ↔ ñ (RODINO ↔ Rodiño).
        #   - S opcional al final de palabras largas (FARÍAS ↔ FARÍA).
        words = variant.split()
        parts = []
        for w in words:
            # Separar puntuación final (coma, punto) de la palabra.
            trail = ""
            while w and w[-1] in ",.;:":
                trail = w[-1] + trail
                w = w[:-1]
            # Decidir si hay que hacer s-opcional antes de mapear caracteres,
            # porque la clase char-a-char genera segmentos multi-char.
            body = w
            suffix = ""
            if len(w) > 3:
                if w[-1:].lower() == "s":
                    body = w[:-1]
                    suffix = "s?"
                else:
                    suffix = "s?"
            ew = "".join(_regex_char_class_in_word(ch, i, len(body))
                         for i, ch in enumerate(body)) + suffix
            if trail:
                ew += re.escape(trail)
            parts.append(ew)
        pattern = r"\s+".join(parts)
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
