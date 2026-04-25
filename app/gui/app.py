"""GUI Tkinter para el anonimizador (PR 12).

**Diseño**: 4 pestañas, todo en una ventana, threading básico para que
el pipeline no congele la UI.

1. **Procesamiento**: file picker + Run, log en vivo, métricas por etapa.
2. **Revisión**: tabla de spans/clusters detectados con su rol y un
   toggle "anonimizar" editable. El abogado revisa antes de aplicar.
3. **Configuración**: tabla `Role → anonimizar (bool)` editable.
4. **Auditoría**: visor del audit JSON del último run.

**Flujo**: el run inicial es "detect-only" (dry_run=True). Cuando el
usuario revisa y aprueba, se aplica el reemplazo final con las
overrides desde Revisión.

Stdlib only (Tkinter). No dependencias nuevas.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

from app.classify.lm_client import LMStudioConfig
from app.classify.taxonomy import DEFAULT_ANONYMIZE_POLICY, AnonymizationPolicy, Role
from app.detect.span import Span
from app.pipeline.run import PipelineConfig, PipelineResult, run_pipeline, write_audit_log


logger = logging.getLogger(__name__)


# ============================ helper de logging =========================
class _QueueHandler(logging.Handler):
    """Handler que empuja LogRecords a una `queue.Queue` para consumirlos
    desde el hilo de la UI sin bloquear."""

    def __init__(self, q: "queue.Queue[str]") -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except queue.Full:
            pass


# ============================ aplicación =================================
class AnonimizadorApp(tk.Tk):
    POLL_MS = 200

    def __init__(self) -> None:
        super().__init__()
        self.title("Anonimizador 2.0")
        self.geometry("1100x750")

        # Estado
        self.input_path: Optional[Path] = None
        self.last_result: Optional[PipelineResult] = None
        # Override por ficha_id: True = anonimizar, False = preservar
        self.user_overrides: Dict[int, bool] = {}
        # Política configurable (overrides vacíos = usa los defaults)
        self.policy = AnonymizationPolicy()

        self._log_queue: queue.Queue[str] = queue.Queue(maxsize=2000)
        self._setup_logging()

        self._build_ui()
        self.after(self.POLL_MS, self._drain_log_queue)

    # --- Logging puente ---
    def _setup_logging(self) -> None:
        root = logging.getLogger()
        if root.level > logging.INFO:
            root.setLevel(logging.INFO)
        handler = _QueueHandler(self._log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        root.addHandler(handler)
        self._log_handler = handler

    # --- Construcción UI ---
    def _build_ui(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_proc = ttk.Frame(nb)
        self.tab_review = ttk.Frame(nb)
        self.tab_config = ttk.Frame(nb)
        self.tab_audit = ttk.Frame(nb)
        nb.add(self.tab_proc, text="Procesamiento")
        nb.add(self.tab_review, text="Revisión")
        nb.add(self.tab_config, text="Configuración")
        nb.add(self.tab_audit, text="Auditoría")

        self._build_processing_tab()
        self._build_review_tab()
        self._build_config_tab()
        self._build_audit_tab()

    # ---------------------- Pestaña Procesamiento ----------------------
    def _build_processing_tab(self) -> None:
        f = self.tab_proc
        f.columnconfigure(0, weight=1)

        # File picker
        top = ttk.LabelFrame(f, text="Archivo .docx")
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Ruta:").grid(row=0, column=0, padx=6, pady=6, sticky="w")
        self.path_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.path_var, state="readonly").grid(
            row=0, column=1, padx=6, pady=6, sticky="ew"
        )
        ttk.Button(top, text="Examinar…", command=self._on_browse).grid(
            row=0, column=2, padx=6, pady=6
        )

        # Acciones
        actions = ttk.LabelFrame(f, text="Ejecución")
        actions.grid(row=1, column=0, sticky="ew", padx=10, pady=10)

        self.btn_detect = ttk.Button(
            actions, text="1. Detectar (dry-run)", command=self._on_detect
        )
        self.btn_detect.grid(row=0, column=0, padx=6, pady=6)

        self.btn_apply = ttk.Button(
            actions,
            text="2. Aplicar y validar",
            command=self._on_apply,
            state="disabled",
        )
        self.btn_apply.grid(row=0, column=1, padx=6, pady=6)

        self.status_var = tk.StringVar(value="Listo.")
        ttk.Label(actions, textvariable=self.status_var).grid(
            row=1, column=0, columnspan=3, padx=6, pady=6, sticky="w"
        )

        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=300)
        self.progress.grid(row=2, column=0, columnspan=3, padx=6, pady=6, sticky="ew")

        # Log en vivo
        log_frame = ttk.LabelFrame(f, text="Log")
        log_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=10)
        f.rowconfigure(2, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=15, wrap="none", font=("Consolas", 9))
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=sb.set)

    # ---------------------- Pestaña Revisión ----------------------
    def _build_review_tab(self) -> None:
        f = self.tab_review
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        info = ttk.Label(
            f,
            text=(
                "Revisá los spans detectados. Hacé doble clic sobre la columna "
                "'Anonimizar' para alternar. Cuando termines, volvé a "
                "Procesamiento y aplicá."
            ),
            wraplength=1000,
        )
        info.grid(row=0, column=0, sticky="ew", padx=10, pady=8)

        cols = ("id", "tipo", "fuente", "rol", "texto", "anonimizar")
        self.review_tree = ttk.Treeview(f, columns=cols, show="headings", height=25)
        for c, w in zip(cols, (50, 80, 90, 200, 360, 100)):
            self.review_tree.heading(c, text=c.upper())
            self.review_tree.column(c, width=w, anchor="w")
        self.review_tree.grid(row=1, column=0, sticky="nsew", padx=10, pady=8)

        sb = ttk.Scrollbar(f, orient="vertical", command=self.review_tree.yview)
        sb.grid(row=1, column=1, sticky="ns", pady=8)
        self.review_tree.configure(yscrollcommand=sb.set)

        self.review_tree.bind("<Double-1>", self._on_review_double_click)

    def _on_review_double_click(self, event: tk.Event) -> None:
        item = self.review_tree.identify_row(event.y)
        col = self.review_tree.identify_column(event.x)
        if not item or col != "#6":  # columna 'anonimizar'
            return
        values = list(self.review_tree.item(item, "values"))
        ficha_id = int(values[0])
        current = values[5] == "Sí"
        new = not current
        self.user_overrides[ficha_id] = new
        values[5] = "Sí" if new else "No"
        self.review_tree.item(item, values=values)

    def _refresh_review(self, result: PipelineResult) -> None:
        self.review_tree.delete(*self.review_tree.get_children())
        # Spans regex (siempre anonimizar)
        for span in result.spans:
            if span.source != "regex":
                continue
            self.review_tree.insert(
                "",
                "end",
                values=("-", span.type, "regex", "(determinista)", span.text[:80], "Sí"),
            )
        # Clasificaciones LLM
        for r in result.classifications:
            anon = self.user_overrides.get(r.ficha.id, r.anonymize)
            self.review_tree.insert(
                "",
                "end",
                values=(
                    r.ficha.id,
                    r.ficha.span.type,
                    r.source,
                    r.role.value,
                    r.ficha.text[:80],
                    "Sí" if anon else "No",
                ),
            )

    # ---------------------- Pestaña Configuración ----------------------
    def _build_config_tab(self) -> None:
        f = self.tab_config
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        ttk.Label(
            f,
            text=(
                "Política por rol. Doble clic para alternar entre "
                "Anonimizar y Preservar."
            ),
        ).grid(row=0, column=0, sticky="w", padx=10, pady=8)

        self.config_tree = ttk.Treeview(
            f, columns=("rol", "accion"), show="headings", height=20
        )
        self.config_tree.heading("rol", text="ROL")
        self.config_tree.heading("accion", text="ACCIÓN")
        self.config_tree.column("rol", width=300, anchor="w")
        self.config_tree.column("accion", width=150, anchor="w")
        self.config_tree.grid(row=1, column=0, sticky="nsew", padx=10, pady=8)
        self.config_tree.bind("<Double-1>", self._on_config_double_click)
        self._refresh_config_tree()

    def _refresh_config_tree(self) -> None:
        self.config_tree.delete(*self.config_tree.get_children())
        for role in Role:
            anon = self.policy.should_anonymize(role)
            self.config_tree.insert(
                "",
                "end",
                iid=role.value,
                values=(role.value, "Anonimizar" if anon else "Preservar"),
            )

    def _on_config_double_click(self, event: tk.Event) -> None:
        item = self.config_tree.identify_row(event.y)
        if not item:
            return
        role = Role(item)
        current = self.policy.should_anonymize(role)
        self.policy.set(role, not current)
        self._refresh_config_tree()

    # ---------------------- Pestaña Auditoría ----------------------
    def _build_audit_tab(self) -> None:
        f = self.tab_audit
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)
        self.audit_text = tk.Text(f, wrap="none", font=("Consolas", 9))
        self.audit_text.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        sb = ttk.Scrollbar(f, orient="vertical", command=self.audit_text.yview)
        sb.grid(row=0, column=1, sticky="ns", pady=10)
        self.audit_text.configure(yscrollcommand=sb.set)

    def _refresh_audit(self, result: PipelineResult) -> None:
        self.audit_text.delete("1.0", "end")
        self.audit_text.insert(
            "1.0", json.dumps(result.audit, ensure_ascii=False, indent=2)
        )

    # ---------------------- Acciones ----------------------
    def _on_browse(self) -> None:
        path = filedialog.askopenfilename(
            title="Seleccionar .docx",
            filetypes=[("Documentos Word", "*.docx"), ("Todos", "*.*")],
        )
        if path:
            self.input_path = Path(path)
            self.path_var.set(str(self.input_path))
            self.btn_apply.configure(state="disabled")
            self.user_overrides.clear()

    def _on_detect(self) -> None:
        if not self.input_path or not self.input_path.exists():
            messagebox.showwarning("Falta archivo", "Seleccioná un .docx primero.")
            return
        self._run_in_thread(dry_run=True)

    def _on_apply(self) -> None:
        if not self.input_path or not self.last_result:
            messagebox.showwarning("Sin detección", "Corré primero la detección.")
            return
        self._run_in_thread(dry_run=False)

    def _run_in_thread(self, *, dry_run: bool) -> None:
        self.btn_detect.configure(state="disabled")
        self.btn_apply.configure(state="disabled")
        self.progress.start(10)
        self.status_var.set("Detectando..." if dry_run else "Aplicando...")

        def worker() -> None:
            try:
                config = PipelineConfig(
                    use_ner=True,
                    llm_config=LMStudioConfig(),
                    policy=self.policy,
                    dry_run=dry_run,
                )
                result = run_pipeline(self.input_path, config)
                # Aplicar overrides del usuario sobre las clasificaciones.
                # En dry_run no hay efecto; en el segundo run, esto requiere
                # que el usuario haya tocado la pestaña Revisión y luego
                # aplicado: sus overrides ya están en self.user_overrides.
                # Si hay overrides para una ficha, sobreescribimos
                # `anonymize` antes de evaluar el output.
                if not dry_run and self.user_overrides:
                    for r in result.classifications:
                        if r.ficha.id in self.user_overrides:
                            r.anonymize = self.user_overrides[r.ficha.id]
                self.after(0, self._on_done, result, dry_run, None)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Pipeline falló")
                self.after(0, self._on_done, None, dry_run, exc)

        threading.Thread(target=worker, daemon=True).start()

    def _on_done(
        self,
        result: Optional[PipelineResult],
        dry_run: bool,
        error: Optional[Exception],
    ) -> None:
        self.progress.stop()
        self.btn_detect.configure(state="normal")
        if error is not None:
            self.status_var.set(f"Error: {error}")
            messagebox.showerror("Error", str(error))
            return
        assert result is not None
        self.last_result = result

        # Persistir audit JSON al lado del input
        try:
            audit_path = self.input_path.with_name(self.input_path.stem + "_audit.json")
            write_audit_log(result, audit_path)
        except Exception:  # noqa: BLE001
            logger.warning("No pude escribir el audit JSON", exc_info=True)

        self._refresh_review(result)
        self._refresh_audit(result)

        if dry_run:
            self.btn_apply.configure(state="normal")
            self.status_var.set(
                f"Detección OK. {len(result.spans)} spans, "
                f"{len(result.classifications)} clasificaciones. "
                "Revisá la pestaña Revisión y aplicá."
            )
        else:
            ok = result.success
            if ok:
                self.status_var.set(
                    f"OK ✓  Output: {result.output_path.name}"
                )
                messagebox.showinfo("Listo", f"Archivo generado:\n{result.output_path}")
            else:
                blockers = result.validation.blockers if result.validation else []
                self.status_var.set(
                    f"Bloqueado por validacion: {len(blockers)} observaciones. Ver pestaña Auditoria."
                )
                messagebox.showerror(
                    "Validacion bloqueada",
                    "El pipeline detecto observaciones y bloqueo la salida final. El archivo se guardo como "
                    f"{result.output_path.name}. Revisá la pestaña Auditoría.",
                )

    # ---------------------- Log queue drain ----------------------
    def _drain_log_queue(self) -> None:
        try:
            while True:
                line = self._log_queue.get_nowait()
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
        except queue.Empty:
            pass
        self.after(self.POLL_MS, self._drain_log_queue)


def main() -> None:
    app = AnonimizadorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
