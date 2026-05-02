"""
Endpoints de consulta de tópicos ROS.

GET /api/v1/topics              — lista todos os tópicos publicados no roscore
GET /api/v1/topics/{topic_name} — detalha o tipo de um tópico específico

Requer que o nó ROS esteja inicializado (ros_client.init() chamado no startup).
ROSUnavailableError e ROSNotInitializedError são tratados pelo handler global
em app/main.py e convertidos em HTTP 503 e HTTP 500 respectivamente.
"""

from __future__ import annotations

from typing import List, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.logging import get_logger
from app.ros.ros_client import ros_client

logger = get_logger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TopicInfo(BaseModel):
    name: str
    type: str


class TopicsResponse(BaseModel):
    count: int
    topics: List[TopicInfo]


class TopicDetailResponse(BaseModel):
    name: str
    type: str


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------

@router.get(
    "/",
    response_model=TopicsResponse,
    summary="Lista tópicos ROS ativos",
)
def list_topics():
    """
    Retorna todos os tópicos com publishers ativos no roscore.

    Chama ``rospy.get_published_topics()`` internamente. Apenas tópicos
    com ao menos um publisher ativo são listados.

    Raises:
        HTTP 503: ROS não disponível (roscore fora, rospy ausente).
        HTTP 500: Nó ROS não inicializado.
    """
    logger.info("GET /topics — consultando tópicos no roscore.")

    # ROSUnavailableError e ROSNotInitializedError propagam para o handler global
    raw_topics: List[Tuple[str, str]] = ros_client.get_topics()

    topics = [TopicInfo(name=name, type=msg_type) for name, msg_type in raw_topics]

    logger.info("GET /topics — %d tópico(s) encontrado(s).", len(topics))
    return TopicsResponse(count=len(topics), topics=topics)


@router.get(
    "/{topic_name:path}",
    response_model=TopicDetailResponse,
    summary="Tipo de um tópico específico",
)
def get_topic_type(topic_name: str):
    """
    Retorna o tipo da mensagem de um tópico específico.

    O ``topic_name`` deve ser o nome completo do tópico com a barra inicial,
    por exemplo: ``/chatter``, ``/scan``, ``/tf``.

    Como FastAPI não aceita ``/`` em path params por padrão, use o caminho
    completo na URL: ``GET /api/v1/topics//chatter``
    ou codifique a barra: ``GET /api/v1/topics/%2Fchatter``.

    Raises:
        HTTP 404: Tópico não encontrado ou sem publishers ativos.
        HTTP 503: ROS não disponível.
        HTTP 500: Nó ROS não inicializado.
    """
    # Garante que o nome começa com "/"
    if not topic_name.startswith("/"):
        topic_name = f"/{topic_name}"

    logger.info("GET /topics/%s — consultando tipo do tópico.", topic_name)

    try:
        msg_type = ros_client.get_topic_type(topic_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    logger.info("GET /topics/%s — tipo: %s.", topic_name, msg_type)
    return TopicDetailResponse(name=topic_name, type=msg_type)
