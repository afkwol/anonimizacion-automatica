# Plan de PRs — Anonimizador 2.0

Refactor hacia el enfoque **B + C + capa determinista**: detección de spans con regex + NER + LLM clasificador con taxonomía cerrada de roles, reemplazo determinista, y manejo de archivos al estilo skill (unpack XML / PyMuPDF + OCR).

Cada PR es atómico, reviewable, y deja la app funcionando. Marcar `[x]` al completar. Usar el bloque **Comentarios** de cada PR para notas, decisiones, links a commits, o cosas que quedaron pendientes.

---

## PR 0 — Saneamiento del repo y baseline

- [ ] `.gitignore` definitivo (`.venv/`, `__pycache__/`, `logs/`, `*.pyc`, salidas `_anonimizado.*`, `_comparacion.*`)
- [ ] Mover `anonimizador v.5.py` → `app/legacy_monolith.py` (preservar como referencia, no se borra todavía)
- [ ] Crear estructura de paquetes: `app/`, `app/io/`, `app/pipeline/`, `app/detect/`, `app/classify/`, `app/replace/`, `app/validate/`, `tests/`, `fixtures/`
- [ ] `pyproject.toml` con dependencias pinneadas; eliminar `requirements.txt` o regenerarlo desde acá
- [ ] Smoke test: el monolito legacy sigue corriendo end-to-end con un fixture
- [ ] Tag `v0.legacy-baseline` para poder comparar regresiones

**Comentarios:**
> _vacío_

---

## PR 1 — Capa de I/O: extracción robusta DOCX

Reemplazar `python-docx .paragraphs` por un walker XML que ve **todo** el contenido (tablas, headers, footers, footnotes, text boxes).

- [ ] `app/io/soffice.py`: wrapper LibreOffice headless (`.doc` → `.docx`)
- [ ] `app/io/docx_unpack.py`: unpack DOCX a directorio temporal (ZIP → XMLs)
- [ ] `app/io/docx_walk.py`: itera todos los `<w:t>` con `(xml_path, element_id, offset_in_run, text)`
- [ ] Modelo de datos `TextRun(file, xpath, run_id, char_start, char_end, text)`
- [ ] Función `extract_runs(docx_path) -> list[TextRun]` cubre paragraphs + tablas + headers/footers + footnotes
- [ ] Test sobre los 3 fixtures de `ejemplos/`: el texto extraído incluye carátula, firmas y tablas
- [ ] Comparar cobertura vs `python-docx` (debe ser estrictamente mayor o igual)

**Comentarios:**
> _vacío_

---

## PR 2 — Capa de I/O: extracción robusta PDF + OCR fallback

- [ ] `app/io/pdf_extract.py` con PyMuPDF (`fitz`): extrae texto **con bounding boxes** por span
- [ ] `app/io/pdf_ocr.py`: fallback con `ocrmypdf` cuando la página no tiene capa de texto
- [ ] Detección automática "¿necesito OCR?" por página (umbral de caracteres extraídos)
- [ ] Modelo de datos `PdfSpan(page, bbox, char_start, char_end, text)`
- [ ] Test con un PDF nativo y un PDF escaneado (agregar a `fixtures/`)

**Comentarios:**
> _vacío_

---

## PR 3 — Segmentación semántica de oraciones

- [ ] Reemplazar `re.compile(r"\S+\s*")` por `pysbd` (segmenter español)
- [ ] `app/pipeline/segment.py`: `segment_sentences(text) -> list[Sentence]` con offsets exactos en el texto original
- [ ] `app/pipeline/chunk.py`: empaqueta oraciones en chunks usando el **tokenizer real** del modelo (HuggingFace tokenizer del modelo cargado en LM Studio, fallback `tiktoken cl100k`)
- [ ] Regla dura: **una oración no se parte salvo que exceda sola el presupuesto**
- [ ] Si excede: cortar por `;`, luego `:`, luego `,`, jamás a mitad de palabra
- [ ] Tests: oraciones nunca cortadas en límites de entidad; un doc largo produce N chunks coherentes
- [ ] Borrar la prohibición de overlap del legacy (queda como parámetro opcional)

**Comentarios:**
> _vacío_

---

## PR 4 — Detección Capa 1: regex deterministas (identificadores)

- [ ] `app/detect/regex_detectors.py` con detectores para:
  - DNI argentino (`\d{1,2}\.?\d{3}\.?\d{3}` con validación de formato)
  - CUIT/CUIL (con dígito verificador)
  - CBU (22 dígitos con validación de checksum)
  - Email (RFC-lite)
  - Teléfono argentino (fijo, móvil, con/sin código de área)
  - Patente automotor (formato viejo y Mercosur)
  - Pasaporte
- [ ] Cada detector retorna `Span(start, end, type, value, confidence=1.0, source="regex")`
- [ ] Tests con casos positivos y negativos por cada detector (no más falsos positivos sobre números de expediente)
- [ ] **Decisión clave:** estos spans son inmutables y no pasan por LLM; van directo a reemplazo

