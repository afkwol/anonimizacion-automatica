"""Pipeline liviano de anonimización ("opción D").

Para causas civiles/laborales/comerciales donde solo se anonimizan las
partes del proceso + identificadores numéricos (DNI/CUIT/CBU/etc.).

**Flujo** (4 pasos vs 13 del pipeline completo):

1. **Extract** — leer PDF o DOCX → full_text.
2. **Detect** —
   a. LLM: un solo prompt extrae nombres de partes a anonimizar.
   b. Carátula: detector estructural como red de seguridad.
   c. Regex: DNI/CUIT/CBU/email/teléfono/patente.
   d. Name search: buscar variantes de cada nombre en todo el texto.
3. **Replace** — asignar placeholders estables y escribir output.
4. **Audit** — JSON con lo que pasó.

**Costo**: 1 call LLM (~2s) + regex + string search. Sin NER, sin
fichas, sin clasificación batch, sin coreference.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from app.classify.lm_client import LMStudioClient, LMStudioConfig
from app.detect.caratula_parties import detect_caratula_parties
from app.detect.llm_parties import extract_parties
from app.detect.name_variants import (
    expand_variants_from_text,
    find_all_occurrences,
    generate_variants,
)
from app.detect.regex_detectors import detect_all as detect_regex
from app.detect.span import Span, resolve_overlaps
from app.io.docx_extract import DocxDocument, extract_runs
from app.io.pdf_extract import PdfDocument, extract_pdf
from app.replace.docx_replacer import write_anonymized_docx
from app.replace.pdf_replacer import write_anonymized_pdf
from app.replace.text_replacer import Replacement

logger = logging.getLogger(__name__)


# Mapping tipo regex → prefijo de placeholder.
_REGEX_PLACEHOLDER_PREFIX: Dict[str, str] = {
    "DNI": "DNI",
    "CUIT": "CUIT",
    "CBU": "CBU",
    "EMAIL": "EMAIL",
    "TELEFONO": "TEL",
    "PATENTE": "PATENTE",
    "PASAPORTE": "PASAPORTE",
}

# Sufijos de personas jurídicas (con y sin puntos).
_COMPANY_SUFFIXES = (
    ("S.A.", "S.A."), ("SA", "S.A."),
    ("S.R.L.", "S.R.L."), ("SRL", "S.R.L."),
    ("S.A.S.", "S.A.S."), ("SAS", "S.A.S."),
    ("S.A.C.I.", "S.A.C.I."), ("SACI", "S.A.C.I."),
    ("S.A.C.I.F.", "S.A.C.I.F."),
    ("S.C.", "S.C."), ("S.H.", "S.H."), ("S.E.", "S.E."),
)


def _to_initials(name: str) -> str:
    """Convierte un nombre a iniciales.

    'SONZINI, NAHUEL ANTONIO' → 'S., N. A.'
    'ANJOR S.A.'               → 'A. S.A.'
    'Juan Pérez'               → 'J. P.'
    """
    name = name.strip()
    if not name:
        return name

    # Persona jurídica: conservar sufijo societario.
    upper = name.upper().rstrip()
    for raw_suffix, canonical in _COMPANY_SUFFIXES:
        if upper.endswith(raw_suffix):
            base = name[:len(name) - len(raw_suffix)].strip()
            if base:
                initials = ". ".join(w[0].upper() for w in base.split() if w) + "."
                return f"{initials} {canonical}"
            return name

    # Formato "APELLIDO, NOMBRE".
    if "," in name:
        parts = name.split(",", 1)
        apellido = parts[0].strip()
        nombres = parts[1].strip()
        ap = ". ".join(w[0].upper() for w in apellido.split() if w) + "."
        if nombres:
            nm = ". ".join(w[0].upper() for w in nombres.split() if w) + "."
            return f"{ap}, {nm}"
        return ap

    # Formato "Nombre Apellido" o palabra sola.
    words = name.split()
    if not words:
        return name
    return ". ".join(w[0].upper() for w in words if w) + "."


@dataclass
class LiteConfig:
    """Configuración del pipeline liviano."""

    llm_config: LMStudioConfig = field(default_factory=LMStudioConfig)
    dry_run: bool = False


@dataclass
class LiteResult:
    """Resultado del pipeline liviano."""

    input_path: Path
    output_path: Optional[Path]
    success: bool
    parties_from_llm: List[dict]
    parties_from_caratula: List[str]
    name_spans: List[Span]
    regex_spans: List[Span]
    replacements: List[Replacement]
    audit: Dict[str, object] = field(default_factory=dict)
    elapsed_s: float = 0.0


def run_pipeline_lite(
    input_path: Path,
    config: Optional[LiteConfig] = None,
) -> LiteResult:
    """Pipeline liviano: LLM extrae partes → string search → replace."""
    config = config or LiteConfig()
    input_path = Path(input_path)
    ext = input_path.suffix.lower()
    t_start = time.time()
    timings: Dict[str, float] = {}

    # ── 1. Extract ──────────────────────────────────────────────────
    t0 = time.time()
    pdf_doc: Optional[PdfDocument] = None
    docx_doc: Optional[DocxDocument] = None

    if ext == ".pdf":
        pdf_doc = extract_pdf(input_path)
        text = pdf_doc.full_text
    elif ext == ".docx":
        docx_doc = extract_runs(input_path)
        text = docx_doc.full_text
    else:
        raise ValueError(f"Formato no soportado: {ext}. Usar .docx o .pdf.")
    timings["extract"] = time.time() - t0

    # ── 2a. LLM: extraer partes ────────────────────────────────────
    t0 = time.time()
    client = LMStudioClient(config.llm_config)
    parties_llm = extract_parties(text, client)
    timings["llm_parties"] = time.time() - t0
    logger.info("LLM: %d partes → %s", len(parties_llm), [p["nombre"] for p in parties_llm])

    # ── 2b. Carátula: red de seguridad ─────────────────────────────
    t0 = time.time()
    caratula_spans = detect_caratula_parties(text)
    timings["caratula"] = time.time() - t0

    # Extraer nombres únicos de la carátula que el LLM no haya encontrado.
    caratula_names: List[str] = []
    llm_names_lower = {p["nombre"].lower() for p in parties_llm}
    seen_caratula: set[str] = set()
    from app.detect.llm_parties import _is_company

    for s in caratula_spans:
        name = s.text.strip()
        if _is_company(name):
            logger.info("Carátula descartó persona jurídica: %s", name)
            continue
        if name.lower() not in llm_names_lower and name.lower() not in seen_caratula:
            seen_caratula.add(name.lower())
            rol = "actor" if s.metadata.get("role_hint") == "PARTE_ACTORA" else "demandado"
            parties_llm.append({"nombre": name, "rol": rol})
            caratula_names.append(name)
            logger.info("Carátula agregó: %s (rol=%s)", name, rol)

    # ── 2b'. Guardrail: 0 partes en doc con texto sustancial ──────
    # Si después del LLM y la red de seguridad de carátula quedaron 0 partes
    # pero el documento tiene texto significativo, marcar warning visible:
    # el output podría salir SIN anonimizar y el operador no enterarse.
    warnings: List[str] = []
    if len(parties_llm) == 0 and len(text.strip()) > 500:
        warning = (
            f"GUARDRAIL: 0 partes detectadas en documento de {len(text)} chars. "
            f"Output saldrá sin anonimizar. Revisar manualmente: la carátula "
            f"puede no estar al inicio del PDF, o ser un fallo plenamente "
            f"corporativo, o el LLM falló."
        )
        warnings.append(warning)
        logger.warning(warning)

    # ── 2c. Regex ──────────────────────────────────────────────────
    t0 = time.time()
    regex_spans = detect_regex(text)
    timings["regex"] = time.time() - t0

    # ── 2d. Name search ────────────────────────────────────────────
    t0 = time.time()
    all_name_spans: List[Span] = []
    party_to_placeholder: Dict[str, str] = {}

    for party in parties_llm:
        nombre = party["nombre"]
        placeholder = _to_initials(nombre)
        party_to_placeholder[nombre] = placeholder

        variants = generate_variants(nombre)
        # Si el LLM dio la forma abreviada de la carátula ("VÁZQUEZ, ELSA A."),
        # buscar en el cuerpo formas extendidas ("Elsa Alicia Vázquez") y agregarlas.
        extra = expand_variants_from_text(nombre, text)
        if extra:
            logger.info("  + %d variantes extendidas detectadas en el texto: %s",
                        len(extra), extra[:3])
            variants.extend(extra)
        spans = find_all_occurrences(text, variants)
        # Tag spans with their placeholder.
        for s in spans:
            s.metadata["placeholder"] = placeholder
            s.metadata["party_name"] = nombre
            s.metadata["party_rol"] = party.get("rol", "desconocido")
        all_name_spans.extend(spans)
        logger.info(
            "%s → %s (%d variantes, %d ocurrencias)",
            nombre, placeholder, len(variants), len(spans),
        )

    timings["name_search"] = time.time() - t0

    # ── Resolve overlaps (name spans + regex) ──────────────────────
    # Name spans tienen source="text_search" (no está en SOURCE_PRIORITY,
    # default 0). Forzamos prioridad alta para que ganen sobre regex
    # cuando un DNI está dentro de un nombre.
    from app.detect.span import SOURCE_PRIORITY
    SOURCE_PRIORITY.setdefault("text_search", 90)

    all_spans = resolve_overlaps(list(regex_spans) + list(all_name_spans))

    # ── 3. Build replacements ──────────────────────────────────────
    t0 = time.time()
    replacements: List[Replacement] = []
    expected: List[str] = []

    # Regex replacements.
    regex_seen: Dict[tuple[str, str], str] = {}
    regex_counters: Dict[str, int] = {}
    for s in all_spans:
        if s.source == "regex":
            prefix = _REGEX_PLACEHOLDER_PREFIX.get(s.type, s.type)
            key = (s.type, s.text)
            ph = regex_seen.get(key)
            if ph is None:
                regex_counters[s.type] = regex_counters.get(s.type, 0) + 1
                ph = f"[{prefix}_{regex_counters[s.type]}]"
                regex_seen[key] = ph
                expected.append(ph)
            replacements.append(Replacement(start=s.start, end=s.end, replacement=ph))

    # Name replacements.
    for s in all_spans:
        if s.source == "text_search":
            ph = s.metadata.get("placeholder", "[PERSONA]")
            replacements.append(Replacement(start=s.start, end=s.end, replacement=ph))
            if ph not in expected:
                expected.append(ph)

    timings["build_replacements"] = time.time() - t0

    # ── Write output ───────────────────────────────────────────────
    output_path: Optional[Path] = None
    success = True

    if not config.dry_run and replacements:
        t0 = time.time()
        if pdf_doc is not None:
            output_path = input_path.with_name(input_path.stem + "_anonimizado.pdf")
            write_anonymized_pdf(pdf_doc, replacements, output_path)
        else:
            assert docx_doc is not None
            output_path = input_path.with_name(input_path.stem + "_anonimizado.docx")
            write_anonymized_docx(docx_doc, replacements, output_path)
        timings["write"] = time.time() - t0

    elapsed = time.time() - t_start

    # ── 4. Audit ───────────────────────────────────────────────────
    audit = {
        "mode": "lite",
        "input": str(input_path),
        "output": str(output_path) if output_path else None,
        "success": success,
        "elapsed_s": round(elapsed, 2),
        "parties": [
            {
                "nombre": p["nombre"],
                "rol": p["rol"],
                "placeholder": party_to_placeholder.get(p["nombre"]),
                "from_caratula": p["nombre"] in caratula_names,
            }
            for p in parties_llm
        ],
        "n_name_spans": len(all_name_spans),
        "n_regex_spans": len(regex_spans),
        "n_replacements": len(replacements),
        "warnings": warnings,
        # Campos de observabilidad para detectar regresiones a escala.
        "n_pages": pdf_doc.page_count if pdf_doc else 0,
        "text_chars": len(text),
        "model": config.llm_config.model,
        "replacements_per_page": (
            round(len(replacements) / pdf_doc.page_count, 2)
            if pdf_doc and pdf_doc.page_count else 0
        ),
        "replacements_per_kchar": (
            round(len(replacements) * 1000 / len(text), 2) if text else 0
        ),
        "timings": {k: round(v, 3) for k, v in timings.items()},
    }

    return LiteResult(
        input_path=input_path,
        output_path=output_path,
        success=success,
        parties_from_llm=[p for p in parties_llm if p["nombre"] not in caratula_names],
        parties_from_caratula=caratula_names,
        name_spans=all_name_spans,
        regex_spans=regex_spans,
        replacements=replacements,
        audit=audit,
        elapsed_s=elapsed,
    )


def write_audit_log(result: LiteResult, log_path: Path) -> None:
    """Escribe el audit log como JSON pretty-printed."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(result.audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
