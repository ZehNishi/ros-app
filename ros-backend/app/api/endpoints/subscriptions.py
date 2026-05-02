"""
Endpoints de gerenciamento de subscrições e leitura de mensagens ROS.

POST /api/v1/subscribe                  — inicia subscrição dinâmica a um tópico
POST /api/v1/unsubscribe                — cancela subscrição
GET  /api/v1/subscriptions              — lista subscrições ativas com estatísticas
GET  /api/v1/topic/{name}/history       — histórico de mensagens do buffer
GET  /api/v1/topic/{name}/stream        — stream SSE de mensagens em tempo real
GET  /api/v1/topic/{name}              — última mensagem convertida para JSON

IMPORTANTE — ordem de registro das rotas (mais específica primeiro):
    1. /topic/{name}/history  — sufixo literal; deve vir antes do path converter
    2. /topic/{name}/stream   — sufixo literal; deve vir antes do path converter
    3. /topic/{name:path}     — captura qualquer coisa restante

Todas as rotas que interagem com o ROS propagam ROSUnavailableError e
ROSNotInitializedError, que são convertidas em HTTP 503/500 pelo handler
global em app/main.py.
"""

import asyncio
import json
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logging import get_logger
from app.ros.message_converter import convert_ros_message
from app.ros.ros_client import ros_client
from app.ros.topic_manager import (
    TopicNotSubscribedError,
    TopicSubscribeError,
    topic_manager,
)

# Configurações do stream SSE
_SSE_MIN_INTERVAL   = 0.01    # 10 ms — evita busy-loop
_SSE_MAX_INTERVAL   = 60.0    # 1 min — evita conexão ociosa demais
_SSE_DEFAULT_INTERVAL = 0.1   # 100 ms padrão
_SSE_KEEPALIVE_SECS = 15.0    # envia ": keepalive" se não houver dados

logger = get_logger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SubscribeRequest(BaseModel):
    topic: str = Field(..., examples=["/chatter"], description="Nome completo do tópico ROS.")


class SubscribeResponse(BaseModel):
    status: str
    topic: str
    msg_type: str


class UnsubscribeRequest(BaseModel):
    topic: str = Field(..., examples=["/chatter"], description="Nome completo do tópico ROS.")


class UnsubscribeResponse(BaseModel):
    status: str
    topic: str


class SubscriptionInfo(BaseModel):
    topic_name: str
    msg_type: str
    message_count: int
    has_latest: bool
    buffer_size: int = 0
    buffer_max: int = 0


class SubscriptionsResponse(BaseModel):
    count: int
    subscriptions: list[SubscriptionInfo]


class TopicMessageResponse(BaseModel):
    topic: str
    has_message: bool
    message: Optional[dict[str, Any]] = None
    status: str


class HistoryEntry(BaseModel):
    timestamp: float
    data: dict[str, Any]


class TopicHistoryResponse(BaseModel):
    topic: str
    count: int
    buffer_max: int
    entries: list[HistoryEntry]


# ---------------------------------------------------------------------------
# POST /subscribe
# ---------------------------------------------------------------------------

@router.post(
    "/subscribe",
    response_model=SubscribeResponse,
    summary="Inicia subscrição dinâmica a um tópico ROS",
)
def subscribe(body: SubscribeRequest):
    """
    Faz subscribe dinâmico ao tópico informado.

    O tipo da mensagem é resolvido automaticamente via rosmaster.
    Deve haver ao menos um publisher ativo no tópico no momento
    da chamada para que o tipo possa ser detectado.

    Chamar novamente para um tópico já subscrito é idempotente
    (retorna 200 sem criar um subscriber duplicado).

    Raises:
        HTTP 400: Falha ao fazer subscribe (tópico sem publishers ou tipo inválido).
        HTTP 503: ROS não disponível.
        HTTP 500: Nó ROS não inicializado.
    """
    topic = _normalize_topic(body.topic)
    logger.info("POST /subscribe — topic='%s'.", topic)

    try:
        topic_manager.subscribe(topic)
    except TopicSubscribeError as exc:
        logger.warning("Falha ao fazer subscribe em '%s': %s", topic, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Recupera o tipo resolvido a partir do registry do topic_manager
    subscriptions = topic_manager.list_subscribed()
    msg_type = next(
        (s["msg_type"] for s in subscriptions if s["topic_name"] == topic),
        "unknown",
    )

    logger.info("Subscribe em '%s' (%s) concluído.", topic, msg_type)
    return SubscribeResponse(status="subscribed", topic=topic, msg_type=msg_type)


# ---------------------------------------------------------------------------
# POST /unsubscribe
# ---------------------------------------------------------------------------

@router.post(
    "/unsubscribe",
    response_model=UnsubscribeResponse,
    summary="Cancela subscrição a um tópico ROS",
)
def unsubscribe(body: UnsubscribeRequest):
    """
    Cancela a subscrição ao tópico e descarta o buffer de mensagens.

    Raises:
        HTTP 404: Tópico não estava subscrito.
    """
    topic = _normalize_topic(body.topic)
    logger.info("POST /unsubscribe — topic='%s'.", topic)

    try:
        topic_manager.unsubscribe(topic)
    except TopicNotSubscribedError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Tópico '{topic}' não está subscrito.",
        ) from exc

    logger.info("Unsubscribe de '%s' concluído.", topic)
    return UnsubscribeResponse(status="unsubscribed", topic=topic)


