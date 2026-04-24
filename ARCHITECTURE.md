# Arquitectura

Dos pipelines conviven en el código:

- **`--lite`** (recomendado, validado sobre 300+ resoluciones): un prompt al
  LLM extrae todas las partes del proceso en una sola llamada, se validan
  contra el texto, se reemplazan con tolerancia ortográfica. Rápido y robusto.
- **Modo completo** (legacy, modo por defecto sin `--lite`): NER + fichas por
  entidad + clasificador por *batch*. Más lento y menos robusto; se mantiene
  por compatibilidad.

Este documento describe el modo **`--lite`**, que es el pipeline principal.

## Diagrama de capas

```
┌──────────────────────────────────────────────────────────────────────┐
│  CLI (app/__main__.py)                                               │
│    python -m app --lite <archivo|carpeta>                            │
│    python -m app metrics <carpeta>                                   │
│    python -m app review <carpeta>                                    │
│                                                                      │
│  GUI (app/gui/app.py)                   Dashboard (app/review/)      │
│    Tkinter, 4 pestañas                  Flask + Jinja, QA visual     │
└──────────────────────────────────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼─────────────────────────────────────┐
│  Orquestador: app/pipeline/run_lite.py                               │
│                                                                      │
│    1. extract     2. llm_parties   3. caratula_parties               │
│    4. guardrail   5. regex         6. name_search                    │
│    7. resolve_overlaps             8. replace    9. audit            │
└──────────────────────────────────────────────────────────────────────┘
         │              │              │              │
         ▼              ▼              ▼              ▼
   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
   │   I/O    │   │  Detect  │   │ Classify │   │ Replace  │
   │ pdf_     │   │ llm_     │   │ lm_      │   │ pdf_     │
   │ extract  │   │ parties  │   │ client   │   │ replacer │
   │ docx_    │   │ caratula │   │          │   │ text_    │
   │ extract  │   │ regex_   │   │          │   │ replacer │
   │          │   │ detectors│   │          │   │ docx_    │
   │          │   │ name_    │   │          │   │ replacer │
   │          │   │ variants │   │          │   │          │
   └──────────┘   └──────────┘   └──────────┘   └──────────┘
```

## Pipeline (orden estricto)

1. **`extract_pdf` / `extract_runs`** — abre el archivo (PyMuPDF para PDF,
   ZIP+XML para DOCX) y genera `full_text` + posiciones por carácter para
   volver a mapear al layout original durante el reemplazo.

2. **`extract_parties`** (`app/detect/llm_parties.py`) — un único *prompt*
   al LLM con el texto completo. Devuelve JSON con las partes a anonimizar:

   ```json
   {"partes": [{"nombre": "APELLIDO, NOMBRE", "rol": "actor"}]}
   ```

   Roles aceptados: `actor`, `demandado`, `causante`, `heredero`, `testigo`,
   `victima`, `menor`. Roles **excluidos** por el prompt: jueces,
   camaristas, fiscales, secretarios, letrados, autores de doctrina,
   peritos oficiales, personas jurídicas.

   Los nombres devueltos se validan contra el texto literal (descarte de
   alucinaciones) y se filtran contra una lista de sufijos societarios
   (`_is_company`) para eliminar razones sociales que se hayan colado.

3. **`detect_caratula_parties`** (`app/detect/caratula_parties.py`) — red de
   seguridad: busca el patrón `APELLIDO, NOMBRE c/ DEMANDADO` en la cabecera
   del documento. Tolera mayúsculas (`REINANTE, LAUTARO NEHUEN C/ ...`),
   *Title Case* (`Reinoso, Elías Maximiliano c. ...`), iniciales
   (`Pedro A.`), separadores `c/`, `C/`, `c.`, `C.`, `contra`. Si el LLM
   omitió al actor o al demandado, la carátula los agrega.

4. **Guardián anti-silencio** — si después de LLM + carátula hay 0 partes
   y el texto tiene más de 500 caracteres, se emite una alerta que queda
   registrada en el *audit* y hace que el comando salga con código 2.

5. **`detect_regex`** — DNI / CUIT (mód-11) / CBU (dos bloques BCRA) /
   email / teléfono / patente / pasaporte. Sus *spans* obtienen prioridad
   máxima en la resolución de solapamientos.

6. **`find_all_occurrences`** + **`generate_variants`** + **`expand_variants_from_text`** (`app/detect/name_variants.py`) — para cada
   parte, se generan variantes ortográficas plausibles y se buscan en el
   texto con *regex* tolerante a:

   - acentos en cualquier posición de la palabra (`Nehuén` ↔ `Nehuen`)
   - `n` ↔ `ñ` (`Rodino` ↔ `Rodiño`)
   - vocales internas intercambiables en palabras ≥ 4 *chars*
     (`Esteban` ↔ `Estaban`)
   - espacios o *non-breaking space* antes de coma final (`Ferrari ,` ↔
     `Ferrari,`)
   - "s" opcional al final de palabras largas (`Farías` ↔ `Faría`)
   - nombres extendidos: si el LLM dio `VÁZQUEZ, ELSA A.` y el cuerpo usa
     `Elsa Alicia Vázquez`, se detecta y agrega como variante adicional

   Se omiten variantes con coma (`APELLIDO, NOMBRE`) cuando el nombre
   original no venía con coma, para evitar falsos *matches* en listas
   enumeradas tipo `Alfredo Oscar Revol, Mariano Jorge Revol, ...`.

