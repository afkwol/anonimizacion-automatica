# Plan de PRs — Anonimizador 2.0

Refactor hacia el enfoque **B + C + capa determinista**: detección de spans con regex + NER + LLM clasificador con taxonomía cerrada de roles, reemplazo determinista, y manejo de archivos al estilo skill (unpack XML / PyMuPDF + OCR).

Cada PR es atómico, reviewable, y deja la app funcionando. Marcar `[x]` al completar. Usar el bloque **Comentarios** de cada PR para notas, decisiones, links a commits, o cosas que quedaron pendientes.

---

## PR 0 — Saneamiento del repo y baseline ✅

- [x] `.gitignore` definitivo (`.venv/`, `__pycache__/`, `logs/`, `*.pyc`, salidas `_anonimizado.*`, `_comparacion.*`)
- [x] Mover `anonimizador v.5.py` → `app/legacy_monolith.py` (preservar como referencia, no se borra todavía)
- [x] Crear estructura de paquetes: `app/`, `app/io/`, `app/pipeline/`, `app/detect/`, `app/classify/`, `app/replace/`, `app/validate/`, `tests/`, `fixtures/`
- [x] `pyproject.toml` con dependencias pinneadas; eliminar `requirements.txt` o regenerarlo desde acá
- [x] Smoke test: el monolito legacy sigue corriendo end-to-end con un fixture
- [x] Tag `v0.legacy-baseline` para poder comparar regresiones

**Comentarios:**
> Commit `d50f31d`, tag `v0.legacy-baseline`. CONFIG_PATH del legacy fue ajustado a `parent.parent` para que siga encontrando `config.yaml` en raíz tras el move. `requirements.txt` se dejó por ahora (no molesta); se elimina en PR 15. Smoke test: `load_config` + `build_chunks` OK contra un string de prueba (sin red). Tests reales end-to-end contra LM Studio quedan para cuando el usuario tenga el servidor arriba.

---

## PR 1 — Capa de I/O: extracción robusta DOCX ✅

Reemplazar `python-docx .paragraphs` por un walker XML que ve **todo** el contenido (tablas, headers, footers, footnotes, text boxes).

- [ ] `app/io/soffice.py`: wrapper LibreOffice headless (`.doc` → `.docx`) — **diferido a PR 1.1** cuando aparezca un `.doc` legacy real
- [x] ~~`app/io/docx_unpack.py`: unpack DOCX a directorio temporal~~ — no hizo falta desempacar a disco; se lee el ZIP en memoria con `zipfile`+`lxml`, más simple y sin side effects
- [x] Walker XML: `_iter_text_nodes` itera todos los `<w:t>` en orden de documento, incluyendo tablas, text boxes, smartArt (vía `iter()` del árbol completo)
- [x] Modelo de datos `TextRun(part, run_index, text, char_start, char_end)` + `DocxDocument` bundle
- [x] `extract_runs(docx_path) -> DocxDocument` cubre `document.xml`, `header*.xml`, `footer*.xml`, `footnotes.xml`, `endnotes.xml`, `comments.xml`
- [x] Tests sobre los 3 fixtures: offsets round-trip, cobertura ≥ python-docx (comparación normalizada + verificación palabra por palabra)
- [x] Comparación vs `python-docx`: 9/9 tests pasan. Los fixtures actuales no tienen headers/footers/footnotes, así que la ventaja estructural se verifica en los tests por no-regresión; queda probada en el campo cuando llegue un doc real con headers.

**Comentarios:**
> Commit `8a996c7`. Decisiones clave:
> - **No unpack a disco**: `zipfile.ZipFile` + `lxml.etree.fromstring` en memoria. Más rápido, sin cleanup, sin riesgo de dejar basura en `/tmp`.
> - **Separador entre parts = `\n\n`** (cuerpo vs header vs footer) y **separador entre párrafos = `\n`** (igual que python-docx). Elegido así porque el segmenter de PR 3 usa líneas en blanco como límite duro.
> - **Preservación de `<w:p>` ancestro por nodo** para poder emitir `\n` al cambiar de párrafo. El test inicial fallaba por 1-10 chars justamente por esto; resuelto.
> - **Test de no-regresión**: en vez de comparar bytes crudos (sensible a diferencias de whitespace), normalizo whitespace y además chequeo que ninguna palabra >3 chars del extractor viejo desaparezca en el nuevo. Esto es robusto y atrapa regresiones reales.
> - **Pendiente para PR 9**: el reemplazo deberá tocar el XML directamente usando `(part, run_index)` como clave. Ya está preparado el modelo de datos para eso.
> - **Pendiente para PR 2**: soffice wrapper para `.doc` legacy, sólo cuando aparezca un fixture que lo requiera.

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

