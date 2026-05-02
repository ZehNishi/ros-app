"""
Endpoints relacionados ao ROS.

Expõe operações ROS como rotas HTTP REST.
A lógica ROS fica em app/ros/ — estes endpoints apenas chamam essas funções.

Exemplos planejados:
    POST /api/v1/ros/publish/chatter   — publica mensagem no tópico /chatter
    GET  /api/v1/ros/topics            — lista tópicos disponíveis (futuro)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.ros.node import ros_node
from app.ros.topics import publish_chatter

router = APIRouter()


class PublishRequest(BaseModel):
    message: str


class PublishResponse(BaseModel):
    success: bool
    detail: str


@router.post("/publish/chatter", response_model=PublishResponse)
def publish_to_chatter(body: PublishRequest):
    """
    Publica uma mensagem no tópico ROS /chatter.

    Requer que o nó ROS esteja ativo.
    """
    if not ros_node.is_running:
        raise HTTPException(status_code=503, detail="Nó ROS não está ativo.")

    # TODO: remover o try/except quando publish_chatter estiver implementado
    try:
        publish_chatter(body.message)
        return PublishResponse(success=True, detail="Mensagem publicada.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Adicione novos endpoints ROS abaixo seguindo o mesmo padrão
# ---------------------------------------------------------------------------
