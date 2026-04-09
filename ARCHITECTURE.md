# Arquitectura — Anonimizador 2.0

## Capas

```
┌─────────────────────────────────────────────────────────────┐
│  GUI (app/gui)             CLI (app/__main__.py)            │
│  Tkinter, 4 pestañas       argparse                         │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────┐
│  Orquestador  (app/pipeline/run.py)                         │
│  extract → detect → resolve → fichas → classify → coref →  │
│            replace → validate → audit                       │
└─────────────────────────────────────────────────────────────┘
       │           │             │              │         │
       ▼           ▼             ▼              ▼         ▼
┌──────────┐ ┌──────────┐ ┌────────────┐ ┌──────────┐ ┌──────────┐
│   I/O    │ │  Detect  │ │  Classify  │ │  Replace │ │ Validate │
│ docx_    │ │ regex    │ │ taxonomy   │ │ text_    │ │ post_    │
│ extract  │ │ ner      │ │ ficha      │ │ replacer │ │ checks   │
│          │ │ structure│ │ lm_client  │ │ docx_    │ │          │
│          │ │ citations│ │ llm_       │ │ replacer │ │          │
│          │ │ span     │ │ classifier │ │          │ │          │
│          │ │          │ │ coreference│ │          │ │          │
└──────────┘ └──────────┘ └────────────┘ └──────────┘ └──────────┘
```

## Pipeline (orden estricto)

1. **`extract_runs`** — abre el `.docx` como ZIP, recorre `<w:t>` en todas
   las parts (document, headers, footers, footnotes, endnotes, comments).
   Emite `TextRun`s con `(part, run_index, char_start, char_end)`. El
   `full_text` es la concatenación; los offsets de cada run apuntan a él.

2. **`detect_regex`** — DNI/CUIT/CBU/email/teléfono/patente/pasaporte con
   checksums (mod-11 para CUIT, dos bloques BCRA para CBU).

3. **`detect_zones`** — heurísticas estructurales: CARÁTULA, FIRMA,
   CITA_DOCTRINA, CITA_JURISPRUDENCIA. Son priors, no veredictos.

4. **`detect_citations`** — emite `Span`s concretos con `metadata.preserve=True`
   sobre nombres de autores y causas citadas. Tipo: `AUTOR_DOCTRINA` /
   `AUTOR_JURISPRUDENCIA`. `source="structure"` para ganar a NER en el
   resolve.

5. **`detect_entities`** (NER, opcional) — spaCy `es_core_news_md`. Sólo
   `PER` por default. Lazy-loaded.

6. **`resolve_overlaps`** — merge de spans con prioridad
   `regex(100) > structure(80) > ner(60) > llm(40)`. En empates, gana el
   span más largo.

7. **`build_fichas`** — para cada span no-regex y no-`preserve`, arma una
   ficha con contexto ±200 chars, zone hint y NER type.

8. **`LLMClassifier.classify`** — batches al LM Studio, retries con backoff.
   Output: enum cerrado de 21 roles. Cualquier respuesta inválida →
   `DESCONOCIDO`. Garantiza len(output) == len(input).

9. **`resolve_coreference`** — agrupa fichas por apellido normalizado
   (lowercase + sin acentos + sin títulos). Conflictos de rol se resuelven
   por mayor confianza. Asigna placeholders estables `[PREFIX_N]`.

10. **`build_replacements`** — combina `Replacement`s de spans regex (con
    counters por tipo) y de spans clasificados (con placeholders de
    coreferencia). Ordenados, sin solapar.

11. **`write_anonymized_docx`** — copia el ZIP byte-a-byte salvo en las
    parts modificadas; en esas reescribe sólo los `<w:t>` afectados, con
    `xml:space="preserve"` automático para whitespace.

12. **`validate_docx_output`** — re-extrae el output, re-corre regex,
    chequea ratio de longitud, integridad estructural del ZIP y presencia
    de placeholders esperados. Si algo falla → `*_FAILED.docx`.

13. **Audit JSON** — escribe `*_audit.json` con métricas por etapa,
    coreferencia y issues de validación.

## Decisiones clave

### LLM como clasificador, no como reescritor

El legacy v.5 mandaba chunks de texto al LLM y le pedía "reescribí esto
anonimizando los nombres". Esto **es** la fuente de las alucinaciones,
omisiones y problemas de chunkeo: el LLM puede reescribir mal, omitir, o
inventar.

En 2.0 el LLM **estructuralmente no puede** alterar texto. Recibe sólo
una ficha (entidad + contexto + zona) y debe devolver un valor del enum.
La sustitución la hace el código determinista en `replace/`.

### Fail-closed en todos los puntos de duda

- Rol desconocido del LLM → `DESCONOCIDO` → se anonimiza.
- Validación post-hoc detecta filtración → archivo renombrado a `_FAILED`.
- NER spaCy no disponible → se continúa sin NER (warning).
- LLM HTTP error tras retries → batch entero cae a `DESCONOCIDO`.

### Determinismo total

`temperature=0`, `top_p=1`, `top_k=1` están hardcoded en `LMStudioConfig`.
Dos corridas del mismo doc con el mismo modelo deben producir bytes
idénticos.

### Preservar > anonimizar (cuando hay base legal)

La política default refleja Acordadas CSJN 15/13 y 24/13: jueces, fiscales,
secretarios, autores citados y entidades públicas NO se anonimizan. La
política es editable desde la pestaña Configuración de la GUI.

## Testing

- **Unit tests** por módulo (~140 tests): `tests/test_*.py`.
- **Golden E2E** (`tests/test_pipeline_e2e.py`): corre el pipeline completo
  con LLM mockeado contra los fixtures de `ejemplos/` y valida thresholds
  mínimos por fixture (`tests/golden/expectations.json`).
- **Smoke GUI**: construye la ventana sin lanzar mainloop.

Total: 152 tests.

## Layout

```
app/
├── __main__.py          # CLI entry point
├── classify/
│   ├── coreference.py   # Agrupación + placeholders estables
│   ├── ficha.py         # Construcción de fichas para el LLM
│   ├── llm_classifier.py # Cliente del clasificador (Pydantic)
│   ├── lm_client.py     # Wrapper HTTP de LM Studio
│   └── taxonomy.py      # Enum cerrado de roles + política
├── detect/
│   ├── citations.py     # Doctrina y jurisprudencia (preserve=True)
│   ├── ner.py           # spaCy es_core_news_md
│   ├── regex_detectors.py # DNI/CUIT/CBU/etc con checksums
│   ├── span.py          # Modelo común + resolve_overlaps
│   └── structure.py     # Carátula, firma, citas (zonas)
├── gui/
│   └── app.py           # Tkinter, 4 pestañas
├── io/
│   └── docx_extract.py  # Walker de <w:t> sobre el ZIP
├── pipeline/
│   ├── chunk.py         # Token-budget chunking (no usado en E2E actual)
│   ├── run.py           # Orquestador
│   └── segment.py       # pysbd español
├── replace/
│   ├── docx_replacer.py # Reemplazo in-place preservando formato
│   └── text_replacer.py # Reemplazo sobre str plano
└── validate/
    └── post_checks.py   # Gate fail-closed
```
