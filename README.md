# Anonimizador de Documentos Legales

Aplicación de escritorio en Python que anonimiza archivos judiciales PDF y Word de forma local. El flujo divide cada documento en fragmentos, envía cada chunk a un modelo de lenguaje servido por [LM Studio](https://lmstudio.ai), aplica reglas estrictas de anonimización y reconstruye el documento final. Incluye una interfaz gráfica con pestañas para configurar parámetros, seguir el progreso y revisar los registros.

## Requisitos

- Python 3.9 o superior (probado en Windows).
- LM Studio instalado con el servidor local habilitado.
- Dependencias Python listadas en `requirements.txt`.

Instalación de dependencias:

```bash
python3 -m pip install -r requirements.txt
```

> `tkinter` se incluye con la mayoría de instalaciones oficiales de Python en Windows. Si falta, instalá Python desde [python.org](https://www.python.org/) seleccionando la opción “tcl/tk and IDLE”.

## Configuración

La configuración se almacena en `config.yaml` y admite override mediante variables de entorno. Campos principales:

- `lm_api`: `base_url`, `api_key`, `model` que LM Studio expondrá.
- `chunking`: parámetros de troceo en tokens (`max_context_tokens`, `overlap_tokens`, `safety_factor`).
- `inference`: hiperparámetros enviados al endpoint `/chat/completions`.
- `runtime`: comportamiento del pipeline (directorio de logs, reintentos, modo debug, etc.).

Variables de entorno opcionales (ejemplos):

```bash
set LM_API_BASE=http://127.0.0.1:1234/v1
set LM_API_KEY=lm-studio
set LM_API_MODEL=granite-3.1-8b-instruct
set LOGS_DIR=C:\anonimizador\logs
```

La pestaña **Configuración** de la GUI permite editar y guardar estos valores sin abrir el archivo manualmente.

## Puesta en marcha

1. Abrí LM Studio, cargá el modelo deseado y activá el servidor local (`Start Server`). El endpoint por defecto es `http://127.0.0.1:1234/v1`.
2. Cloná o copiá este proyecto y abrí una terminal en el directorio raíz.
3. Instalá dependencias: `python3 -m pip install -r requirements.txt`.
4. Ejecutá la aplicación:

   ```bash
   python3 "anonimizador v.5.py"
   ```

   En Windows se puede asociar el script a `python.exe` y ejecutarlo con doble click, siempre que el PATH y las dependencias estén configuradas.

## Uso de la GUI

### Pestaña Procesamiento

- **Examinar…**: selecciona un archivo `.pdf`, `.doc` o `.docx`.
- **Iniciar anonimización**: procesa el documento en un hilo independiente. Se muestra el progreso por chunks, bitácora y resumen final.
- **Limpiar resumen**: limpia el panel de resultado sin afectar los logs.

### Pestaña Configuración

- Visualiza y edita todos los parámetros. Los campos booleanos usan checkboxes, listas (p. ej. `stop_sequences`) se editan una por línea.
- **Guardar configuración** (persistente en `config.yaml`) y **Recargar desde archivo** para descartar cambios.

### Pestaña Registros

- Muestra en tiempo real los mensajes de log.
- **Abrir carpeta de logs** abre el directorio configurado (por defecto `./logs/`).
- **Limpiar vista** sólo afecta la visualización actual.

### Pestaña Acerca de

- Resumen del objetivo del proyecto y recordatorios sobre LM Studio.

## Salidas

- Documento anonimizado: se guarda junto al archivo original con sufijo `_anonimizado.txt`.
- Logs estructurados:
  - `logs/run_<timestamp>.jsonl`: entradas por chunk con métricas de duración, previews y errores.
  - `logs/run_summary_<timestamp>.json`: resumen del proceso (estado, tiempos, rutas).

En modo debug (`runtime.debug: true`) también se registran las entradas y salidas completas de cada chunk.

## Resolución de problemas

- **Sin conexión con LM Studio**: verificá que el servidor local esté activo (`LM Studio > Start Server`) y que la URL/clave coincidan.
- **Dependencias faltantes**: ejecutá nuevamente `python3 -m pip install -r requirements.txt`.
- **Errores en chunks individuales**: revisá el JSONL de la corrida y ajustá parámetros de solapamiento, contexto o reintentos desde la Configuración.