## PR 3 — Segmentación semántica de oraciones ✅

- [x] Reemplazar `re.compile(r"\S+\s*")` por `pysbd` (segmenter español)
- [x] `app/pipeline/segment.py`: `segment_sentences(text) -> list[Sentence]` con offsets exactos
- [x] `app/pipeline/chunk.py`: empaqueta oraciones bajo presupuesto de tokens con `token_counter` **inyectable** (default: heurística ~4 chars/token para ES como límite superior conservador; hook listo para enchufar un tokenizer real en el futuro)
- [x] Regla dura: una oración no se parte salvo que exceda sola el presupuesto
- [x] Si excede: cortar por `;` → `:` → `,`, fallback por palabras (nunca a mitad de palabra)
- [x] Tests: 10/10 — round-trip, abreviaturas legales, presupuesto respetado, cobertura total, entidades íntegras, fallback de oraciones oversized, counter inyectable, overlap, e2e con `extract_runs` sobre fixture real
- [x] Overlap como parámetro opcional (`overlap_sentences`, default 0)

**Comentarios:**
> Commit `109071e`. Decisiones clave:
> - **Tokenizer real deferido**: LM Studio no expone el tokenizer del modelo vía API de forma estándar. El default heurístico (4 chars/token) es un **límite superior** conservador para español — tiende a sobre-contar, lo que es seguro (nunca desborda). Cuando se necesite precisión exacta, se inyecta `token_counter=tokenizer.encode` sin tocar el pipeline.
> - **pysbd con `char_span=True` y `clean=False`** es clave para preservar offsets exactos. `clean=True` modifica el texto internamente y rompe el round-trip.
> - **Separadores de split ordenados `;`→`:`→`,`**: refleja la estructura natural del español legal (listas de testigos con `;`, enumeraciones con `:`). El fallback por palabras sólo se dispara en oraciones patológicas.
> - **Overlap por oraciones**, no por tokens: mucho más simple de razonar y suficiente para el pipeline B+C donde el overlap es secundario (los detectores ven el doc completo por oraciones).
> - **Test e2e**: integra `extract_runs` → `segment` → `chunk` sobre un fixture real en `ejemplos/`. Produce chunks coherentes bajo presupuesto de 500 tokens heurísticos.

---

## PR 4 — Detección Capa 1: regex deterministas (identificadores) ✅

- [x] `app/detect/regex_detectors.py` con detectores para DNI, CUIT/CUIL, CBU, EMAIL, TELEFONO, PATENTE (Mercosur + viejo), PASAPORTE
- [x] CUIT/CUIL: checksum AFIP mod-11 completo
- [x] CBU: checksum BCRA de dos bloques (8+14)
- [x] DNI/TELEFONO/PASAPORTE: exigen keyword contextual para evitar falsos positivos sobre números de expediente
- [x] `app/detect/span.py`: `Span` + `resolve_overlaps` con `SOURCE_PRIORITY` (regex > structure > ner > llm)
- [x] Tests (25/25): precisión (cero falsos positivos sobre expediente/montos/fechas), recall (todas las variantes de formato), checksums verificados a mano, resolución de solapamientos

**Comentarios:**
> Commit `7dd44e4`. Decisiones:
> - **DNI exige keyword** (`DNI`, `D.N.I.`, `LE`, `LC`, `documento`) para descartar números de expediente del estilo `12.345/2024`. Falso positivo = anonimizar un número de expediente (grave porque rompe la trazabilidad de la causa); el costo del recall perdido es bajo porque los DNIs en texto legal casi siempre aparecen con keyword.
> - **Teléfono y pasaporte** mismo criterio: keyword obligatorio.
> - **CUIT, CBU, Email**: no requieren keyword — tienen suficiente estructura/checksum como para ser autovalidantes.
> - **Patente viejo** (LLL NNN) tiene confidence 0.75 porque colisiona con siglas. En PR 10 el gate de validación tratará esto con cuidado.
> - **Checksum AFIP con caso especial**: `check==10 → 9` (convención oficial).
> - **Compartible con PR 10**: el mismo `detect_all` se re-ejecuta sobre el texto anonimizado. Si encuentra algo, fail-closed.

