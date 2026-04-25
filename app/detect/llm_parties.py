"""Extracción de partes procesales vía un solo prompt al LLM.

Modo liviano ("opción D"): en vez de NER → fichas → clasificación
por batch, enviamos el texto completo del documento al LLM y le
pedimos que devuelva SOLO las partes del proceso a anonimizar.

**Ventaja**: 1 call, ~2 segundos (8K prefill a 5000 tok/s + ~50 tokens
generación a 80 tok/s). Vs 26+ segundos del pipeline completo con
NER + fichas + clasificación batch.

**Validación anti-alucinación**: cada nombre devuelto por el LLM se
verifica contra el texto literal. Si no existe → se descarta.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import List, Optional

from app.classify.lm_client import LMStudioClient, LMStudioConfig

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
Sos un asistente de anonimización judicial argentina.
Dado el texto de un fallo o resolución judicial, identificá ÚNICAMENTE \
las partes del proceso que deben anonimizarse.

INCLUIR:
- Parte actora (persona física)
- Parte demandada (persona física, NO personas jurídicas como S.A., S.R.L.)
- Causantes en sucesiones, declaratorias de herederos y testamentarios \
(la persona fallecida cuyo patrimonio se reparte)
- Herederos, legatarios y beneficiarios mencionados
- Testigos
- Víctimas
- Menores mencionados

NO INCLUIR:
- Jueces, vocales, camaristas, conjueces
- Letrados (apoderados, patrocinantes, defensores)
- Secretarios, prosecretarios, actuarios
- Autores de doctrina o jurisprudencia citada
- Personas jurídicas (S.A., S.R.L., S.A.S., cooperativas, asociaciones)
- Funcionarios públicos actuando en rol oficial
- Peritos, mediadores, síndicos

Devolvé SOLO un JSON válido con este formato, sin texto adicional:
{"partes": [{"nombre": "APELLIDO, NOMBRE", "rol": "actor|demandado|causante|heredero|testigo|victima|menor"}]}

Usá el nombre EXACTO como aparece en el documento (respetá mayúsculas, \
acentos, comas). Si la misma persona aparece con varias formas, usá la \
forma más completa (la de la carátula).

Si no hay partes persona física para anonimizar, devolvé {"partes": []}.
"""


def extract_parties(
    text: str,
    client: LMStudioClient,
    *,
    max_chars: int = 40_000,
) -> List[dict]:
    """Extrae las partes a anonimizar del texto completo via LLM.

    Args:
        text: texto completo del documento.
        client: cliente LM Studio configurado.
        max_chars: truncar texto si supera este límite (seguridad para
            no exceder context window del modelo).

    Returns:
        Lista de dicts con keys "nombre" y "rol", validados contra el
        texto fuente. Los nombres que el LLM devuelve pero que no
        existen en el texto se descartan silenciosamente.
    """
    truncated = text[:max_chars]

    raw = client.chat(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=truncated,
        force_json=True,
    )

    parties = _parse_response(raw)
    validated = _validate_against_text(parties, text)

    logger.info(
        "LLM devolvió %d partes, %d validadas contra texto",
        len(parties),
        len(validated),
    )

    return validated


def _parse_response(raw: str) -> List[dict]:
    """Parsea la respuesta del LLM. Tolerante a markdown fences."""
    # Quitar ```json ... ``` si el modelo lo envuelve.
    cleaned = re.sub(r"```json\s*", "", raw)
    cleaned = re.sub(r"```\s*", "", cleaned)
    cleaned = cleaned.strip()

    # Algunos modelos reasoning meten texto antes del JSON.
    # Buscar el primer '{'.
    brace = cleaned.find("{")
    if brace > 0:
        cleaned = cleaned[brace:]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("LLM devolvió JSON inválido: %s", raw[:200])
        return []

    partes = data.get("partes", [])
    if not isinstance(partes, list):
        logger.warning("'partes' no es lista: %s", type(partes))
        return []

    result = []
    for p in partes:
        if isinstance(p, dict) and "nombre" in p:
            result.append({
                "nombre": str(p["nombre"]).strip(),
                "rol": str(p.get("rol", "desconocido")).strip().lower(),
            })
    return result


_COMPANY_SUFFIXES_FILTER = (
    "S.A.", "SA", "S.R.L.", "SRL", "S.A.S.", "SAS",
    "S.A.C.I.", "SACI", "S.A.C.I.F.", "SACIF",
    "S.C.", "S.H.", "S.E.",
)


def _remove_accents(s: str) -> str:
    """Quita acentos para búsqueda tolerante."""
    nfkd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn")


_COMPANY_KEYWORDS = (
    "COOPERATIVA", "MUNICIPALIDAD", "GOBIERNO", "PROVINCIA", "ESTADO",
    "BANCO", "EMPRESA", "ASOCIACIÓN", "ASOCIACION", "FUNDACIÓN", "FUNDACION",
    "MUTUAL", "SINDICATO", "FEDERACIÓN", "FEDERACION", "MINISTERIO",
)


