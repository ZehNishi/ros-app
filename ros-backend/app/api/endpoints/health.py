"""
Endpoints de saúde da aplicação.

GET /api/v1/health       — verifica se a API está no ar
GET /api/v1/health/ros   — verifica se o nó ROS está ativo
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.ros.node import ros_node

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    ros_running: bool


@router.get("/", response_model=HealthResponse)
def health_check():
    """Retorna o status geral da aplicação."""
    return HealthResponse(status="ok", ros_running=ros_node.is_running)


@router.get("/ros", response_model=HealthResponse)
def ros_health():
    """Retorna se o nó ROS está inicializado e operacional."""
    status = "ok" if ros_node.is_running else "ros_not_running"
    return HealthResponse(status=status, ros_running=ros_node.is_running)
