"""
Endpoints de gerenciamento de subscrições e leitura de mensagens ROS.

POST /api/v1/subscribe              — inicia subscrição dinâmica a um tópico
POST /api/v1/unsubscribe            — cancela subscrição
GET  /api/v1/subscriptions          — lista subscrições ativas com estatísticas
GET  /api/v1/topic/{name}           — retorna última mensagem convertida para JSON

Todas as rotas que interagem com o ROS propagam ROSUnavailableError e
ROSNotInitializedError, que são convertidas em HTTP 503/500 pelo handler
global em app/main.py.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Optional

from app.core.logging import get_logger
from app.ros.message_converter import convert_ros_message
from app.ros.ros_client import ros_client
from app.ros.topic_manager import (
    TopicNotSubscribedError,
    TopicSubscribeError,
    topic_manager,
)

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


class SubscriptionsResponse(BaseModel):
    count: int
    subscriptions: list[SubscriptionInfo]


class TopicMessageResponse(BaseModel):
    topic: str
    has_message: bool
    message: Optional[dict[str, Any]] = None
    status: str


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