**Comentarios:**
> _vacío_

---

## PR 5 — Detección Capa 2: NER en español

- [ ] Evaluar `spaCy es_core_news_lg` vs Presidio con recognizers ES — elegir uno (o ambos en cascada)
- [ ] `app/detect/ner.py`: `detect_entities(text) -> list[Span]` con tipos `PER`, `LOC`, `ORG`
- [ ] Resolución de solapamientos entre spans regex y NER (regex gana siempre)
- [ ] Tests sobre los 3 fixtures: cobertura mínima de personas conocidas
- [ ] Métrica: precision/recall vs los `_anonimizado.txt` actuales como ground truth aproximado

**Comentarios:**
> _vacío_

---

## PR 6 — Heurística estructural (zonas del documento)

Pre-clasifica zonas obvias antes de invocar al LLM, para reducir costo y mejorar precisión.

- [ ] `app/detect/structure.py`: detecta zonas
  - **Carátula / encabezado del juzgado** (primeros N párrafos, patrones "Juzgado…", "Expte. N°…")
  - **Bloque de firma final** (últimos N párrafos, patrones "Es copia", "Firmado:", "Dr./Dra. … Juez/a/Secretario/a")
  - **Citas doctrinarias** (texto en cursiva o entre comillas tipográficas seguido de coma + año)
  - **Citas de jurisprudencia** ("CSJN, Fallos…", "CNCiv. Sala…")
  - **Notas al pie** (en DOCX vienen marcadas, en PDF detectables por tamaño de fuente)
- [ ] Cada zona pre-asigna un rol por defecto a las entidades adentro
- [ ] Tests: en los 3 fixtures las zonas detectadas son razonables

**Comentarios:**
> _vacío_

---

## PR 7 — Clasificador LLM con taxonomía cerrada (Capa B+C)

El corazón del cambio. El LLM **nunca reescribe texto**; solo clasifica fichas de entidades.

- [ ] `app/classify/taxonomy.py`: enum cerrado de roles
  ```
  PARTE_ACTORA, PARTE_DEMANDADA, TERCERO_CITADO,
  TESTIGO, VICTIMA, MENOR, FAMILIAR_DE_PARTE,
  LETRADO_PATROCINANTE, LETRADO_APODERADO,
  JUEZ, FISCAL, DEFENSOR_OFICIAL, SECRETARIO,
  PERITO_OFICIAL, PERITO_DE_PARTE,
  AUTOR_DOCTRINA, AUTOR_JURISPRUDENCIA,
  FUNCIONARIO_PUBLICO, ENTIDAD_PUBLICA, ENTIDAD_PRIVADA,
  DESCONOCIDO
  ```
- [ ] Mapping `role → anonimizar (bool)` configurable en `config.yaml` (defaults razonables: jueces/fiscales/defensores/peritos oficiales/autores citados/funcionarios = no; todo lo demás = sí)
- [ ] `app/classify/ficha.py`: construye ficha `{texto, contexto_izq (±200 chars), contexto_der, zona_estructural, tipo_ner}`
- [ ] `app/classify/llm_classifier.py`: prompt con taxonomía + 8-10 few-shots; recibe **batch** de fichas, devuelve JSON con `{id, rol, anonimizar, confianza}`
- [ ] Validación dura del JSON de salida (Pydantic): si el LLM devuelve un rol fuera de la taxonomía → `DESCONOCIDO` + warning
- [ ] Determinismo: `temperature=0`, `top_p=1`, `top_k=1`, formato JSON forzado
- [ ] Tests con fichas mockeadas y con fichas reales contra LM Studio
- [ ] Tests de robustez: el LLM nunca puede hacer que se pierda texto del documento (es estructuralmente imposible en este enfoque, pero hay que aseverarlo)

**Comentarios:**
> _vacío_

---

## PR 8 — Coreferencia y consistencia entre menciones

- [ ] `app/classify/coreference.py`: agrupa menciones de la misma entidad por apellido normalizado (lowercase + sin acentos + sin títulos "Dr./Dra./Sr./Sra.")
- [ ] Política de consistencia: si dos menciones del mismo apellido reciben roles distintos del LLM, usar el rol de mayor confianza y loggear
- [ ] Asignación de placeholders **estables** y **únicos por entidad**: `[ACTOR_1]`, `[ACTOR_2]`, `[TESTIGO_1]`, etc.
- [ ] Persistir el mapping `entidad → placeholder` en el log de auditoría
- [ ] Tests: el mismo nombre repetido N veces siempre recibe el mismo placeholder

**Comentarios:**
> _vacío_

---

## PR 9 — Reemplazo determinista sobre el documento original

