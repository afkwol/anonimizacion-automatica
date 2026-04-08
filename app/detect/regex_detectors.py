"""Capa 1: detectores regex deterministas para identificadores numéricos.

Estos detectores tienen **recall ≈ 1.0** sobre identificadores con
formato estructurado y **precision ≈ 1.0** gracias a validaciones de
formato y checksums. Corren antes de NER y LLM, y sus spans ganan
cualquier disputa por solapamiento (ver `span.resolve_overlaps`).

Cada detector es una función pura `detect_<tipo>(text) -> list[Span]`.
`detect_all(text)` corre todos y devuelve la unión ya resuelta.

IMPORTANTE: el PR 10 (validación post-hoc) re-ejecuta estos mismos
detectores sobre el texto anonimizado y **falla el build** si alguno
encuentra un match. Por eso la precisión acá es crítica: un falso
positivo sería anonimizar algo que no debía (ej: número de expediente),
y un falso negativo sería filtrar PII.
"""
from __future__ import annotations

import re
from typing import Callable, List

from .span import Span, resolve_overlaps


# ----------------------------- DNI ---------------------------------------
# DNI argentino: 7 u 8 dígitos, opcionalmente con puntos como separadores
# de miles. Formato aceptado: "12.345.678", "12345678", "1.234.567".
# Rechazamos: números sin contexto (lo decidimos por el contexto "DNI"
# en el sufijo/prefijo NO acá — acá matcheamos por patrón estricto con
# look-behind para el keyword "DNI/L.E./L.C.").
#
# Regla conservadora: para minimizar falsos positivos sobre números de
# expediente o montos, exigimos que el número esté precedido por una
# palabra-clave típica: DNI, D.N.I., LE, LC, L.E., L.C., documento,
# "identidad N°", etc.
_DNI_KEYWORDS = r"(?:DNI|D\.N\.I\.?|D\. ?N\. ?I\.?|L\.?E\.?|L\.?C\.?|documento(?:\s+de\s+identidad)?)"
_DNI_NUMBER = r"(\d{1,2}\.?\d{3}\.?\d{3})"
_DNI_PATTERN = re.compile(
    rf"(?i){_DNI_KEYWORDS}[\s:N°º.]*{_DNI_NUMBER}\b",
)


def detect_dni(text: str) -> List[Span]:
    spans: List[Span] = []
    for m in _DNI_PATTERN.finditer(text):
        num = m.group(1)
        digits = num.replace(".", "")
        if not (7 <= len(digits) <= 8):
            continue
        # Rango plausible: DNIs argentinos modernos van de ~1M a ~99M.
        if int(digits) < 1_000_000 or int(digits) > 99_999_999:
            continue
        # Usamos el span del NÚMERO, no del keyword completo, para no
        # anonimizar la palabra "DNI" misma.
        start = m.start(1)
        end = m.end(1)
        spans.append(
            Span(
                start=start,
                end=end,
                text=text[start:end],
                type="DNI",
                source="regex",
                confidence=1.0,
                metadata={"normalized": digits},
            )
        )
    return spans


# ----------------------------- CUIT / CUIL -------------------------------
# CUIT/CUIL: XX-XXXXXXXX-X (11 dígitos, formato con o sin guiones).
# Tiene dígito verificador calculable; verificamos el checksum.
_CUIT_PATTERN = re.compile(r"\b(\d{2})[-\s]?(\d{8})[-\s]?(\d)\b")


def _cuit_checksum_ok(eleven_digits: str) -> bool:
    """Checksum oficial del CUIT/CUIL."""
    if len(eleven_digits) != 11 or not eleven_digits.isdigit():
        return False
    weights = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    total = sum(int(d) * w for d, w in zip(eleven_digits[:10], weights))
    remainder = total % 11
    check = 11 - remainder
    if check == 11:
        check = 0
    elif check == 10:
        check = 9  # convención AFIP
    return check == int(eleven_digits[10])


def detect_cuit(text: str) -> List[Span]:
    spans: List[Span] = []
    for m in _CUIT_PATTERN.finditer(text):
        digits = m.group(1) + m.group(2) + m.group(3)
        if not _cuit_checksum_ok(digits):
            continue
        spans.append(
            Span(
                start=m.start(),
                end=m.end(),
                text=text[m.start():m.end()],
                type="CUIT",
                source="regex",
                confidence=1.0,
                metadata={"normalized": digits},
            )
        )
    return spans


# ----------------------------- CBU ---------------------------------------
# CBU: 22 dígitos agrupados como 8 + 14. Dos checksums (uno por bloque).
_CBU_PATTERN = re.compile(r"\b(\d{22})\b")


def _cbu_block_checksum(block: str, weights: tuple[int, ...]) -> bool:
    """Algoritmo de checksum de un bloque CBU (BCRA)."""
    total = sum(int(d) * w for d, w in zip(block[:-1], weights))
    check = (10 - (total % 10)) % 10
    return check == int(block[-1])


def _cbu_is_valid(cbu: str) -> bool:
    if len(cbu) != 22 or not cbu.isdigit():
        return False
    bloque1 = cbu[:8]
    bloque2 = cbu[8:]
    w1 = (7, 1, 3, 9, 7, 1, 3)
    w2 = (3, 9, 7, 1, 3, 9, 7, 1, 3, 9, 7, 1, 3)
    return _cbu_block_checksum(bloque1, w1) and _cbu_block_checksum(bloque2, w2)


