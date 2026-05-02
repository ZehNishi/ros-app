"""
Endpoints de saúde da aplicação.

GET /api/v1/health      — verifica se a API está no ar
GET /api/v1/health/ros  — verifica se o nó ROS está ativo e acessível
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import settings
from app.core.logging import get_logger
from app.ros.ros_client import ros_client

logger = get_logger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    ros_ready: bool
    ros_master_uri: str
    node_name: str


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------

@router.get("/", response_model=HealthResponse, summary="Status da API")
def health_check():
    """
    Retorna o status geral da aplicação.

    Sempre retorna HTTP 200 — indica que a API está no ar.
    O campo ``ros_ready`` informa se o nó ROS está ativo.
    """
    logger.debug("GET /health chamado.")
    return HealthResponse(
        status="ok",
        ros_ready=ros_client.is_ready,
        ros_master_uri=settings.ROS_MASTER_URI,
        node_name=settings.ROS_NODE_NAME,
    )


@router.get("/ros", response_model=HealthResponse, summary="Status do nó ROS")
def ros_health():
    """
    Retorna se o nó ROS está inicializado e o roscore está acessível.

    - ``status: "ok"``              → ROS pronto para uso.
    - ``status: "ros_not_running"`` → nó não inicializado ou roscore fora.

    Útil para health checks de infraestrutura (Kubernetes, Docker, etc.).
    """
    logger.debug("GET /health/ros chamado.")
    ready = ros_client.is_ready
    return HealthResponse(
        status="ok" if ready else "ros_not_running",
        ros_ready=ready,
        ros_master_uri=settings.ROS_MASTER_URI,
        node_name=settings.ROS_NODE_NAME,
    )