- [ ] `app/replace/text_replacer.py`: aplica spans sobre `str` plano (para texto crudo y debug)
- [ ] `app/replace/docx_replacer.py`: aplica spans sobre la lista de `TextRun` del PR 1, **preservando formato** (rPr, bold, fuentes, numeración). Repack a `.docx` editable.
- [ ] `app/replace/pdf_replacer.py`: aplica redacción visual con PyMuPDF (`page.add_redact_annot` + `page.apply_redactions`) sobre los bounding boxes del PR 2. Salida es PDF con cuadros negros reales (no se puede copiar el texto debajo).
- [ ] La salida ya **no es `.txt`**: es del mismo formato que el input
- [ ] Tests: roundtrip docx → anonimizar → docx abre en Word sin warnings; PDF redactado no permite copiar texto subyacente

**Comentarios:**
> _vacío_

---

## PR 10 — Validación post-hoc bloqueante

- [ ] `app/validate/post_checks.py`: re-escanea el documento de salida con los **regex del PR 4**. Si encuentra algo → **fail-closed**, no se entrega el archivo.
- [ ] Chequeo de longitud: ratio out/in dentro de `[0.85, 1.05]` (ahora es bajo porque borramos PII, pero no debe colapsar el doc)
- [ ] Chequeo de integridad estructural: en DOCX, número de párrafos/tablas/celdas idéntico al original
- [ ] Chequeo de placeholders: todo span marcado para anonimizar tiene su placeholder en el output
- [ ] Si cualquier check falla: el archivo final se nombra `*_FAILED.docx` y se genera reporte detallado
- [ ] Tests: inyectar fallas y verificar que el gate las atrapa

**Comentarios:**
> _vacío_

---

## PR 11 — Pipeline orquestador y CLI

- [ ] `app/pipeline/run.py`: orquesta extract → segment → detect (regex + NER + structure) → classify (LLM) → coref → replace → validate
- [ ] CLI `python -m app <archivo>` con flags `--config`, `--debug`, `--dry-run` (solo detección, no reemplazo)
- [ ] Logging estructurado por etapa, con métricas por fase
- [ ] El JSONL de auditoría incluye: spans detectados, fichas clasificadas, mapping de coreferencia, placeholders asignados, resultado de cada gate de validación

**Comentarios:**
> _vacío_

---

## PR 12 — GUI nueva (o adaptación de la existente)

- [ ] Repensar pestañas:
  - **Procesamiento**: como ahora pero con barra de progreso por fase (extract / detect / classify / replace / validate)
  - **Revisión**: tabla con todas las entidades detectadas, su rol asignado, su placeholder, y un toggle para sobrescribir manualmente antes del reemplazo final
  - **Configuración**: incluye el mapping `rol → anonimizar (bool)`
  - **Auditoría**: visor del JSONL con filtros
- [ ] La pestaña **Revisión** es el cambio más importante: el abogado revisa antes de aceptar
- [ ] Tests manuales sobre los 3 fixtures

**Comentarios:**
> _vacío_

---

## PR 13 — Doctrina y jurisprudencia: detector dedicado

Las citas son la fuente más común de falsos positivos en anonimización legal.

- [ ] `app/detect/citations.py`: detector de
  - Fallos CSJN (`"CSJN, Fallos …"`, `"Fallos: 123:456"`)
  - Cámara/Sala (`"CNCiv. Sala A, …"`)
  - Doctrina (`"AUTOR, *Obra*, Editorial, año, p. NN"`)
- [ ] Las entidades dentro de una cita reciben automáticamente rol `AUTOR_JURISPRUDENCIA` o `AUTOR_DOCTRINA` y no se anonimizan
- [ ] Tests: en `ejemplos/` ningún autor citado queda anonimizado

**Comentarios:**
> _vacío_

---

## PR 14 — Evaluación cuantitativa y golden tests

- [ ] `tests/golden/`: para cada fixture, un JSON con las entidades esperadas y su rol esperado (curado a mano)
- [ ] `tests/test_pipeline_e2e.py`: corre el pipeline completo y compara contra los golden
- [ ] Métricas reportadas: precision/recall por rol, % de identificadores numéricos atrapados, tiempo total
- [ ] Umbral mínimo para merge: recall ≥ 0.98 sobre identificadores numéricos, ≥ 0.90 sobre personas

**Comentarios:**
> _vacío_

---

## PR 15 — Limpieza final

- [ ] Borrar `app/legacy_monolith.py`
- [ ] Actualizar `README.md` con la arquitectura nueva
- [ ] Documento `ARCHITECTURE.md` con diagrama de capas
- [ ] Tag `v1.0`

**Comentarios:**
> _vacío_

---

## Notas globales

- **Orden sugerido de merge:** 0 → 1 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 2 → 13 → 14 → 12 → 15. (PR 2 = PDF se puede diferir hasta tener DOCX sólido; PR 12 = GUI al final porque depende de todo lo demás).
- **Política fail-closed en todo el pipeline:** ante cualquier duda, no se entrega el archivo. La precisión vale más que la disponibilidad en este dominio.
- **Auditabilidad:** cada decisión de anonimización debe ser trazable en el log a (span detectado, fuente, contexto, rol asignado, confianza, placeholder).
- **No usar el LLM para reescribir texto. Nunca.** El LLM solo clasifica fichas. Esta regla es la garantía estructural contra alucinaciones.
