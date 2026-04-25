"""Agregación de métricas sobre una carpeta de audits.

Lee todos los `*_audit.json` y calcula estadísticas para detectar
regresiones (caídas en tasa de detección, modelo cambiado, outliers).

Uso:
    python -m app metrics laboratorio/tanda2
    python -m app metrics laboratorio/tanda2 --csv salida.csv
    python -m app metrics laboratorio/tanda2 --outliers   # solo outliers
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


@dataclass
class DocMetric:
    stem: str
    n_pages: int
    text_chars: int
    n_replacements: int
    replacements_per_page: float
    replacements_per_kchar: float
    n_parties: int
    elapsed_s: float
    model: str
    has_warnings: bool
    success: bool


def load_metrics(folder: Path) -> List[DocMetric]:
    out: List[DocMetric] = []
    for audit_path in sorted(folder.glob("*_audit.json")):
        try:
            data = json.loads(audit_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        # Stem del audit es <name>_audit; queremos <name>.
        stem = audit_path.stem.removesuffix("_audit")
        out.append(DocMetric(
            stem=stem,
            n_pages=data.get("n_pages", 0),
            text_chars=data.get("text_chars", 0),
            n_replacements=data.get("n_replacements", 0),
            replacements_per_page=data.get("replacements_per_page", 0.0),
            replacements_per_kchar=data.get("replacements_per_kchar", 0.0),
            n_parties=len(data.get("parties", [])),
            elapsed_s=data.get("elapsed_s", 0.0),
            model=data.get("model", "?"),
            has_warnings=bool(data.get("warnings")),
            success=bool(data.get("success")),
        ))
    return out


def _z_score(x: float, mu: float, sigma: float) -> float:
    if sigma == 0:
        return 0.0
    return (x - mu) / sigma


def detect_outliers(metrics: List[DocMetric], z_threshold: float = 2.0) -> List[tuple[DocMetric, str]]:
    """Detecta documentos con métricas anómalas vs. el resto del lote.

    Considera outlier si replacements_per_kchar está >z_threshold desv estándar
    debajo de la mediana del lote (sólo flagea por DEBAJO porque eso indica
    sub-anonimización; sobre-anonimizar es raro y menos riesgoso).
    """
    if len(metrics) < 5:
        return []
    rates = [m.replacements_per_kchar for m in metrics
             if m.n_pages > 0 and not m.has_warnings]
    if not rates:
        return []
    mu = statistics.mean(rates)
    sigma = statistics.stdev(rates) if len(rates) > 1 else 0.0
    out = []
    for m in metrics:
        if m.has_warnings:
            out.append((m, "revision manual requerida: no fue posible identificar partes procesales"))
            continue
        if m.n_pages > 2 and m.replacements_per_kchar < mu - z_threshold * sigma:
            z = _z_score(m.replacements_per_kchar, mu, sigma)
            out.append((m, f"baja densidad ({m.replacements_per_kchar:.2f} reemp/kchar, z={z:.1f})"))
    return out


def summary(metrics: List[DocMetric]) -> dict:
    if not metrics:
        return {"total": 0}
    rates = [m.replacements_per_kchar for m in metrics if m.n_pages > 0]
    repls = [m.n_replacements for m in metrics]
    elapsed = [m.elapsed_s for m in metrics]
    models = {m.model for m in metrics}
    return {
        "total": len(metrics),
        "with_warnings": sum(1 for m in metrics if m.has_warnings),
        "zero_replacements": sum(1 for m in metrics if m.n_replacements == 0),
        "models": sorted(models),
        "avg_replacements": round(statistics.mean(repls), 1),
        "median_replacements": statistics.median(repls),
        "avg_elapsed_s": round(statistics.mean(elapsed), 2),
        "total_elapsed_s": round(sum(elapsed), 1),
        "median_repl_per_kchar": round(statistics.median(rates), 2) if rates else 0,
        "stdev_repl_per_kchar": round(statistics.stdev(rates), 2) if len(rates) > 1 else 0,
    }


def write_csv(metrics: List[DocMetric], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["stem", "n_pages", "text_chars", "n_parties",
                    "n_replacements", "repl_per_page", "repl_per_kchar",
                    "elapsed_s", "model", "has_warnings", "success"])
        for m in metrics:
            w.writerow([m.stem, m.n_pages, m.text_chars, m.n_parties,
                        m.n_replacements, m.replacements_per_page,
                        m.replacements_per_kchar, m.elapsed_s, m.model,
                        int(m.has_warnings), int(m.success)])


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="metrics", description="Agregación de audits")
    ap.add_argument("folder", type=Path, help="Carpeta con audits *_audit.json")
    ap.add_argument("--csv", type=Path, default=None, help="Exportar CSV")
    ap.add_argument("--outliers", action="store_true",
                    help="Solo mostrar outliers (densidad anómalamente baja)")
    ap.add_argument("--z", type=float, default=2.0, help="Threshold z-score (default 2.0)")
    args = ap.parse_args(argv)

    if not args.folder.is_dir():
        print(f"ERROR: no es directorio: {args.folder}")
        return 2

    metrics = load_metrics(args.folder)
    if not metrics:
        print(f"No hay audits en {args.folder}")
        return 1

    if args.csv:
        write_csv(metrics, args.csv)
        print(f"CSV escrito: {args.csv}")

    if args.outliers:
        outs = detect_outliers(metrics, z_threshold=args.z)
        print(f"\n=== Outliers en {args.folder} ({len(outs)}/{len(metrics)}) ===")
        for m, reason in outs:
            print(f"  {m.stem}")
            print(f"    {reason} | {m.n_replacements} repl en {m.n_pages}p ({m.text_chars} chars)")
        return 0

    s = summary(metrics)
    print(f"\n=== Resumen métricas: {args.folder} ===")
    print(f"  Documentos:           {s['total']}")
    print(f"  Revision manual:      {s['with_warnings']}")
    print(f"  Con 0 reemplazos:     {s['zero_replacements']}")
    print(f"  Modelos usados:       {', '.join(s['models'])}")
    print(f"  Reemplazos promedio:  {s['avg_replacements']} (mediana {s['median_replacements']})")
    print(f"  Densidad mediana:     {s['median_repl_per_kchar']} reemp/kchar (σ={s['stdev_repl_per_kchar']})")
    print(f"  Tiempo total:         {s['total_elapsed_s']}s ({s['avg_elapsed_s']}s prom)")
    outs = detect_outliers(metrics, z_threshold=args.z)
    if outs:
        print(f"\n  ⚠ {len(outs)} outliers detectados (correr con --outliers para detalle)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
