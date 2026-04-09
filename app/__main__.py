"""CLI: `python -m app <archivo.docx>`.

Flags:
  --dry-run        Sólo detección, no escribe el archivo de salida.
  --no-ner         Saltea NER (sólo regex + LLM).
  --debug          Loglevel DEBUG.
  --audit PATH     Path del audit JSON (default: <input>_audit.json).
  --base-url URL   URL del servidor LM Studio.
  --model NAME     Modelo del LLM.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from app.classify.lm_client import LMStudioConfig
from app.pipeline.run import PipelineConfig, run_pipeline, write_audit_log


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="anonimizador",
        description="Anonimizador determinista de documentos judiciales (B+C).",
    )
    p.add_argument("input", type=Path, nargs="?", help="Archivo .docx de entrada")
    p.add_argument("--gui", action="store_true", help="Lanzar la GUI Tkinter")
    p.add_argument("--dry-run", action="store_true", help="No escribir output")
    p.add_argument("--no-ner", action="store_true", help="Saltear NER")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--audit", type=Path, default=None, help="Path del audit JSON")
    p.add_argument("--base-url", default=None, help="URL LM Studio")
    p.add_argument("--model", default=None, help="Modelo LLM")
    p.add_argument("--batch-size", type=int, default=10)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
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

    config = PipelineConfig(
        use_ner=not args.no_ner,
        llm_config=llm_cfg,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )

    result = run_pipeline(args.input, config)

    audit_path = args.audit or args.input.with_name(args.input.stem + "_audit.json")
    write_audit_log(result, audit_path)

    print(f"\n=== Pipeline {'OK' if result.success else 'FAILED'} ===")
    print(f"Input:  {result.input_path}")
    if result.output_path:
        print(f"Output: {result.output_path}")
    print(f"Audit:  {audit_path}")
    print(f"Spans: {len(result.spans)} | Clusters: {len(result.coreference.clusters) if result.coreference else 0} | Reemplazos: {len(result.replacements)}")
    for m in result.metrics:
        print(f"  - {m.name:20s} {m.duration_s*1000:8.1f} ms  n={m.n_items}")
    if result.validation and not result.validation.passed:
        print("\nBLOCKERS:")
        for issue in result.validation.blockers:
            print(f"  [{issue.code}] {issue.message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
