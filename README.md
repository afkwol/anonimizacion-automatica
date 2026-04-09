# Anonimizador 2.0

Anonimizador determinista de documentos judiciales argentinos (Word `.docx`)
con LLM como **clasificador** (no como reescritor) y validación fail-closed.

Diseñado para abogados que necesitan publicar jurisprudencia preservando los
roles públicos (jueces, fiscales, autores citados) y anonimizando los privados
(partes, testigos, letrados, peritos de parte) **sin alucinaciones, sin
problemas de chunkeo y sin pérdida de formato**.

## Garantías de diseño

- **El LLM no puede alterar texto.** Recibe sólo "fichas" (entidad + ±200
  caracteres de contexto) y devuelve un rol del enum cerrado. Cualquier
  respuesta fuera del enum cae en `DESCONOCIDO` → se anonimiza (fail-closed).
- **Identificadores numéricos vía regex con checksum.** DNI, CUIT (mod-11),
  CBU (dos bloques BCRA), email, teléfono, patente, pasaporte. No pasan por
  el LLM: van directo a anonimización.
- **Formato preservado.** El reemplazo se hace in-place sobre los `<w:t>` del
  XML del DOCX, copiando byte-a-byte las parts no modificadas. Negritas,
  fuentes, numeración, headers, footers, footnotes — todo intacto.
- **Determinismo total.** `temperature=0`, `top_p=1`, `top_k=1` hardcoded.
- **Validación post-hoc bloqueante.** Re-corre los regex sobre el output;
  si filtra cualquier identificador, el archivo se renombra a
  `*_FAILED.docx` y NO se entrega.
- **Citas de doctrina y jurisprudencia preservadas.** Detector dedicado
  (`CSJN`, `Fallos:`, `"X c/ Y"`, `ver doctrina de NOMBRE`, `conf. NOMBRE`,
  patrón `APELLIDO, ..., AÑO`) marca autores como `preserve=True` antes de
  llegar al LLM.
- **Coreferencia estable.** El mismo apellido recibe el mismo placeholder
  (`[ACTOR_1]`, `[TESTIGO_2]`) en todo el documento.

## Requisitos

- Python 3.10+
- LM Studio con un modelo cargado y servidor local activo
- Dependencias: `pip install -e .[ner,dev]`
- spaCy NER (opcional): `python -m spacy download es_core_news_md`

## Uso

### GUI

```
run_anonimizador.bat        # Windows
python -m app --gui         # cualquier plataforma
```

Flujo de la GUI:

1. **Procesamiento** — elegí el `.docx`, click en **1. Detectar (dry-run)**.
2. **Revisión** — tabla de spans detectados con su rol asignado y un toggle
   "anonimizar". Doble clic para alternar entradas individuales.
3. **Configuración** — política `Role → anonimizar (bool)`. Editable.
4. **Aplicar y validar** (vuelta a Procesamiento) — escribe el archivo final
   y corre el gate de validación.
5. **Auditoría** — JSON con todo lo que pasó por etapa.

### CLI

```bash
python -m app archivo.docx                          # full pipeline
python -m app --dry-run --no-ner archivo.docx       # solo detección, sin spaCy
python -m app --base-url http://192.168.1.10:1234/v1 archivo.docx
```

Genera dos archivos al lado del input:
- `archivo_anonimizado.docx` (o `_FAILED.docx` si la validación falla)
- `archivo_audit.json`

## Configuración de LM Studio

LM Studio acepta `response_format: text` (no `json_object`). El cliente lo
maneja transparente. Si tu modelo es **reasoning** (qwen3, deepseek-r1), subí
`max_tokens` ≥ 16384 — el modelo gasta tokens en `reasoning_content` antes
del `content`. Lo más simple: cargá un instruct model (qwen2.5-instruct,
llama-3-instruct, granite-3-instruct).

## Tests

```bash
pytest -q                                  # 152 tests, todo mockeado
pytest tests/test_pipeline_e2e.py -v       # golden por fixture
```

Los E2E tests no requieren LM Studio: el cliente se mockea a `resultados: []`.

## Arquitectura

Ver [`ARCHITECTURE.md`](ARCHITECTURE.md) para el diagrama de capas y la
política de prioridades de detección.