---

## PR 5 — Detección Capa 2: NER en español ✅

- [x] Elegido **spaCy `es_core_news_md`** sobre `_lg` (10x menor, ~2pts F1 menos pero suficiente como capa de candidatos) y sobre Presidio (overhead innecesario para sólo NER, sin reglas legales españolas decentes)
- [x] `app/detect/ner.py`: `detect_entities` con singleton lazy-loaded
- [x] Default sólo `PER` (LOC/ORG son ruido: juzgados, ciudades, organismos públicos)
- [x] Resolución de solapamientos vía `resolve_overlaps` del PR 4 (regex > ner)
- [x] Tests (9/9): integración real, offsets, filtro de tipos, mock path, manejo de errores
- [x] Métrica de cobertura sobre fixture real: 7 regex + 74 NER = 81 spans combinados sin solapamientos

**Comentarios:**
> Commit `bf85c9a`. Decisiones:
> - **spaCy es opcional** (`extras_require[ner]`). El módulo lanza `ImportError`/`OSError` con instrucciones claras si falta. Esto permite que regex + segmentación funcionen en entornos minimalistas.
> - **Default `types=("PER",)`**: NER en LOC/ORG produce demasiados falsos positivos para legal (juzgados, organismos, ciudades). Mejor que el LLM clasificador del PR 7 los descubra por contexto si son relevantes.
> - **Confidence 0.7**: prior bajo, expresa que NER es ruidoso y el LLM tiene autoridad para sobreescribir vía rol asignado.
> - **Sanity check sobre CERAMI fixture**: detecta correctamente "Juan José Cerami", "Dr. Juan Exequiel Vergara", "Silvia Albarracín", "Dr. Guillermo H. Capdevila", pero también captura ruido como "Visa", "Prisma Medios" y un span sucio "Juan José Cerami DNI". Esto es **esperado**: NER es generoso, el LLM clasificador (PR 7) será quien filtre. La precisión final viene de la combinación.
> - **No se evaluó Presidio**: tras leer su soporte ES, no tiene reconocedores legales argentinos out-of-the-box, y agregar uno propio duplica trabajo que ya hicimos en PR 4 (regex). spaCy puro es la decisión correcta para este pipeline.

---

## PR 6 — Heurística estructural (zonas del documento) ✅

- [x] `app/detect/structure.py`: detecta CARATULA, FIRMA, CITA_DOCTRINA, CITA_JURISPRUDENCIA
- [x] Cada zona expone `default_role` (PARTE, JUEZ, AUTOR_DOCTRINA, AUTOR_JURISPRUDENCIA) que el clasificador del PR 7 toma como prior
- [x] `assign_zone()` con resolución de prioridades (citas > carátula/firma)
- [x] Tests (16/16): cada zona, fallbacks, fixture real, **regresión del bug NANZER**
- [x] Notas al pie diferidas: en DOCX ya vienen como `footnotes.xml` separadas (PR 1 las extrae); en PDF se evalúa cuando llegue PR 2.

**Comentarios:**
> Commit `d2ab542`. Decisiones y bugs encontrados:
> - **Bug del NANZER fixture**: la primera versión usaba `re.search` y tomaba el primer match de "PROTOCOLICESE", lo que generaba una zona FIRMA de 40k chars en NANZER porque la palabra aparece como cita interna. Arreglado: tomar el **último** match Y exigir que esté en el último 30% del documento. Hay test de regresión.
> - **Carátula con fallback de 800 chars**: en los 3 fixtures reales no encontramos los marcadores "Y VISTOS:" / "RESULTA:" en el inicio (Córdoba usa otra estructura). El fallback es razonable y se puede afinar después.
> - **`default_role` no es imperativo**: es un prior que el LLM puede sobrescribir. Esto preserva la doctrina del enfoque B+C: el clasificador tiene la última palabra.
> - **CITA_DOCTRINA detecta `conf. art.`**: hay falsos positivos como "conf. args. art. 68 del CPCyC" que NO son doctrina sino citas legales. El LLM clasificador (PR 7) los descartará por contexto. Se podría agregar exclusión por palabra "art./inc." pero prefiero dejarlo permisivo y que el LLM filtre.
> - **Sanity check sobre los 3 fixtures**: detecta correctamente carátula + firma + múltiples citas en CERAMI/DIAZ/NANZER. La firma de NANZER ahora es 89 chars en vez de 40k.

