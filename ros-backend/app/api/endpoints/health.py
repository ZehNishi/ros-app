"""
Endpoints de saúde da aplicação.

GET /api/v1/health      — verifica se a API está no ar
GET /api/v1/health/ros  — verifica se o nó ROS está ativo e acessível
POST /api/v1/health/connect — inicializa o nó ROS
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

_TCP_PROBE_TIMEOUT = 3.0   # segundos para o pré-teste TCP ao rosmaster


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
# Helpers
# ---------------------------------------------------------------------------

def _rosmaster_reachable(uri: str) -> bool:
    """
    Testa se a porta TCP do rosmaster está aberta.

    Retorna True rapidamente (< _TCP_PROBE_TIMEOUT) se acessível.
    Evita chamar rospy.init_node() quando o roscore não está de pé —
    o que causaria bloqueio indefinido no servidor.
    """
    try:
        parsed = urlparse(uri)
        host = parsed.hostname or "localhost"
        port = parsed.port or 11311
        conn = socket.create_connection((host, port), timeout=_TCP_PROBE_TIMEOUT)
        conn.close()
        return True
    except (socket.error, OSError):
        return False


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

    Fluxo:
    1. Se já inicializado → retorna sucesso imediatamente (idempotente).
    2. Testa conexão TCP ao rosmaster (timeout=3s) → 503 se porta fechada.
    3. Chama rospy.init_node() → 503 se falhar.

    O pré-teste TCP garante que init_node() nunca bloqueie o servidor
    quando o roscore não está de pé.
    """
    if ros_client._initialized:
        logger.info("POST /connect: ROS já inicializado — retornando sucesso (idempotente).")
        return {"status": "ok", "message": "ROS já estava inicializado."}

    # Aplica configurações de rede antes do probe
    if req.mode == "wifi" and req.master_uri:
        os.environ["ROS_MASTER_URI"] = req.master_uri
        settings.ROS_MASTER_URI = req.master_uri
        if req.ros_ip:
            os.environ["ROS_IP"] = req.ros_ip
    else:
        os.environ["ROS_MASTER_URI"] = "http://localhost:11311"
        settings.ROS_MASTER_URI = "http://localhost:11311"
        if "ROS_IP" in os.environ:
            del os.environ["ROS_IP"]

    # Pré-teste TCP — falha rápida se roscore não estiver ouvindo na porta
    if not _rosmaster_reachable(settings.ROS_MASTER_URI):
        msg = (
            f"Roscore não encontrado em {settings.ROS_MASTER_URI}. "
            "Verifique se o roscore está rodando (execute 'roscore' num terminal) "
            "e se ROS_MASTER_URI está correto."
        )
        logger.warning("POST /connect: %s", msg)
        raise HTTPException(status_code=503, detail=msg)

    # Porta aberta → init_node() deve completar rapidamente
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
        target_ip = parsed.hostname or target_uri.split(":")[0]

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target_ip, 1))
        ip = s.getsockname()[0]
        s.close()
        return {"ip": ip}
    except Exception:
        return {"ip": ""}
