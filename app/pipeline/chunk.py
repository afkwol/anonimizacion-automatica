"""Empaqueta oraciones en chunks respetando un presupuesto de tokens.

Diseño — reglas duras:

1. **Una oración nunca se parte** salvo que por sí sola exceda el
   presupuesto máximo. Esta es la garantía estructural para que el PR 4
   (regex) y PR 5 (NER) jamás vean una entidad truncada.

2. **Si una oración individual excede el presupuesto**, se parte por
   delimitadores en orden de preferencia: `;` → `:` → `,`. Nunca por
   espacios ni a mitad de palabra. Si ningún delimitador sirve, se corta
   por palabras como último recurso (muy raro en texto legal).

3. **Overlap opcional**: por defecto `0` porque con el nuevo pipeline los
   detectores ven el documento completo por oraciones, no dependen de
   overlap entre chunks. Queda como parámetro por si alguna vez se usa
   para un modo "LLM ve ventana deslizante".

4. **Conteo de tokens**: en PR 3 aceptamos un callable `token_counter`
   inyectable. El default es una heurística conservadora para español
   (~4 caracteres por token BPE). Cuando tengamos el tokenizer real del
   modelo cargado en LM Studio, se pasa como argumento sin tocar el resto
   del pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Sequence

from .segment import Sentence

TokenCounter = Callable[[str], int]


def default_token_counter(text: str) -> int:
    """Heurística conservadora: ~4 chars por token para español.

    Es un LÍMITE SUPERIOR aproximado — tiende a sobre-contar un poco para
    no desbordar el contexto del modelo. Cuando se enchufe un tokenizer
    real (HF / tiktoken), esta función se reemplaza via inyección en
    `build_chunks(token_counter=...)`.
    """
    if not text:
        return 0
    # max(1, ...) asegura que oraciones muy cortas cuenten al menos 1 token.
    return max(1, (len(text) + 3) // 4)


@dataclass
class Chunk:
    """Un chunk listo para ser consumido por un detector o clasificador.

    Los offsets `char_start`/`char_end` son absolutos dentro del texto
    original (el mismo que se pasó a `segment_sentences`). Eso permite
    traducir cualquier span detectado dentro del chunk a una posición
    global sin ambigüedad.
    """

    index: int
    total: int
    text: str
    char_start: int
    char_end: int
    token_count: int
    sentences: List[Sentence] = field(default_factory=list)


# Delimitadores para partir una oración que excede el presupuesto por sí sola.
# Orden = prioridad: preferimos cortar en ';' antes que ':', y ':' antes que ','.
_SPLIT_DELIMS = (";", ":", ",")


def _split_oversized_sentence(
    sentence: Sentence,
    max_tokens: int,
    token_counter: TokenCounter,
) -> List[Sentence]:
    """Parte una oración que excede `max_tokens` por delimitadores seguros.

    Nunca corta a mitad de palabra ni rompe números. Si ningún delimitador
    alcanza a traer la pieza por debajo del presupuesto, cae en un corte
    por palabras como último recurso.
    """
    text = sentence.text
    if token_counter(text) <= max_tokens:
        return [sentence]

    # Probamos cada delimitador en orden. Nos quedamos con el primero que
    # produzca al menos una pieza manejable.
    for delim in _SPLIT_DELIMS:
        if delim not in text:
            continue
        pieces: List[Sentence] = []
        cursor = 0
        for idx, ch in enumerate(text):
            if ch == delim:
                piece_text = text[cursor : idx + 1]
                if piece_text.strip():
                    pieces.append(
                        Sentence(
                            text=piece_text,
                            start=sentence.start + cursor,
                            end=sentence.start + idx + 1,
                        )
                    )
                cursor = idx + 1
        tail = text[cursor:]
        if tail.strip():
            pieces.append(
                Sentence(
                    text=tail,
                    start=sentence.start + cursor,
                    end=sentence.end,
                )
            )
        # ¿Alguna pieza sigue siendo demasiado grande? Bajamos al siguiente delim.
        if all(token_counter(p.text) <= max_tokens for p in pieces):
            return pieces

    # Fallback: cortar por palabras. Sólo debería ocurrir en oraciones
    # patológicas (listas largas sin puntuación).
    words = text.split(" ")
    pieces: List[Sentence] = []
    buf_words: List[str] = []
    buf_start_offset = 0
    running_offset = 0
    for word in words:
        tentative = " ".join([*buf_words, word])
        if token_counter(tentative) > max_tokens and buf_words:
            piece_text = " ".join(buf_words)
            pieces.append(
                Sentence(
                    text=piece_text,
                    start=sentence.start + buf_start_offset,
                    end=sentence.start + buf_start_offset + len(piece_text),
                )
            )
            buf_start_offset = running_offset
            buf_words = [word]
        else:
            buf_words.append(word)
        running_offset += len(word) + 1  # +1 por el espacio separador
    if buf_words:
        piece_text = " ".join(buf_words)
        pieces.append(
            Sentence(
                text=piece_text,
                start=sentence.start + buf_start_offset,
                end=sentence.end,
            )
        )
    return pieces


def build_chunks(
    sentences: Sequence[Sentence],
    max_tokens: int,
    *,
    overlap_sentences: int = 0,
    token_counter: TokenCounter = default_token_counter,
) -> List[Chunk]:
    """Agrupa oraciones en chunks sin superar `max_tokens`.

    Args:
        sentences: salida de `segment_sentences`. Se asume orden.
        max_tokens: presupuesto duro por chunk.
        overlap_sentences: cuántas oraciones del final del chunk N se
            repiten al inicio del chunk N+1. Default 0.
        token_counter: inyectable; default = heurística 4 chars/token.

    Returns:
        Lista de `Chunk` con offsets absolutos y `sentences` asociadas.
        El primer chunk siempre empieza en la primera oración; el último
        siempre termina en la última.
    """
    if max_tokens <= 0:
        raise ValueError(f"max_tokens debe ser > 0 (recibido: {max_tokens})")
    if overlap_sentences < 0:
        raise ValueError("overlap_sentences no puede ser negativo")
    if not sentences:
        return []

    # Paso 1: normalizamos oraciones oversize. Si alguna oración sola
    # excede el presupuesto, la partimos antes de empezar a empacar.
    normalized: List[Sentence] = []
    for s in sentences:
        if token_counter(s.text) <= max_tokens:
            normalized.append(s)
        else:
            normalized.extend(_split_oversized_sentence(s, max_tokens, token_counter))

    # Paso 2: greedy packing.
    chunks: List[Chunk] = []
    buffer: List[Sentence] = []
    buffer_tokens = 0

    def flush() -> None:
        nonlocal buffer, buffer_tokens
        if not buffer:
            return
        char_start = buffer[0].start
        char_end = buffer[-1].end
        # Reconstituimos el texto del chunk a partir de offsets absolutos.
        # Esto preserva whitespace original entre oraciones.
        chunks.append(
            Chunk(
                index=len(chunks) + 1,
                total=0,  # se completa al final
                text="",  # idem, se completa al final
                char_start=char_start,
                char_end=char_end,
                token_count=buffer_tokens,
                sentences=list(buffer),
            )
        )
        buffer = []
        buffer_tokens = 0

    for sent in normalized:
        t = token_counter(sent.text)
        if buffer and buffer_tokens + t > max_tokens:
            flush()
            # Overlap: arrastramos las últimas N oraciones del chunk anterior.
            if overlap_sentences > 0 and chunks:
                carry = chunks[-1].sentences[-overlap_sentences:]
                buffer = list(carry)
                buffer_tokens = sum(token_counter(s.text) for s in buffer)
        buffer.append(sent)
        buffer_tokens += t

    flush()

    total = len(chunks)
    for c in chunks:
        c.total = total
    return chunks


def rebuild_chunk_text(chunk: Chunk, source_text: str) -> str:
    """Reconstruye el texto exacto de un chunk a partir del texto fuente.

    Preserva los espacios originales entre oraciones. Usar esta función
    cuando se necesite alimentar el chunk al detector, en lugar de
    concatenar `.text` de cada oración (que omitiría whitespace externo).
    """
    return source_text[chunk.char_start : chunk.char_end]