def _is_company(name: str) -> bool:
    """Detecta si el nombre corresponde a una persona jurídica.

    Heurística: termina en sufijo societario (SA, SRL, etc.) **o** contiene
    sufijo societario embebido (PLAN OVALO S.A. DE AHORRO ...) **o** alguna
    keyword obvia de persona jurídica.
    """
    upper = name.upper().rstrip(".")
    # Colapsar puntos para detectar sufijos embebidos: "S.A." → "SA",
    # "S.R.L." → "SRL". Así "PLAN OVALO S.A. DE AHORRO" → "PLAN OVALO SA DE AHORRO"
    # y podemos buscar \bSA\b de forma confiable.
    compact = re.sub(r"\.", "", upper)
    bare_suffixes = {s.rstrip(".").replace(".", "") for s in _COMPANY_SUFFIXES_FILTER}
    tokens = re.findall(r"\w+", compact)
    if any(t in bare_suffixes for t in tokens):
        return True
    # Keywords típicas de persona jurídica
    for kw in _COMPANY_KEYWORDS:
        if re.search(r"\b" + kw + r"\b", upper):
            return True
    return False


_DOUBLE_CONSONANT_RE = re.compile(r"([bcdfghjklmnpqrstvwxz])\1", re.IGNORECASE)


def _collapse_doubles(s: str) -> str:
    """Colapsa consonantes dobles (tt→t, ll→l, nn→n, etc.).

    Útil para tolerar typos del LLM en apellidos italianos como
    'Galletini' vs 'Gallettini', o errores comunes con ll/l, rr/r.
    """
    return _DOUBLE_CONSONANT_RE.sub(r"\1", s)


def _correct_name_with_text(name: str, text_words: List[str]) -> Optional[str]:
    """Si la grafía del LLM no aparece pero la forma sin dobles consonantes
    coincide con una palabra del texto, devuelve la versión corregida.

    Ej: LLM='GALLETTINI, EZEQUIEL EMILIANO', texto contiene 'Galletini'
    → devuelve 'Galletini, EZEQUIEL EMILIANO'.
    """
    text_map: dict = {}
    for tw in text_words:
        if len(tw) < 3:
            continue
        key = _collapse_doubles(_remove_accents(tw.lower()))
        text_map.setdefault(key, tw)

    has_comma = "," in name
    if has_comma:
        apellido_str, resto_str = name.split(",", 1)
        apellido_words = apellido_str.split()
        resto_words = resto_str.split()
        all_words = apellido_words + resto_words
        n_apellido = len(apellido_words)
    else:
        all_words = name.split()
        n_apellido = 0

    corrected = []
    found_correction = False
    for w in all_words:
        key = _collapse_doubles(_remove_accents(w.lower()))
        if key in text_map and text_map[key].lower() != w.lower():
            corrected.append(text_map[key])
            found_correction = True
        else:
            corrected.append(w)

    if not found_correction:
        return None

    if has_comma:
        return " ".join(corrected[:n_apellido]) + ", " + " ".join(corrected[n_apellido:])
    return " ".join(corrected)


def _validate_against_text(
    parties: List[dict],
    text: str,
) -> List[dict]:
    """Descarta partes cuyo nombre no existe literalmente en el texto.

    También descarta personas jurídicas (S.A., S.R.L., etc.).
    Busca con y sin acentos para tolerar PDFs con mala codificación, y
    como último recurso colapsa consonantes dobles para tolerar typos
    del LLM (Gallettini → Galletini).
    """
    validated = []
    # Normalizar whitespace para tolerar saltos de línea en PDFs.
    text_norm = re.sub(r"\s+", " ", text)
    text_lower = text_norm.lower()
    text_no_acc = _remove_accents(text_lower)
    text_words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", text_norm)

    for p in parties:
        name = p["nombre"]
        if not name or len(name) < 3:
            continue

        if _is_company(name):
            logger.info(
                "Descartando '%s' (rol=%s): persona jurídica",
                name, p["rol"],
            )
            continue

        name_norm = re.sub(r"\s+", " ", name)

        # Generar formas a buscar: original + invertida (APELLIDO NOMBRE ↔ NOMBRE APELLIDO).
        candidates = [name_norm]
        if "," in name_norm:
            parts = name_norm.split(",", 1)
            candidates.append(f"{parts[1].strip()} {parts[0].strip()}")
        else:
            words = name_norm.split()
            if len(words) >= 2:
                # Probar primera palabra como apellido (DÍAZ MARÍA DEL CARMEN → María del Carmen Díaz).
                candidates.append(f"{' '.join(words[1:])} {words[0]}")
                # Probar última palabra como apellido (Nombre Apellido → Apellido Nombre).
                candidates.append(f"{words[-1]} {' '.join(words[:-1])}")

        found = False
        for candidate in candidates:
            if candidate in text_norm:
                found = True
                break
            if candidate.lower() in text_lower:
                found = True
                break
            if _remove_accents(candidate.lower()) in text_no_acc:
                found = True
                break
        if found:
            validated.append(p)
            continue

        # Último recurso: corregir typo de consonantes dobles del LLM
        # contra grafía real del texto.
        corrected = _correct_name_with_text(name_norm, text_words)
        if corrected and corrected != name_norm:
            logger.info(
                "Corrigiendo grafía LLM '%s' → '%s' (consonantes dobles)",
                name, corrected,
            )
            p_corrected = dict(p)
            p_corrected["nombre"] = corrected
            validated.append(p_corrected)
            continue

        logger.info(
            "Descartando '%s' (rol=%s): no encontrado en texto",
            name, p["rol"],
        )

    return validated