# ---------------------------------------------------------------------------
# GET /subscriptions
# ---------------------------------------------------------------------------

@router.get(
    "/subscriptions",
    response_model=SubscriptionsResponse,
    summary="Lista subscrições ativas",
)
def list_subscriptions():
    """
    Retorna todos os tópicos atualmente subscritos com estatísticas básicas.

    Campos por subscrição:
    - ``topic_name``    — nome completo do tópico.
    - ``msg_type``      — tipo da mensagem ROS.
    - ``message_count`` — total de mensagens recebidas desde o subscribe.
    - ``has_latest``    — True se ao menos uma mensagem foi recebida.
    """
    logger.debug("GET /subscriptions chamado.")

    raw = topic_manager.list_subscribed()
    subscriptions = [SubscriptionInfo(**item) for item in raw]

    logger.info("GET /subscriptions — %d subscrição(ões) ativa(s).", len(subscriptions))
    return SubscriptionsResponse(count=len(subscriptions), subscriptions=subscriptions)


# ---------------------------------------------------------------------------
# GET /topic/{name}/history  ← deve ficar ANTES de /topic/{name:path}
# ---------------------------------------------------------------------------

@router.get(
    "/topic/{topic_name}/history",
    response_model=TopicHistoryResponse,
    summary="Histórico de mensagens do buffer",
)
def get_topic_history(
    topic_name: str,
    limit: Optional[int] = Query(
        default=None,
        ge=1,
        description="Retorna apenas os últimos N elementos. Omita para retornar tudo.",
    ),
):
    """
    Retorna o histórico de mensagens armazenadas no buffer do tópico.

    Cada entrada contém:
    - ``timestamp`` — ``time.time()`` registrado no momento em que a mensagem
                      chegou ao callback do subscriber (não o timestamp ROS).
    - ``data``      — mensagem ROS convertida para dict JSON-serializável via
                      ``convert_ros_message(msg, include_meta=True)``.

    A conversão opera sobre uma **cópia** do snapshot do buffer — o buffer
    original não é modificado nem bloqueado durante a conversão.

    Comportamento por estado:
    - Tópico não subscrito → HTTP 404.
    - Buffer vazio         → HTTP 200 com ``entries: []`` e ``count: 0``.
    - ``limit`` definido   → retorna apenas os últimos N elementos.

    Exemplo de resposta:
        {
          "topic": "/chatter",
          "count": 2,
          "buffer_max": 1000,
          "entries": [
            {"timestamp": 1714000000.1, "data": {"data": "hello", "_type": "std_msgs/String", ...}},
            {"timestamp": 1714000000.5, "data": {"data": "world", "_type": "std_msgs/String", ...}}
          ]
        }

    Raises:
        HTTP 404: Tópico não subscrito. Chame POST /subscribe primeiro.
    """
    topic = _normalize_topic(topic_name)
    logger.debug("GET /topic/%s/history — limit=%s.", topic, limit)

    # Obtém snapshot do buffer (lista de {"timestamp": float, "msg": <rospy>})
    try:
        snapshot = topic_manager.get_history(topic, limit=limit)
    except TopicNotSubscribedError as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Tópico '{topic}' não está subscrito. "
                "Chame POST /api/v1/subscribe antes de ler o histórico."
            ),
        ) from exc

    # Buffer vazio — retorna resposta válida sem erro
    if not snapshot:
        info = next(
            (s for s in topic_manager.list_subscribed() if s["topic_name"] == topic),
            {},
        )
        logger.debug("GET /topic/%s/history — buffer vazio.", topic)
        return TopicHistoryResponse(
            topic=topic,
            count=0,
            buffer_max=info.get("buffer_max", 0),
            entries=[],
        )

    # Converte cada mensagem do snapshot para dict JSON-serializável.
    # O snapshot já é uma cópia independente do buffer (list(...) no get_history),
    # portanto a conversão não bloqueia nem afeta o buffer original.
    entries: list[HistoryEntry] = []
    conversion_errors = 0

    for i, entry in enumerate(snapshot):
        ts: float = entry["timestamp"]
        raw_msg = entry["msg"]

        try:
            converted = convert_ros_message(raw_msg, include_meta=True)
        except Exception as exc:
            conversion_errors += 1
            logger.warning(
                "GET /topic/%s/history — erro ao converter entrada #%d: %s",
                topic, i, exc,
            )
            converted = {"_error": str(exc), "_raw": str(raw_msg)}

        entries.append(HistoryEntry(timestamp=ts, data=converted))

    if conversion_errors:
        logger.warning(
            "GET /topic/%s/history — %d/%d entrada(s) com erro de conversão.",
            topic, conversion_errors, len(snapshot),
        )

    # Recupera buffer_max da subscrição para incluir na resposta
    info = next(
        (s for s in topic_manager.list_subscribed() if s["topic_name"] == topic),
        {},
    )

    logger.info(
        "GET /topic/%s/history — retornando %d entrada(s) (limit=%s).",
        topic, len(entries), limit,
    )
    return TopicHistoryResponse(
        topic=topic,
        count=len(entries),
        buffer_max=info.get("buffer_max", 0),
        entries=entries,
    )


