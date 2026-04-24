"""Dashboard de revisión humano-en-el-loop.

Sirve UI local para comparar PDFs original vs anonimizado, con filtros
por estado y persistencia de aprobaciones.

Uso:
    python -m app review laboratorio/tanda2
    python -m app review laboratorio/tanda2 --port 5555 --no-open

Estado se guarda en `<folder>/_review_state.json`. Imágenes renderizadas
se cachean en `<folder>/_review_cache/<stem>/orig_NNN.png` y `anon_NNN.png`.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import fitz
from flask import Flask, abort, jsonify, render_template, request, send_file

logger = logging.getLogger(__name__)

STATUSES = ("pending", "approved", "rejected", "needs_fix")

# Etiquetas visibles en castellano. El código interno se mantiene en inglés
# para estabilidad del archivo de estado.
STATUS_LABELS = {
    "pending": "Pendiente",
    "approved": "Aprobado",
    "rejected": "Rechazado",
    "needs_fix": "Necesita corrección",
}


@dataclass
class DocEntry:
    stem: str
    n_pages: int
    n_replacements: int
    n_parties: int
    has_warnings: bool
    warnings: List[str]
    status: str  # del state file
    parties: list  # nombres detectados
    is_problematic: bool  # warnings o 0 reemplazos


def _load_state(folder: Path) -> dict:
    p = folder / "_review_state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(folder: Path, state: dict) -> None:
    (folder / "_review_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _scan_folder(folder: Path) -> List[DocEntry]:
    """Lista todos los pares orig/anon con datos del audit y estado guardado."""
    state = _load_state(folder)
    entries: List[DocEntry] = []
    for orig in sorted(folder.glob("*.pdf")):
        if orig.stem.endswith("_anonimizado"):
            continue
        audit_p = orig.with_name(orig.stem + "_audit.json")
        anon_p = orig.with_name(orig.stem + "_anonimizado.pdf")
        if not audit_p.exists():
            continue
        try:
            audit = json.loads(audit_p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        warnings = audit.get("warnings") or []
        n_repl = audit.get("n_replacements", 0)
        is_problematic = bool(warnings) or n_repl == 0 or not anon_p.exists()
        entries.append(DocEntry(
            stem=orig.stem,
            n_pages=audit.get("n_pages", 0),
            n_replacements=n_repl,
            n_parties=len(audit.get("parties", [])),
            has_warnings=bool(warnings),
            warnings=warnings,
            status=state.get(orig.stem, {}).get("status", "pending"),
            parties=audit.get("parties", []),
            is_problematic=is_problematic,
        ))
    return entries


def _ensure_renders(folder: Path, stem: str, dpi: int = 100) -> Path:
    """Renderiza páginas orig+anon a PNG si no están en cache."""
    cache_dir = folder / "_review_cache" / stem
    cache_dir.mkdir(parents=True, exist_ok=True)
    for kind, suffix in [("orig", ".pdf"), ("anon", "_anonimizado.pdf")]:
        pdf_path = folder / f"{stem}{suffix}"
        if not pdf_path.exists():
            continue
        # Marker para saber si ya renderizamos
        marker = cache_dir / f".{kind}_done"
        if marker.exists():
            continue
        with fitz.open(str(pdf_path)) as d:
            for i, page in enumerate(d):
                target = cache_dir / f"{kind}_{i:03d}.png"
                if not target.exists():
                    pix = page.get_pixmap(dpi=dpi)
                    pix.save(str(target))
        marker.touch()
    return cache_dir


def create_app(folder: Path) -> Flask:
    folder = folder.resolve()
    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
    app.config["FOLDER"] = folder
    # Expone los labels a todos los templates sin tener que pasarlos en cada render.
    app.jinja_env.globals["STATUS_LABELS"] = STATUS_LABELS

    @app.route("/")
    def index():
        entries = _scan_folder(folder)
        filt = request.args.get("filter", "problematic")  # problematic|all|pending|approved|rejected
        if filt == "problematic":
            shown = [e for e in entries if e.is_problematic]
        elif filt in STATUSES:
            shown = [e for e in entries if e.status == filt]
        else:
            shown = entries
        counts = {
            "all": len(entries),
            "problematic": sum(1 for e in entries if e.is_problematic),
            "pending": sum(1 for e in entries if e.status == "pending"),
            "approved": sum(1 for e in entries if e.status == "approved"),
            "rejected": sum(1 for e in entries if e.status == "rejected"),
            "needs_fix": sum(1 for e in entries if e.status == "needs_fix"),
        }
        return render_template("index.html", entries=shown, filt=filt,
                               counts=counts, folder=str(folder))

    @app.route("/doc/<path:stem>")
    def doc_view(stem: str):
        entries = _scan_folder(folder)
        entry = next((e for e in entries if e.stem == stem), None)
        if entry is None:
            abort(404)
        # Render lazy: solo cuando se abre el doc.
        _ensure_renders(folder, stem)
        # Determinar páginas disponibles y cuáles tienen anonimizado.
        cache_dir = folder / "_review_cache" / stem
        orig_pages = sorted({int(p.stem.split("_")[1])
                             for p in cache_dir.glob("orig_*.png")})
        anon_exists = (folder / f"{stem}_anonimizado.pdf").exists()
        return render_template("doc.html", entry=entry, pages=orig_pages,
                               anon_exists=anon_exists, folder=str(folder))

    @app.route("/img/<path:stem>/<kind>/<page>.png")
    def img(stem: str, kind: str, page: str):
        if kind not in ("orig", "anon"):
            abort(400)
        try:
            page_idx = int(page)
        except ValueError:
            abort(400)
        p = folder / "_review_cache" / stem / f"{kind}_{page_idx:03d}.png"
        if not p.exists():
            abort(404)
        return send_file(str(p), mimetype="image/png")

    @app.route("/api/status", methods=["POST"])
    def set_status():
        data = request.get_json() or {}
        stem = data.get("stem")
        status = data.get("status")
        note = data.get("note", "")
        if not stem or status not in STATUSES:
            return jsonify({"error": "stem y status válidos requeridos"}), 400
        state = _load_state(folder)
        state[stem] = {"status": status, "note": note}
        _save_state(folder, state)
        return jsonify({"ok": True, "status": status})

    @app.route("/api/state")
    def get_state():
        return jsonify(_load_state(folder))

    return app


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="review", description="Dashboard de revisión")
    ap.add_argument("folder", type=Path, help="Carpeta con PDFs y audits")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true", help="No abrir browser")
    args = ap.parse_args(argv)

    if not args.folder.is_dir():
        print(f"ERROR: no es directorio: {args.folder}", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    app = create_app(args.folder)
    url = f"http://{args.host}:{args.port}/"
    print(f"\n  Dashboard de revisión: {url}")
    print(f"  Carpeta: {args.folder.resolve()}")
    print(f"  Estado se guarda en: {args.folder.resolve() / '_review_state.json'}\n")
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