7. **`resolve_overlaps`** (`app/detect/span.py`) — elimina *spans*
   solapados por prioridad (`regex > structure > ner > llm`), luego por
   longitud.

8. **`write_anonymized_pdf` / `write_anonymized_docx`** — el *replacer*
   identifica el rectángulo de cada ocurrencia en el archivo original,
   lo tacha con un rectángulo blanco, y escribe el *placeholder* con
   iniciales (`R., L. N.` para `REINANTE, LAUTARO NEHUEN`) centrado y
   rodeado de guiones para conservar el ancho original.

9. **Audit JSON** — escribe `<nombre>_audit.json` con:
   - Partes detectadas (nombre, rol, placeholder, origen)
   - `n_name_spans`, `n_regex_spans`, `n_replacements`
   - `n_pages`, `text_chars`, `model`, `replacements_per_page`, `replacements_per_kchar`
   - `warnings` del guardián anti-silencio
   - `timings` por etapa

## Módulos complementarios

### `app/metrics.py`

Lector de *audits* que agrega métricas sobre una carpeta procesada:
mediana y desvío de `replacements_per_kchar`, detección de *outliers* por
*z-score*, exportación CSV. Útil para detectar regresiones al actualizar
el modelo o el código.

### `app/review/`

Servidor Flask local (`python -m app review <carpeta>`) que muestra:
- Cola filtrable por estado (Pendiente / Aprobado / Necesita corrección /
  Rechazado) y por origen (Requieren revisión / Todos).
- Vista por documento con páginas renderizadas *lado a lado*
  (original vs anonimizado).
- Persistencia de decisiones en `_review_state.json`.

Pensado para flujos humano-en-el-loop sobre lotes grandes.

## Decisiones clave

### El LLM no reescribe texto

El *prompt* pide únicamente el JSON de partes; el reemplazo físico lo hace
código determinístico. Esto elimina la clase de errores más común en
sistemas de anonimización basados en LLM: alucinaciones, omisiones,
cambios de formato, introducción de errores tipográficos.

### Validación fail-closed

- Nombres devueltos por el LLM que no aparecen en el texto → descartados.
- Personas jurídicas detectadas por el LLM o la carátula → filtradas antes
  de anonimizar.
- 0 partes detectadas en documento con texto → alerta visible + exit 2.

### Determinismo

`temperature=0`, `top_p=1`, `top_k=1` fijos en `LMStudioConfig`. Dos
corridas sobre el mismo documento con el mismo modelo producen *audits*
idénticos.

### Sobre-anonimizar es mejor que sub-anonimizar

La expansión de variantes (acentos, ñ/n, vocales internas) prioriza no
perder ocurrencias aún a costa de eventuales falsos positivos — el costo
de un nombre expuesto es mucho mayor que el de una palabra tachada de
más. Los falsos positivos son además visibles en el *dashboard* de
revisión.

## Layout del código

```
app/
├── __main__.py              # CLI: --lite, --gui, review, metrics
├── metrics.py               # Agregador de audits
├── classify/
│   └── lm_client.py         # Cliente HTTP de LM Studio (determinista)
├── detect/
│   ├── caratula_parties.py  # Red de seguridad: "APELLIDO, NOMBRE c/ ..."
│   ├── llm_parties.py       # Prompt principal + filtro de jurídicas
│   ├── name_variants.py     # Variantes ortográficas + búsqueda flexible
│   ├── regex_detectors.py   # DNI / CUIT / CBU / email / etc.
│   └── span.py              # Modelo común + resolve_overlaps
├── gui/
│   └── app.py               # Tkinter, para uso interactivo
├── io/
│   ├── docx_extract.py      # Walker de <w:t> sobre el ZIP
│   └── pdf_extract.py       # PyMuPDF con posiciones por span
├── pipeline/
│   ├── run.py               # Modo completo (legacy)
│   └── run_lite.py          # Modo --lite (recomendado)
├── replace/
│   ├── docx_replacer.py     # In-place sobre <w:t> preservando formato
│   ├── pdf_replacer.py      # Tacha + placeholder con iniciales sobre PDF
│   └── text_replacer.py     # Reemplazo sobre str plano
├── review/
│   ├── server.py            # Dashboard Flask
│   └── templates/           # UI de revisión
└── validate/
    └── post_checks.py       # Gate fail-closed (modo completo)
```