---

## PR 7 — Clasificador LLM con taxonomía cerrada (Capa B+C) ✅

El corazón del cambio. El LLM **nunca reescribe texto**; solo clasifica fichas de entidades.

- [x] `app/classify/taxonomy.py`: enum cerrado de 21 roles
- [x] `DEFAULT_ANONYMIZE_POLICY` + `AnonymizationPolicy` configurable + `from_dict` para cargar desde config.yaml
- [x] `app/classify/ficha.py`: `build_fichas` con contexto ±200 chars, zone hint, tipo NER. **Filtra spans regex** automáticamente.
- [x] `app/classify/lm_client.py`: cliente minimal stateless con determinismo hardcodeado
- [x] `app/classify/llm_classifier.py`: prompt cerrado + 4 few-shots + JSON contract + batching + retries
- [x] Validación Pydantic: `_ClassificationResponse` con `_ClassificationItem`
- [x] **Garantías estructurales** verificadas en tests:
  - JSON malformado → fallback DESCONOCIDO para todo el batch
  - Rol fuera del enum → DESCONOCIDO (`parse_role_safe` jamás raise)
  - Ficha omitida por el LLM → DESCONOCIDO
  - Largo de salida == largo de entrada SIEMPRE
  - DESCONOCIDO se anonimiza por policy (fail-closed)
  - Confianza fuera de rango → Pydantic ValidationError → fallback
- [x] Determinismo: temp=0, top_p=1, top_k=1, `force_json=True`
- [x] Tests (27/27): taxonomy + ficha + lm_client (mock HTTP) + classifier (mock client). Sin dependencia de LM Studio para tests.
- [x] Garantía estructural: el LLM **estructuralmente no puede** alterar texto porque sólo recibe fichas y devuelve JSON con strings del enum cerrado. Esto está aseverado por construcción + tests.

**Comentarios:**
> Commit `3296c3e`. Decisiones clave:
> - **Pydantic v2 con `model_validate`**: el wrapping `{"resultados": [...]}` es necesario porque LM Studio en JSON mode espera object en el top level, no array.
> - **Few-shots en español**: 4 ejemplos cubren los 4 casos críticos (parte/letrado/juez/autor doctrina). Más ejemplos no agregan valor con un modelo determinista.
> - **`get_placeholder_for_role`** vive en `llm_classifier.py` aunque depende sólo de `taxonomy.PLACEHOLDER_PREFIX`. Puesto ahí porque PR 8/9 lo van a importar desde el módulo de classification.
> - **`build_fichas` filtra `source=="regex"`**: los DNIs/CUITs/etc no necesitan clasificación, van directo al pipeline de anonimización. Ahorra ~50% de llamadas LLM en docs típicos.
> - **`max_retries=2` con backoff lineal**: balance razonable. El usuario puede subir/bajar via config.
> - **Test de integración real con LM Studio diferido**: requiere servidor corriendo, queda como `pytest -m integration` opcional cuando armemos PR 11/14.

---

## PR 8 — Coreferencia y consistencia entre menciones

- [x] `app/classify/coreference.py`: agrupa menciones de la misma entidad por apellido normalizado (lowercase + sin acentos + sin títulos "Dr./Dra./Sr./Sra.")
- [x] Política de consistencia: si dos menciones del mismo apellido reciben roles distintos del LLM, usar el rol de mayor confianza y loggear
- [x] Asignación de placeholders **estables** y **únicos por entidad**: `[ACTOR_1]`, `[ACTOR_2]`, `[TESTIGO_1]`, etc.
- [x] Persistir el mapping `entidad → placeholder` en el log de auditoría (`audit_log()`)
- [x] Tests: el mismo nombre repetido N veces siempre recibe el mismo placeholder (11 tests)