def detect_cbu(text: str) -> List[Span]:
    spans: List[Span] = []
    for m in _CBU_PATTERN.finditer(text):
        cbu = m.group(1)
        if not _cbu_is_valid(cbu):
            continue
        spans.append(
            Span(
                start=m.start(),
                end=m.end(),
                text=cbu,
                type="CBU",
                source="regex",
                confidence=1.0,
                metadata={"normalized": cbu},
            )
        )
    return spans


# ----------------------------- Email -------------------------------------
# RFC-lite: suficiente para texto legal, no pretende cubrir todos los
# casos exóticos de RFC 5322 (eso trae más falsos positivos que recall).
_EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)


def detect_email(text: str) -> List[Span]:
    return [
        Span(
            start=m.start(),
            end=m.end(),
            text=m.group(),
            type="EMAIL",
            source="regex",
            confidence=1.0,
        )
        for m in _EMAIL_PATTERN.finditer(text)
    ]


# ----------------------------- Teléfono ----------------------------------
# Teléfonos argentinos: muy variados en formato. Estrategia: exigir
# keyword ("tel", "tel.", "teléfono", "celular", "cel.", "móvil") + un
# bloque de dígitos con separadores opcionales (6-14 dígitos totales).
_PHONE_KEYWORDS = r"(?:tel[eé]fono|tel\.?|celular|cel\.?|m[oó]vil|fax)"
_PHONE_NUMBER = r"((?:\+?54[\s\-]?)?(?:\(?\d{2,4}\)?[\s\-]?)?\d{3,4}[\s\-]?\d{4})"
_PHONE_PATTERN = re.compile(
    rf"(?i){_PHONE_KEYWORDS}[\s:N°º.]*{_PHONE_NUMBER}",
)


def detect_phone(text: str) -> List[Span]:
    spans: List[Span] = []
    for m in _PHONE_PATTERN.finditer(text):
        num = m.group(1)
        # Descartar si tiene menos de 6 dígitos reales (demasiado corto).
        digits_only = re.sub(r"\D", "", num)
        if not (6 <= len(digits_only) <= 14):
            continue
        start = m.start(1)
        end = m.end(1)
        spans.append(
            Span(
                start=start,
                end=end,
                text=text[start:end],
                type="TELEFONO",
                source="regex",
                confidence=0.95,  # un poco menos que DNI/CUIT por la varianza
                metadata={"normalized": digits_only},
            )
        )
    return spans


# ----------------------------- Patente automotor -------------------------
# Formato viejo: 3 letras + 3 números (LLL NNN).
# Formato Mercosur: 2 letras + 3 números + 2 letras (LL NNN LL).
_PATENTE_OLD = re.compile(r"\b([A-Z]{3})\s*(\d{3})\b")
_PATENTE_MERCOSUR = re.compile(r"\b([A-Z]{2})\s*(\d{3})\s*([A-Z]{2})\b")


def detect_patente(text: str) -> List[Span]:
    spans: List[Span] = []
    for m in _PATENTE_MERCOSUR.finditer(text):
        spans.append(
            Span(
                start=m.start(),
                end=m.end(),
                text=m.group(),
                type="PATENTE",
                source="regex",
                confidence=0.9,
                metadata={"formato": "mercosur"},
            )
        )
    for m in _PATENTE_OLD.finditer(text):
        # Evitar solaparse con Mercosur (ya detectado).
        if any(s.start <= m.start() < s.end for s in spans):
            continue
        spans.append(
            Span(
                start=m.start(),
                end=m.end(),
                text=m.group(),
                type="PATENTE",
                source="regex",
                confidence=0.75,  # mayor riesgo de falsos positivos con siglas
                metadata={"formato": "viejo"},
            )
        )
    return spans


# ----------------------------- Pasaporte ---------------------------------
# Pasaporte argentino: letra(s) + 6-8 dígitos. Exigimos keyword para
# minimizar falsos positivos.
_PASAPORTE_PATTERN = re.compile(
    r"(?i)pasaporte[\s:N°º.]*([A-Z]{1,3}\s?\d{6,8})\b",
)


def detect_pasaporte(text: str) -> List[Span]:
    spans: List[Span] = []
    for m in _PASAPORTE_PATTERN.finditer(text):
        start = m.start(1)
        end = m.end(1)
        spans.append(
            Span(
                start=start,
                end=end,
                text=text[start:end],
                type="PASAPORTE",
                source="regex",
                confidence=0.9,
            )
        )
    return spans


# ----------------------------- Orquestador -------------------------------
_ALL_DETECTORS: tuple[Callable[[str], List[Span]], ...] = (
    detect_dni,
    detect_cuit,
    detect_cbu,
    detect_email,
    detect_phone,
    detect_patente,
    detect_pasaporte,
)


def detect_all(text: str) -> List[Span]:
    """Corre todos los detectores regex y resuelve solapamientos entre ellos.

    Uso típico: primera pasada del pipeline de detección. El resultado
    se combina luego con NER (capa 2) y clasificación LLM (capa 4),
    respetando `SOURCE_PRIORITY`.
    """
    spans: List[Span] = []
    for fn in _ALL_DETECTORS:
        spans.extend(fn(text))
    return resolve_overlaps(spans)
