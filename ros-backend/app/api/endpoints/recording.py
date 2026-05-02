"""
Endpoints para controle do DataRecorder.

POST /api/v1/recording/start    — inicia sessão de gravação (thread background automática)
POST /api/v1/recording/stop     — para sessão e aguarda thread encerrar (join)
POST /api/v1/recording/save     — exporta dados para CSV
GET  /api/v1/recording/status   — estado atual da gravação

A partir da versão com background thread, ``start_recording()`` dispara
automaticamente uma thread daemon que chama ``record_from_buffer()`` a cada
``RECORD_INTERVAL`` segundos (padrão 0.2s). Nenhum polling externo é necessário.

Fluxo:
    POST /recording/start   → thread background inicia
    GET  /recording/status  → counts atualizam em tempo real
    POST /recording/save    → snapshot parcial (gravação continua)
    POST /recording/stop    → thread encerra, dados ficam em memória
    POST /recording/save    → exporta tudo
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.ros.data_recorder import data_recorder
from app.ros.topic_manager import TopicNotSubscribedError, topic_manager

logger = get_logger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class StartRecordingRequest(BaseModel):
    topics: list[str] = Field(
        ...,
        min_length=1,
        examples=[["/chatter", "/gps"]],
        description="Lista de tópicos ROS a gravar. Devem estar subscritos.",
    )


class StartRecordingResponse(BaseModel):
    status: str
    topics: list[str]
    detail: str


class StopRecordingResponse(BaseModel):
    status: str
    detail: str
    total_entries: int


class SaveRecordingRequest(BaseModel):
    output_dir: str = Field(
        ...,
        examples=["data_logs/session1"],
        description=(
            "Caminho do diretório de saída (relativo ao diretório de execução "
            "ou absoluto). Criado automaticamente se não existir."
        ),
    )


class SaveRecordingResponse(BaseModel):
    status: str
    output_dir: str
    files: dict[str, str]
    detail: str


class RecordingStatusResponse(BaseModel):
    recording: bool
    thread_running: bool
    topics: list[str]
    counts: dict[str, int]
    total_entries: int


# ---------------------------------------------------------------------------
# POST /recording/start
# ---------------------------------------------------------------------------

@router.post(
    "/recording/start",
    response_model=StartRecordingResponse,
    summary="Inicia sessão de gravação",
)
def start_recording(body: StartRecordingRequest):
    """
    Inicia uma sessão de gravação para os tópicos informados.

    Todos os tópicos devem estar subscritos no TopicManager antes de iniciar.
    Use ``POST /api/v1/subscribe`` para subscrever tópicos ainda não ativos.

    Se já houver uma sessão ativa, ela é descartada e substituída pela nova
    (os dados anteriores são perdidos — faça um ``POST /recording/save`` antes
    se precisar preservá-los).

    Raises:
        HTTP 400: Lista de tópicos vazia.
        HTTP 409: Um ou mais tópicos não estão subscritos no TopicManager.
    """
    topics = [_normalize_topic(t) for t in body.topics]
    logger.info("POST /recording/start — tópicos=%s.", topics)

    # Valida que todos os tópicos estão subscritos
    subscribed = {s["topic_name"] for s in topic_manager.list_subscribed()}
    not_subscribed = [t for t in topics if t not in subscribed]

    if not_subscribed:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Tópico(s) não subscrito(s): {not_subscribed}. "
                "Chame POST /api/v1/subscribe para cada tópico antes de gravar."
            ),
        )

    try:
        data_recorder.start_recording(topics)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("Gravação iniciada para %d tópico(s).", len(topics))
    return StartRecordingResponse(
        status="recording",
        topics=topics,
        detail=f"Gravação ativa para {len(topics)} tópico(s).",
    )


# ---------------------------------------------------------------------------
# POST /recording/stop
# ---------------------------------------------------------------------------

@router.post(
    "/recording/stop",
    response_model=StopRecordingResponse,
    summary="Para sessão de gravação",
)
def stop_recording():
    """
    Encerra a sessão de gravação ativa e aguarda a thread de background finalizar.

    A thread de background recebe sinal de parada e é joined antes de retornar
    — garantindo que não há coleta parcial em andamento quando o endpoint responde.
    Antes de sinalizar a parada, faz uma última coleta manual para capturar
    mensagens que chegaram entre o último ciclo da thread e este instante.

    Os dados coletados permanecem em memória e podem ser exportados via
    ``POST /recording/save``. Se não houver sessão ativa, retorna
    ``status: "not_recording"`` sem erro (idempotente).
    """
    logger.info("POST /recording/stop chamado.")

    if not data_recorder.recording:
        logger.info("stop_recording: nenhuma sessão ativa.")
        return StopRecordingResponse(
            status="not_recording",
            detail="Nenhuma sessão de gravação estava ativa.",
            total_entries=0,
        )

    # Flush manual: captura mensagens que chegaram desde o último ciclo da thread
    try:
        counts = data_recorder.record_from_buffer(topic_manager)
        logger.info("Flush final antes do stop: %s", counts)
    except Exception as exc:
        logger.warning("Erro no flush final antes do stop: %s", exc)

    # Para a gravação e faz join na thread (bloqueante até a thread encerrar)
    data_recorder.stop_recording()
    stats = data_recorder.get_stats()
    total = stats["total_entries"]

    logger.info("Gravação encerrada. Total de entradas: %d.", total)
    return StopRecordingResponse(
        status="stopped",
        detail=f"Gravação encerrada. {total} entrada(s) em memória.",
        total_entries=total,
    )


# ---------------------------------------------------------------------------
# POST /recording/save
# ---------------------------------------------------------------------------

@router.post(
    "/recording/save",
    response_model=SaveRecordingResponse,
    summary="Exporta dados gravados para CSV",
)
def save_recording(body: SaveRecordingRequest):
    """
    Realiza uma coleta final do buffer e exporta os dados gravados para CSV.

    Um arquivo CSV é gerado por tópico no diretório informado.
    O diretório é criado automaticamente se não existir.

    Pode ser chamado com a gravação ativa (salva snapshot parcial) ou
    após ``POST /recording/stop`` (salva tudo).

    Formato de cada arquivo:
        timestamp,campo1,campo2,...

    Raises:
        HTTP 400: Nenhuma sessão foi iniciada ou diretório inválido.
        HTTP 500: Erro de I/O ao gravar os arquivos.
    """
    logger.info("POST /recording/save — output_dir='%s'.", body.output_dir)

    # Coleta dados pendentes do buffer antes de salvar (se gravação ativa)
    if data_recorder.recording:
        try:
            counts = data_recorder.record_from_buffer(topic_manager)
            logger.info("Coleta antes do save: %s", counts)
        except Exception as exc:
            logger.warning("Erro na coleta antes do save: %s", exc)

    try:
        saved_files = data_recorder.save_to_csv(body.output_dir)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Erro de I/O ao salvar arquivos: {exc}",
        ) from exc

    if not saved_files:
        return SaveRecordingResponse(
            status="no_data",
            output_dir=body.output_dir,
            files={},
            detail="Nenhum arquivo gerado — os buffers de todos os tópicos estavam vazios.",
        )

    logger.info(
        "save_recording: %d arquivo(s) gerado(s) em '%s'.",
        len(saved_files), body.output_dir,
    )
    return SaveRecordingResponse(
        status="saved",
        output_dir=body.output_dir,
        files=saved_files,
        detail=f"{len(saved_files)} arquivo(s) CSV gerado(s).",
    )


# ---------------------------------------------------------------------------
# GET /recording/status
# ---------------------------------------------------------------------------

@router.get(
    "/recording/status",
    response_model=RecordingStatusResponse,
    summary="Estado atual da gravação",
)
def recording_status():
    """
    Retorna o estado atual do DataRecorder.

    A thread de background atualiza os contadores continuamente enquanto a
    gravação estiver ativa — este endpoint apenas lê o estado em memória,
    sem fazer coleta adicional nem bloquear a thread de background.

    Campos da resposta:
    - ``recording``      — True se uma sessão está ativa.
    - ``thread_running`` — True se a thread de background está viva.
    - ``topics``         — lista de tópicos na sessão atual.
    - ``counts``         — número de entradas gravadas por tópico.
    - ``total_entries``  — soma de todas as entradas em memória.
    """
    logger.debug("GET /recording/status chamado.")

    stats = data_recorder.get_stats()

    return RecordingStatusResponse(
        recording=stats["recording"],
        thread_running=stats["thread_running"],
        topics=stats["topics"],
        counts=stats["entry_counts"],
        total_entries=stats["total_entries"],
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _normalize_topic(name: str) -> str:
    """Garante que o nome do tópico começa com '/'."""
    name = name.strip()
    return name if name.startswith("/") else f"/{name}"