**Comentarios:**
> Commit `cec3830`. Decisiones clave:
> - **Cluster key = último token del nombre normalizado**: simple y suficiente para casos judiciales típicos. Casos compuestos como "Pérez de la Rúa" caerían en "rua"; aceptable porque el contexto del LLM separa identidades distintas via otros tokens.
> - **Conflict resolution por confianza**: cuando el LLM clasifica el mismo apellido con roles distintos en distintas menciones, gana el de mayor `confidence`. Loggeado a INFO.
> - **Contadores por rol independientes**: `[ACTOR_1]`, `[ACTOR_2]`, `[TESTIGO_1]` (no global). Replica la convención del legacy y es más legible.
> - **`audit_log()` devuelve dicts**: serializable a JSON sin esfuerzo, listo para PR 11.
> - **Singletons sin apellido válido** reciben key `__singleton_<id>` para no colapsar entre sí.

---

## PR 9 — Reemplazo determinista sobre el documento original

- [x] `app/replace/text_replacer.py`: aplica spans sobre `str` plano (para texto crudo y debug)
- [x] `app/replace/docx_replacer.py`: aplica spans sobre la lista de `TextRun` del PR 1, **preservando formato** (rPr, bold, fuentes, numeración). Repack a `.docx` editable.
- [ ] `app/replace/pdf_replacer.py`: diferido junto con PR 2 (PDF I/O)
- [x] La salida ya **no es `.txt`**: es del mismo formato que el input (DOCX)
- [x] Tests: roundtrip docx → anonimizar → docx (12 tests, fixtures reales)

> Commit `96697be`. Decisiones clave:
> - **Reescritura sólo de las parts modificadas**: las parts intactas se copian byte-a-byte del ZIP original. Esto preserva imágenes, estilos, numbering, content_types, etc., sin riesgo de regresión por re-serialización de lxml.
> - **`xml:space="preserve"` automático**: si el nuevo texto del run empieza/termina con whitespace, lo agregamos para que Word no lo colapse.
> - **Spans multi-run**: primer run = `before+replacement`, intermedios = `""`, último = `after`. Los separadores virtuales (\\n entre párrafos) que estaban dentro del span se descartan — coherente con "todo lo que está dentro del span es PII a borrar".
> - **`apply_replacements` falla rápido en solapamientos**: significa que `resolve_overlaps` no fue llamado río arriba. Silenciar sería peligroso (podría borrar texto correcto).
> - **Sin reemplazos = `shutil.copyfile`**: ni siquiera abre el ZIP, garantiza idempotencia perfecta.

---

## PR 10 — Validación post-hoc bloqueante

- [x] `app/validate/post_checks.py`: re-escanea el documento de salida con los **regex del PR 4**. Si encuentra algo → **fail-closed**.
- [x] Chequeo de longitud: ratio out/in dentro de `[0.5, 1.1]` (relajado vs plan original; ver comentarios)
- [x] Chequeo de integridad estructural: parts del ZIP (DOCX) idénticas
- [x] Chequeo de placeholders: cada placeholder esperado debe aparecer en el output
- [ ] Renombrar a `*_FAILED.docx` cuando falla — diferido a PR 11 (orquestador, donde se decide el nombre final)
- [x] Tests: 12 tests inyectando fallas

**Comentarios:**
> Commit `ed03c3d`. Decisiones clave:
> - **Ratio relajado a `[0.5, 1.1]`**: el plan decía `[0.85, 1.05]` pero borrar muchos nombres largos puede achicar significativamente. 0.5 sigue siendo un piso defensivo (cualquier colapso real cae mucho más abajo).
> - **`check_regex_leak` corre `detect_all`**: misma función que PR 4. Garantiza que el gate es exactamente la misma lógica que la detección, sin posibilidad de drift.
> - **`check_docx_structure` chequea names del ZIP, no contenido XML**: si una part desaparece es blocker; si aparece extra es warning (compatibilidad con futuros pipelines que agreguen metadata).
> - **`ValidationReport.passed` se mantiene actualizado vía `add()`**: API simple, evita olvidar setear el flag.
> - **Renombrado a `*_FAILED.docx` queda para PR 11**: la decisión del path final del output es del orquestador, no del validador.

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