# ---------------------------------------------------------------------------
# GET /topic/{name}/stream  ← deve ficar ANTES de /topic/{name:path}
# ---------------------------------------------------------------------------

@router.get(
    "/topic/{topic_name}/stream",
    summary="Stream SSE de mensagens de um tópico ROS",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "Stream SSE contínuo de mensagens ROS",
        },
        404: {"description": "Tópico não subscrito"},
    },
)
async def stream_topic(
    request: Request,
    topic_name: str,
    interval: float = Query(
        default=_SSE_DEFAULT_INTERVAL,
        ge=_SSE_MIN_INTERVAL,
        le=_SSE_MAX_INTERVAL,
        description=(
            "Intervalo de polling em segundos (padrão 0.1s). "
            f"Mínimo: {_SSE_MIN_INTERVAL}s  Máximo: {_SSE_MAX_INTERVAL}s."
        ),
    ),
):
    """
    Abre uma conexão SSE (Server-Sent Events) e emite novas mensagens do
    tópico ROS em tempo real.

    **Formato de cada evento:**
    ```
    data: {"timestamp": 1714000000.123, "data": {"field": value, ...}}

    ```

    O campo ``data`` contém a mensagem ROS convertida via
    ``convert_ros_message(include_meta=True)`` — inclui ``_type`` e ``_time``.

    **Deduplicação:** apenas mensagens com ``timestamp`` maior que o último
    enviado são emitidas — chamadas repetidas ao buffer nunca geram duplicatas.

    **Keepalive:** se nenhuma mensagem nova chegar em ``{_SSE_KEEPALIVE_SECS}s``,
    um comentário SSE (``": keepalive"``) é enviado para manter a conexão viva
    em proxies e load-balancers.

    **Desconexão:** o loop verifica ``request.is_disconnected()`` a cada
    iteração e encerra silenciosamente quando o cliente fecha a conexão.

    **Headers retornados:**
    - ``Content-Type: text/event-stream``
    - ``Cache-Control: no-cache``
    - ``X-Accel-Buffering: no``  (desativa buffering no nginx)

    Raises:
        HTTP 404: Tópico não subscrito. Chame POST /subscribe primeiro.
    """
    topic = _normalize_topic(topic_name)
    logger.info(
        "GET /topic/%s/stream — nova conexão SSE (interval=%.3fs).", topic, interval
    )

    # Valida se o tópico está subscrito antes de abrir o stream
    subscribed = {s["topic_name"] for s in topic_manager.list_subscribed()}
    if topic not in subscribed:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Tópico '{topic}' não está subscrito. "
                "Chame POST /api/v1/subscribe antes de abrir o stream."
            ),
        )

    return StreamingResponse(
        _sse_generator(request, topic, interval),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "Connection":       "keep-alive",
            "X-Accel-Buffering": "no",   # desativa buffer no nginx
        },
    )


