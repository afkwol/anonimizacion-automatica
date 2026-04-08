"""Validación post-hoc bloqueante (PR 10).

**Filosofía fail-closed**: si CUALQUIER chequeo falla, el archivo NO se
entrega. Mejor errar diciendo "no pude" que entregar un doc con PII
filtrada o estructura corrupta.

Chequeos:
1. **Re-scan regex**: corre los mismos detectores deterministas del PR 4
   sobre el texto del archivo de salida. Si encuentra DNI/CUIT/CBU/etc.,
   significa que la pipeline filtró PII estructurada — abortar.
2. **Ratio de longitud**: el output no puede colapsar. Default `[0.5, 1.1]`
   contra el input (más permisivo que el plan original porque borrar
   nombres largos puede achicar bastante).
3. **Integridad de placeholders**: para cada placeholder esperado, debe
   aparecer al menos una vez en el texto de salida.
4. **Integridad estructural DOCX**: el número de parts del ZIP debe ser
   idéntico al original (no se perdieron headers/footers/footnotes).
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from app.detect.regex_detectors import detect_all
from app.io.docx_extract import extract_runs


@dataclass
class ValidationIssue:
    """Un problema concreto detectado por el validador."""

    code: str  # ej: "REGEX_LEAK", "LENGTH_RATIO", "MISSING_PLACEHOLDER"
    severity: str  # "blocker" | "warning"
    message: str


@dataclass
class ValidationReport:
    passed: bool
    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def blockers(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "blocker"]

    def add(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)
        if issue.severity == "blocker":
            self.passed = False


# Rangos default — pueden overridear
DEFAULT_LENGTH_RATIO_MIN = 0.5
DEFAULT_LENGTH_RATIO_MAX = 1.1


def check_regex_leak(text: str) -> List[ValidationIssue]:
    """Re-corre los detectores regex sobre el texto de salida.

    Cualquier match aquí es un BLOQUEANTE: significa que un identificador
    estructurado se filtró del pipeline. NO debería pasar nunca si la
    detección de PR 4 funcionó bien.
    """
    leaks = detect_all(text)
    return [
        ValidationIssue(
            code="REGEX_LEAK",
            severity="blocker",
            message=(
                f"Identificador no anonimizado en el output: tipo={leak.type} "
                f"texto='{leak.text}' offset={leak.start}"
            ),
        )
        for leak in leaks
    ]


def check_length_ratio(
    input_text: str,
    output_text: str,
    *,
    min_ratio: float = DEFAULT_LENGTH_RATIO_MIN,
    max_ratio: float = DEFAULT_LENGTH_RATIO_MAX,
) -> List[ValidationIssue]:
    """El output no puede colapsar ni inflarse desmesuradamente."""
    if not input_text:
        return []
    ratio = len(output_text) / len(input_text)
    if ratio < min_ratio or ratio > max_ratio:
        return [
            ValidationIssue(
                code="LENGTH_RATIO",
                severity="blocker",
                message=(
                    f"Ratio de longitud out/in = {ratio:.3f} fuera de "
                    f"[{min_ratio}, {max_ratio}]. Posible pérdida o inflado."
                ),
            )
        ]
    return []


def check_placeholders_present(
    output_text: str, expected: Sequence[str]
) -> List[ValidationIssue]:
    """Cada placeholder esperado debe aparecer al menos una vez."""
    missing = [p for p in expected if p and p not in output_text]
    return [
        ValidationIssue(
            code="MISSING_PLACEHOLDER",
            severity="blocker",
            message=f"Placeholder esperado ausente del output: {p}",
        )
        for p in missing
    ]


def check_docx_structure(
    input_path: Path, output_path: Path
) -> List[ValidationIssue]:
    """Verifica que el DOCX de salida tiene la misma estructura ZIP."""
    issues: List[ValidationIssue] = []
    try:
        with zipfile.ZipFile(input_path) as zin, zipfile.ZipFile(output_path) as zout:
            in_names = set(zin.namelist())
            out_names = set(zout.namelist())
    except zipfile.BadZipFile as exc:
        return [
            ValidationIssue(
                code="DOCX_CORRUPT",
                severity="blocker",
                message=f"DOCX inválido o corrupto: {exc}",
            )
        ]
    missing = in_names - out_names
    if missing:
        issues.append(
            ValidationIssue(
                code="DOCX_MISSING_PARTS",
                severity="blocker",
                message=f"Faltan parts en el DOCX de salida: {sorted(missing)}",
            )
        )
    extra = out_names - in_names
    if extra:
        issues.append(
            ValidationIssue(
                code="DOCX_EXTRA_PARTS",
                severity="warning",
                message=f"Parts extra en el DOCX de salida: {sorted(extra)}",
            )
        )
    return issues


def validate_text_output(
    input_text: str,
    output_text: str,
    *,
    expected_placeholders: Sequence[str] = (),
    min_ratio: float = DEFAULT_LENGTH_RATIO_MIN,
    max_ratio: float = DEFAULT_LENGTH_RATIO_MAX,
) -> ValidationReport:
    """Corre todos los chequeos a nivel texto sobre el output."""
    report = ValidationReport(passed=True)
    for issue in check_regex_leak(output_text):
        report.add(issue)
    for issue in check_length_ratio(
        input_text, output_text, min_ratio=min_ratio, max_ratio=max_ratio
    ):
        report.add(issue)
    for issue in check_placeholders_present(output_text, expected_placeholders):
        report.add(issue)
    return report


def validate_docx_output(
    input_path: Path,
    output_path: Path,
    *,
    expected_placeholders: Sequence[str] = (),
    min_ratio: float = DEFAULT_LENGTH_RATIO_MIN,
    max_ratio: float = DEFAULT_LENGTH_RATIO_MAX,
) -> ValidationReport:
    """Corre todos los chequeos sobre un DOCX anonimizado.

    Re-extrae texto del DOCX de salida (no confiamos en lo que dijo el
    pipeline — leemos el archivo final).
    """
    report = ValidationReport(passed=True)

    structure_issues = check_docx_structure(input_path, output_path)
    for issue in structure_issues:
        report.add(issue)
    if not report.passed:
        return report  # si la estructura está rota, no tiene sentido seguir

    in_doc = extract_runs(input_path)
    out_doc = extract_runs(output_path)

    text_report = validate_text_output(
        in_doc.full_text,
        out_doc.full_text,
        expected_placeholders=expected_placeholders,
        min_ratio=min_ratio,
        max_ratio=max_ratio,
    )
    for issue in text_report.issues:
        report.add(issue)
    return report
