"""Orquestador del pipeline completo de anonimización (PR 11).

**Flujo end-to-end** (todo determinista salvo el LLM, que está forzado a
temp=0):

1. **Extract** — leer el .docx con `extract_runs()`.
2. **Detect regex** — `detect_all()` sobre `full_text` (DNI/CUIT/CBU/...).
3. **Detect zones** — `detect_zones()` para priors estructurales.
4. **Detect NER** — opcional; si spaCy no está disponible, se saltea.
5. **Resolve overlaps** — `resolve_overlaps()` con prioridad regex > structure > ner.
6. **Build fichas** — sólo para spans no-regex (los regex van directo a anonimización).
7. **Classify LLM** — clasifica fichas con taxonomía cerrada. Fail-closed a DESCONOCIDO.
8. **Coreference** — agrupa por apellido, asigna placeholders estables.
9. **Build replacements** — combina spans regex (DNI_N etc) + spans clasificados.
10. **Replace** — escribe el .docx anonimizado preservando formato.
11. **Validate** — re-scan regex sobre output; si filtra, renombrar a `*_FAILED.docx`.
12. **Audit log** — JSONL con todo lo que pasó por etapa.

**Composición sobre herencia**: el orquestador es una función (no clase)
que recibe los componentes inyectados (cliente LLM, política, etc.).
Esto facilita testing con mocks y permite al GUI/CLI pasarle distintos
configs sin tener que subclassear.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from app.classify.coreference import CoreferenceResolution, audit_log, resolve_coreference
from app.classify.ficha import build_fichas
from app.classify.llm_classifier import (
    ClassificationResult,
    LLMClassifier,
    get_placeholder_for_role,
)
from app.classify.lm_client import LMStudioClient, LMStudioConfig
from app.classify.taxonomy import AnonymizationPolicy, Role
from app.detect.regex_detectors import detect_all as detect_regex
from app.detect.span import Span, resolve_overlaps
from app.detect.structure import Zone, detect_zones
from app.io.docx_extract import DocxDocument, extract_runs
from app.replace.docx_replacer import write_anonymized_docx
from app.replace.text_replacer import Replacement
from app.validate.post_checks import ValidationReport, validate_docx_output

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


@dataclass
class PipelineConfig:
    """Configuración del pipeline. Inyectable desde CLI/GUI."""

    use_ner: bool = True
    ner_model: str = "es_core_news_md"
    llm_config: LMStudioConfig = field(default_factory=LMStudioConfig)
    policy: AnonymizationPolicy = field(default_factory=AnonymizationPolicy)
    batch_size: int = 10
    max_retries: int = 2
    dry_run: bool = False  # si True, no escribe el archivo de salida


@dataclass
class StageMetrics:
    name: str
    duration_s: float
    n_items: int
    extra: Dict[str, object] = field(default_factory=dict)


@dataclass
class PipelineResult:
    input_path: Path
    output_path: Optional[Path]
    success: bool
    spans: List[Span]
    classifications: List[ClassificationResult]
    coreference: Optional[CoreferenceResolution]
    replacements: List[Replacement]
    validation: Optional[ValidationReport]
    metrics: List[StageMetrics] = field(default_factory=list)
    audit: Dict[str, object] = field(default_factory=dict)


def _stage(metrics: List[StageMetrics], name: str, n: int, t0: float, **extra) -> None:
    metrics.append(StageMetrics(name=name, duration_s=time.time() - t0, n_items=n, extra=extra))


def _build_regex_replacements(
    regex_spans: Sequence[Span],
) -> tuple[List[Replacement], Dict[str, int], List[str]]:
    """Convierte spans regex en Replacements con counters por tipo.

    Devuelve (replacements, counters, expected_placeholders).
    """
    counters: Dict[str, int] = {}
    reps: List[Replacement] = []
    expected: List[str] = []
    # Agrupar por texto normalizado para placeholders estables (mismo
    # DNI repetido → mismo [DNI_N]).
    seen: Dict[tuple[str, str], str] = {}
    for span in regex_spans:
        prefix = _REGEX_PLACEHOLDER_PREFIX.get(span.type, span.type)
        key = (span.type, span.text)
        ph = seen.get(key)
        if ph is None:
            counters[span.type] = counters.get(span.type, 0) + 1
            ph = f"[{prefix}_{counters[span.type]}]"
            seen[key] = ph
            expected.append(ph)
        reps.append(Replacement(start=span.start, end=span.end, replacement=ph))
    return reps, counters, expected


def _build_classified_replacements(
    classified: Sequence[ClassificationResult],
    coref: CoreferenceResolution,
) -> tuple[List[Replacement], List[str]]:
    """Convierte ClassificationResults en Replacements usando el placeholder
    de coreferencia. Sólo si la política dice anonimizar."""
    reps: List[Replacement] = []
    expected: List[str] = []
    seen_placeholders: set[str] = set()
    for r in classified:
        if not r.anonymize:
            continue
        ph = coref.placeholder_by_ficha_id.get(r.ficha.id)
        if ph is None:
            # rol no anonimizable: skip (consistencia con r.anonymize=False)
            continue
        span = r.ficha.span
        reps.append(Replacement(start=span.start, end=span.end, replacement=ph))
        if ph not in seen_placeholders:
            seen_placeholders.add(ph)
            expected.append(ph)
    return reps, expected


def run_pipeline(input_path: Path, config: Optional[PipelineConfig] = None) -> PipelineResult:
    """Ejecuta el pipeline completo sobre un .docx y devuelve el resultado."""
    config = config or PipelineConfig()
    input_path = Path(input_path)
    metrics: List[StageMetrics] = []

    # 1. Extract
    t0 = time.time()
    doc = extract_runs(input_path)
    _stage(metrics, "extract", len(doc.runs), t0, chars=len(doc.full_text))
    text = doc.full_text

    # 2. Regex
    t0 = time.time()
    regex_spans = detect_regex(text)
    _stage(metrics, "detect_regex", len(regex_spans), t0)

    # 3. Zones
    t0 = time.time()
    zones: List[Zone] = detect_zones(text)
    _stage(metrics, "detect_zones", len(zones), t0)

    # 4. NER (opcional)
    ner_spans: List[Span] = []
    if config.use_ner:
        t0 = time.time()
        try:
            from app.detect.ner import detect_entities, is_available

            if is_available(config.ner_model):
                ner_spans = detect_entities(text, model=config.ner_model)
            else:
                logger.warning("NER model %s no disponible; skip", config.ner_model)
        except Exception as exc:
            logger.warning("NER falló (%s); continuando sin NER", exc)
        _stage(metrics, "detect_ner", len(ner_spans), t0)

    # 5. Resolve overlaps
    t0 = time.time()
    all_spans = resolve_overlaps(list(regex_spans) + list(ner_spans))
    _stage(metrics, "resolve_overlaps", len(all_spans), t0)

    # 6. Build fichas (skip regex)
    t0 = time.time()
    fichas = build_fichas(all_spans, text, zones=zones)
    _stage(metrics, "build_fichas", len(fichas), t0)

    # 7. Classify
    t0 = time.time()
    classifications: List[ClassificationResult] = []
    if fichas:
        client = LMStudioClient(config.llm_config)
        classifier = LLMClassifier(
            client=client,
            policy=config.policy,
            batch_size=config.batch_size,
            max_retries=config.max_retries,
        )
        classifications = classifier.classify(fichas)
    _stage(metrics, "classify_llm", len(classifications), t0)

    # 8. Coreference
    t0 = time.time()
    coref = resolve_coreference(classifications)
    _stage(metrics, "coreference", len(coref.clusters), t0)

    # 9. Build replacements (regex first, then classified)
    t0 = time.time()
    regex_only = [s for s in all_spans if s.source == "regex"]
    regex_reps, _, regex_expected = _build_regex_replacements(regex_only)
    cls_reps, cls_expected = _build_classified_replacements(classifications, coref)
    replacements = regex_reps + cls_reps
    expected_placeholders = regex_expected + cls_expected
    _stage(metrics, "build_replacements", len(replacements), t0)

    # 10. Replace (skip if dry_run)
    output_path: Optional[Path] = None
    validation: Optional[ValidationReport] = None
    success = True
    if not config.dry_run:
        t0 = time.time()
        output_path = input_path.with_name(input_path.stem + "_anonimizado.docx")
        write_anonymized_docx(doc, replacements, output_path)
        _stage(metrics, "write_docx", len(replacements), t0)

        # 11. Validate
        t0 = time.time()
        validation = validate_docx_output(
            input_path,
            output_path,
            expected_placeholders=expected_placeholders,
        )
        _stage(metrics, "validate", len(validation.issues), t0)
        if not validation.passed:
            failed_path = output_path.with_name(output_path.stem + "_FAILED.docx")
            output_path.rename(failed_path)
            output_path = failed_path
            success = False
            logger.error(
                "Validación falló; renombrado a %s. Issues: %s",
                failed_path,
                [i.message for i in validation.blockers],
            )

    # 12. Audit
    audit = {
        "input": str(input_path),
        "output": str(output_path) if output_path else None,
        "success": success,
        "n_regex": len(regex_only),
        "n_ner": len(ner_spans),
        "n_fichas": len(fichas),
        "n_classifications": len(classifications),
        "n_clusters": len(coref.clusters),
        "n_replacements": len(replacements),
        "coreference": audit_log(coref),
        "metrics": [
            {"stage": m.name, "duration_s": round(m.duration_s, 4), "n": m.n_items, **m.extra}
            for m in metrics
        ],
        "validation": (
            {
                "passed": validation.passed,
                "issues": [
                    {"code": i.code, "severity": i.severity, "message": i.message}
                    for i in validation.issues
                ],
            }
            if validation
            else None
        ),
    }

    return PipelineResult(
        input_path=input_path,
        output_path=output_path,
        success=success,
        spans=all_spans,
        classifications=classifications,
        coreference=coref,
        replacements=replacements,
        validation=validation,
        metrics=metrics,
        audit=audit,
    )


def write_audit_log(result: PipelineResult, log_path: Path) -> None:
    """Escribe el audit log como JSON pretty-printed."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(result.audit, ensure_ascii=False, indent=2), encoding="utf-8")
