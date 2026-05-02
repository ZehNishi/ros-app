"""
Endpoints de saúde da aplicação.

GET /api/v1/health      — verifica se a API está no ar
GET /api/v1/health/ros  — verifica se o nó ROS está ativo e acessível
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import os
import socket
from urllib.parse import urlparse

from app.core.config import settings
from app.core.logging import get_logger
from app.ros.ros_client import ros_client, ROSUnavailableError

logger = get_logger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    ros_ready: bool
    ros_initialized: bool
    ros_master_uri: str
    node_name: str

class ConnectRequest(BaseModel):
    mode: str
    master_uri: Optional[str] = None
    ros_ip: Optional[str] = None


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
        ros_initialized=ros_client._initialized,
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
    initialized = ros_client._initialized
    
    if ready:
        status_str = "ok"
    else:
        status_str = "ros_not_running" if initialized else "uninitialized"

    return HealthResponse(
        status=status_str,
        ros_ready=ready,
        ros_initialized=initialized,
        ros_master_uri=settings.ROS_MASTER_URI,
        node_name=settings.ROS_NODE_NAME,
    )

@router.post("/connect", summary="Inicializa o nó ROS com as configurações fornecidas")
def connect_ros(req: ConnectRequest):
    """
    Inicializa o cliente ROS com Master URI local ou remoto.

    Comportamentos quando já inicializado:
    - ``_initialized=True`` e ``is_ready=True``  → retorna sucesso (idempotente).
    - ``_initialized=True`` e ``is_ready=False`` → roscore ficou offline;
      rospy não suporta re-inicialização — orienta reinício do servidor.
    """
    if ros_client._initialized:
        if ros_client.is_ready:
            logger.info("POST /connect: ROS já inicializado e ativo — retornando sucesso.")
            return {"status": "ok", "message": "ROS já estava conectado e ativo."}
        else:
            raise HTTPException(
                status_code=409,
                detail=(
                    "O roscore ficou offline após a inicialização. "
                    "Reinicie o servidor Python (ros-backend) e tente conectar novamente."
                ),
            )

    if req.mode == "wifi" and req.master_uri:
        os.environ["ROS_MASTER_URI"] = req.master_uri
        settings.ROS_MASTER_URI = req.master_uri
        if req.ros_ip:
            os.environ["ROS_IP"] = req.ros_ip
    else:
        # Modo local, garante uso dos defaults
        os.environ["ROS_MASTER_URI"] = "http://localhost:11311"
        settings.ROS_MASTER_URI = "http://localhost:11311"
        if "ROS_IP" in os.environ:
            del os.environ["ROS_IP"]

    try:
        ros_client.init()
        return {"status": "ok", "message": "Conectado com sucesso."}
    except ROSUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

@router.get("/detect-ip", summary="Detecta IP local roteável para o target")
def detect_ip(target_uri: str):
    """
    Descobre o IP da máquina atual que consegue acessar o target_uri.
    Evita ter que digitar manualmente o ROS_IP.
    """
    try:
        parsed = urlparse(target_uri)
        # Se parsed.hostname falhar (ex: string mal formatada), tenta usar o target bruto
        target_ip = parsed.hostname or target_uri.split(":")[0]
        
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target_ip, 1))
        ip = s.getsockname()[0]
        s.close()
        return {"ip": ip}
    except Exception:
        return {"ip": ""}
