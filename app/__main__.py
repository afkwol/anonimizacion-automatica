"""CLI: `python -m app <archivo.docx|pdf>`.

Flags:
  --lite             Modo liviano (1 prompt LLM, sin NER). Para civil/laboral.
  --dry-run          Sólo detección, no escribe el archivo de salida.
  --no-ner           Saltea NER (sólo regex + LLM). Solo modo completo.
  --debug            Loglevel DEBUG.
  --audit PATH       Path del audit JSON (default: <input>_audit.json).
  --base-url URL     URL del servidor LM Studio.
  --model NAME       Modelo del LLM.
  --gui              Lanzar la GUI Tkinter.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from app.classify.lm_client import LMStudioConfig

# Forzar UTF-8 en stdout/stderr para que los prints con '→' y tildes no
# rompan cuando el proceso se lanza sin consola (Windows default cp1252).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="anonimizador",
        description="Anonimizador determinista de documentos judiciales (B+C).",
    )
    p.add_argument("input", type=Path, nargs="?", help="Archivo .docx/.pdf o carpeta con varios")
    p.add_argument("--gui", action="store_true", help="Lanzar la GUI Tkinter")
    p.add_argument("--lite", action="store_true", help="Modo liviano (civil/laboral)")
    p.add_argument("--dry-run", action="store_true", help="No escribir output")
    p.add_argument("--safe-publish", action="store_true", help="Exporta salida con nombre neutro, metadata limpia y auditoria publica")
    p.add_argument("--no-audit", action="store_true", help="No escribir archivo de auditoria")
    p.add_argument("--no-ner", action="store_true", help="Saltear NER (modo completo)")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--audit", type=Path, default=None, help="Path del audit JSON")
    p.add_argument("--base-url", default=None, help="URL LM Studio")
    p.add_argument("--model", default=None, help="Modelo LLM")
    p.add_argument("--batch-size", type=int, default=10)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    raw = argv if argv is not None else sys.argv[1:]
    # Subcomandos: metrics, review. Delegamos sin tocar el parser principal.
    if raw and raw[0] == "metrics":
        from app import metrics
        return metrics.main(raw[1:])
    if raw and raw[0] == "review":
        from app.review import server
        return server.main(raw[1:])
    args = _parse_args(raw)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.gui:
        from app.gui.app import main as gui_main

        gui_main()
        return 0

    if args.input is None:
        print("ERROR: especificá un archivo o usá --gui", file=sys.stderr)
        return 2
    if not args.input.exists():
        print(f"ERROR: no existe {args.input}", file=sys.stderr)
        return 2

    llm_cfg = LMStudioConfig()
    if args.base_url:
        llm_cfg.base_url = args.base_url
    if args.model:
        llm_cfg.model = args.model

    # Si es carpeta, procesar todos los .pdf/.docx adentro.
    if args.input.is_dir():
        import time
        from datetime import datetime

        batch_dir = args.input
        files = sorted(
            [p for p in batch_dir.iterdir()
             if p.suffix.lower() in (".pdf", ".docx")
             and not p.stem.endswith("_anonimizado")]
        )
        if not files:
            print(f"ERROR: no hay archivos .pdf/.docx en {batch_dir}", file=sys.stderr)
            return 2

        log_path = batch_dir / f"batch_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        print(f"Procesando {len(files)} archivos en {batch_dir}")
        print(f"Log: {log_path}")

        t_batch = time.time()
        n_ok = 0
        n_review = 0
        n_error = 0
        per_file: list[tuple[str, float, str]] = []  # (name, elapsed, status)

        for i, f in enumerate(files, 1):
            print(f"\n[{i}/{len(files)}] {f.name}")
            if args.no_audit:
                audit_p = None
            else:
                if args.audit:
                    audit_p = args.audit
                else:
                    if args.safe_publish:
                        from app.pipeline.safe_output import build_public_audit_path
                        audit_p = build_public_audit_path(f)
                    else:
                        audit_p = f.with_name(f.stem + "_audit.json")
            args.input = f
            t0 = time.time()
            status = "OK"
            try:
                rc = _run_lite(args, llm_cfg, audit_p) if args.lite else _run_full(args, llm_cfg, audit_p)
                if rc == 0:
                    n_ok += 1
                    status = "ANONIMIZADO_OK"
                elif rc == 2:
                    n_review += 1
                    status = f"REVISION_MANUAL(rc={rc})"
                else:
                    n_error += 1
                    status = f"ERROR_TECNICO(rc={rc})"
            except Exception as e:
                print(f"  ERROR: {e}", file=sys.stderr)
                n_error += 1
                status = f"ERROR_TECNICO({type(e).__name__}: {e})"
            per_file.append((f.name, time.time() - t0, status))

        total_elapsed = time.time() - t_batch
        avg_elapsed = total_elapsed / len(files) if files else 0.0

        lines = [
            f"Batch: {batch_dir}",
            f"Inicio:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Archivos: {len(files)}",
            f"Anonimizados OK:           {n_ok}",
            f"Revision manual requerida: {n_review}",
            f"Errores tecnicos:          {n_error}",
            f"Tiempo total:      {total_elapsed:.2f} s",
            f"Promedio por doc:  {avg_elapsed:.2f} s",
            "",
            "Detalle por archivo:",
        ]
        for name, elapsed, status in per_file:
            lines.append(f"  {elapsed:7.2f}s  {status:20s}  {name}")
        log_path.write_text("\n".join(lines), encoding="utf-8")

        print(
            f"\n=== Batch: {n_ok} anonimizados OK, "
            f"{n_review} con revision manual, "
            f"{n_error} con error tecnico — "
            f"{total_elapsed:.1f}s total, {avg_elapsed:.1f}s promedio ==="
        )
        print(f"Log guardado en: {log_path}")
        if n_error:
            return 1
        if n_review:
            return 2
        return 0

    if args.no_audit:
        audit_path = None
    elif args.audit:
        audit_path = args.audit
    else:
        if args.safe_publish:
            from app.pipeline.safe_output import build_public_audit_path
            audit_path = build_public_audit_path(args.input)
        else:
            audit_path = args.input.with_name(args.input.stem + "_audit.json")

    if args.lite:
        return _run_lite(args, llm_cfg, audit_path)
    else:
        return _run_full(args, llm_cfg, audit_path)


def _run_lite(args: argparse.Namespace, llm_cfg: LMStudioConfig, audit_path: Path | None) -> int:
    """Ejecuta el pipeline liviano."""
    from app.pipeline.run_lite import LiteConfig, run_pipeline_lite, write_audit_log

    config = LiteConfig(llm_config=llm_cfg, dry_run=args.dry_run, safe_publish=args.safe_publish)
    result = run_pipeline_lite(args.input, config)
    if audit_path is not None:
        write_audit_log(result, audit_path)
    warnings = result.audit.get("warnings", [])
    if warnings:
        state = "REVISION MANUAL REQUERIDA"
    elif result.success:
        state = "ANONIMIZACION COMPLETADA"
    else:
        state = "ERROR TECNICO"

    print(f"\n=== Pipeline LITE: {state} ({result.elapsed_s:.1f}s) ===")
    print(f"Input:  {result.input_path}")
    if result.output_path:
        print(f"Output: {result.output_path}")
    if audit_path is not None:
        print(f"Audit:  {audit_path}")
    print(f"\nPartes detectadas:")
    for p in result.audit.get("parties", []):
        src = "carátula" if p.get("from_caratula") else "LLM"
        print(f"  {p['placeholder']:15s} {p['nombre']:<35s} rol={p['rol']:<12s} src={src}")
    print(f"\nOcurrencias: {len(result.name_spans)} nombres + {len(result.regex_spans)} regex → {len(result.replacements)} reemplazos")
    for k, v in result.audit.get("timings", {}).items():
        print(f"  {k:20s} {v*1000:8.1f} ms")
    if warnings:
        print()
        for w in warnings:
            print(f"  !! {w}", file=sys.stderr)
        return 2  # exit code 2 = revision manual requerida (sin salida anonimizada)
    return 0


def _run_full(args: argparse.Namespace, llm_cfg: LMStudioConfig, audit_path: Path | None) -> int:
    """Ejecuta el pipeline completo (NER + fichas + clasificación)."""
    from app.pipeline.run import PipelineConfig, run_pipeline, write_audit_log

    config = PipelineConfig(
        use_ner=not args.no_ner,
        llm_config=llm_cfg,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        safe_publish=args.safe_publish,
    )

    result = run_pipeline(args.input, config)
    if audit_path is not None:
        write_audit_log(result, audit_path)

    state = "ANONIMIZACION COMPLETADA" if result.success else "BLOQUEADO POR VALIDACION"
    print(f"\n=== Pipeline: {state} ===")
    print(f"Input:  {result.input_path}")
    if result.output_path:
        print(f"Output: {result.output_path}")
    if audit_path is not None:
        print(f"Audit:  {audit_path}")
    print(f"Spans: {len(result.spans)} | Clusters: {len(result.coreference.clusters) if result.coreference else 0} | Reemplazos: {len(result.replacements)}")
    for m in result.metrics:
        print(f"  - {m.name:20s} {m.duration_s*1000:8.1f} ms  n={m.n_items}")
    if result.validation and not result.validation.passed:
        print("\nBLOCKERS:")
        for issue in result.validation.blockers:
            print(f"  [{issue.code}] {issue.message}")
        return 1
    if not result.success:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