async def _sse_generator(
    request: Request,
    topic: str,
    interval: float,
) -> AsyncGenerator[str, None]:
    """
    Async generator que produz eventos SSE para o tópico informado.

    Estratégia:
    - Mantém ``last_ts`` por sessão para deduplicação baseada em timestamp.
    - A cada iteração:
        1. Verifica desconexão do cliente (``request.is_disconnected()``).
        2. Chama ``topic_manager.get_history()`` em executor para não bloquear
           o event loop — o lock interno do TopicManager é liberado rapidamente.
        3. Filtra entradas novas (``timestamp > last_ts``).
        4. Converte cada mensagem nova com ``convert_ros_message`` em executor
           (pode ser lento para msgs grandes como sensor_msgs/LaserScan).
        5. Emite evento SSE.
        6. Emite keepalive se ``_SSE_KEEPALIVE_SECS`` passar sem dados novos.
        7. Dorme ``interval`` segundos com ``asyncio.sleep`` (non-blocking).

    Encerramento limpo:
    - ``GeneratorExit``  — cliente desconectou; o StreamingResponse cancela o gen.
    - ``is_disconnected`` — detecção ativa a cada ciclo.
    - Exceção inesperada — loga e encerra (evita loop infinito em erro permanente).
    """
    last_ts: float = -1.0
    last_data_ts: float = asyncio.get_event_loop().time()
    loop = asyncio.get_event_loop()
    sent_count = 0

    logger.debug("_sse_generator('%s'): iniciado.", topic)

    try:
        while True:
            # ----------------------------------------------------------------
            # 1. Detecção de desconexão
            # ----------------------------------------------------------------
            if await request.is_disconnected():
                logger.info(
                    "_sse_generator('%s'): cliente desconectou após %d evento(s).",
                    topic, sent_count,
                )
                break

            # ----------------------------------------------------------------
            # 2. Leitura do buffer (offloada para thread pool — tem Lock interno)
            # ----------------------------------------------------------------
            try:
                history = await loop.run_in_executor(
                    None, lambda: topic_manager.get_history(topic)
                )
            except TopicNotSubscribedError:
                logger.warning(
                    "_sse_generator('%s'): tópico dessubscrito durante stream.", topic
                )
                yield _sse_event(
                    "error",
                    {"error": f"Tópico '{topic}' foi dessubscrito.", "code": "unsubscribed"},
                )
                break
            except Exception as exc:
                logger.warning("_sse_generator('%s'): erro ao ler buffer: %s", topic, exc)
                yield _sse_event("error", {"error": str(exc), "code": "buffer_error"})
                await asyncio.sleep(interval)
                continue

            # ----------------------------------------------------------------
            # 3. Filtra entradas novas
            # ----------------------------------------------------------------
            new_entries = [e for e in history if e["timestamp"] > last_ts]

            # ----------------------------------------------------------------
            # 4. Converte e emite cada entrada nova
            # ----------------------------------------------------------------
            for entry in new_entries:
                raw_msg = entry["msg"]
                ts      = entry["timestamp"]

                try:
                    msg_dict = await loop.run_in_executor(
                        None, lambda m=raw_msg: convert_ros_message(m, include_meta=True)
                    )
                except Exception as exc:
                    logger.warning(
                        "_sse_generator('%s'): erro ao converter msg ts=%.3f: %s",
                        topic, ts, exc,
                    )
                    msg_dict = {"_error": str(exc)}

                payload = {"timestamp": ts, "data": msg_dict}
                yield _sse_event("message", payload)

                last_ts = ts
                last_data_ts = loop.time()
                sent_count += 1

            # ----------------------------------------------------------------
            # 5. Keepalive — mantém conexão viva em proxies sem dados novos
            # ----------------------------------------------------------------
            if not new_entries:
                idle = loop.time() - last_data_ts
                if idle >= _SSE_KEEPALIVE_SECS:
                    yield ": keepalive\n\n"
                    last_data_ts = loop.time()
                    logger.debug(
                        "_sse_generator('%s'): keepalive enviado (idle=%.1fs).", topic, idle
                    )

            # ----------------------------------------------------------------
            # 6. Pausa non-blocking
            # ----------------------------------------------------------------
            await asyncio.sleep(interval)

    except GeneratorExit:
        logger.info(
            "_sse_generator('%s'): GeneratorExit — cliente encerrou conexão.", topic
        )
    except Exception as exc:
        logger.error(
            "_sse_generator('%s'): erro inesperado após %d evento(s): %s",
            topic, sent_count, exc,
        )
        try:
            yield _sse_event("error", {"error": str(exc), "code": "internal_error"})
        except Exception:
            pass
    finally:
        logger.info(
            "_sse_generator('%s'): stream encerrado. Total de eventos: %d.",
            topic, sent_count,
        )


def _sse_event(event_type: str, payload: dict) -> str:
    """
    Serializa um evento SSE no formato especificado pelo W3C::

        event: <type>\\n
        data: <json>\\n
        \\n

    O campo ``event`` permite ao cliente filtrar por tipo com
    ``EventSource.addEventListener('message', ...)`` vs ``'error'``.

    Args:
        event_type: Tipo do evento (``"message"`` ou ``"error"``).
        payload:    Dados a serializar como JSON.

    Returns:
        String no formato SSE pronta para ser yielded pelo generator.
    """
    try:
        data_str = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception as exc:
        data_str = json.dumps({"_serialize_error": str(exc)})
    return f"event: {event_type}\ndata: {data_str}\n\n"


# ---------------------------------------------------------------------------
# GET /topic/{name}
# ---------------------------------------------------------------------------

@router.get(
    "/topic/{topic_name:path}",
    response_model=TopicMessageResponse,
    summary="Última mensagem recebida em um tópico",
)
def get_latest_message(topic_name: str):
    """
    Retorna a última mensagem ROS recebida no tópico, convertida para JSON.

    A conversão é feita por ``convert_ros_message(msg, include_meta=True)``,
    que adiciona ``_type`` e ``_time`` à mensagem resultante.

    Comportamento por estado:
    - Tópico não subscrito        → HTTP 404.
    - Subscrito, sem mensagens    → HTTP 200 com ``has_message: false``.
    - Subscrito, com mensagem     → HTTP 200 com ``has_message: true`` e ``message``.

    Exemplo de resposta com mensagem:
        {
          "topic": "/chatter",
          "has_message": true,
          "status": "ok",
          "message": {
            "data": "hello world",
            "_type": "std_msgs/String",
            "_time": 1714000000.123
          }
        }

    Raises:
        HTTP 404: Tópico não subscrito. Chame POST /subscribe primeiro.
    """
    topic = _normalize_topic(topic_name)
    logger.debug("GET /topic/%s chamado.", topic)

    # Verifica se está subscrito
    try:
        raw_msg = topic_manager.get_latest(topic)
    except TopicNotSubscribedError as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Tópico '{topic}' não está subscrito. "
                "Chame POST /api/v1/subscribe antes de ler mensagens."
            ),
        ) from exc

    # Ainda não chegou nenhuma mensagem desde o subscribe
    if raw_msg is None:
        logger.debug("GET /topic/%s — sem mensagens ainda.", topic)
        return TopicMessageResponse(
            topic=topic,
            has_message=False,
            message=None,
            status="waiting_for_message",
        )

    # Converte a mensagem ROS para dict JSON-serializável
    converted = convert_ros_message(raw_msg, include_meta=True)

    logger.info(
        "GET /topic/%s — mensagem convertida (%d campo(s)).",
        topic,
        len(converted),
    )
    return TopicMessageResponse(
        topic=topic,
        has_message=True,
        message=converted,
        status="ok",
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _normalize_topic(name: str) -> str:
    """Garante que o nome do tópico começa com '/'."""
    name = name.strip()
    return name if name.startswith("/") else f"/{name}"
