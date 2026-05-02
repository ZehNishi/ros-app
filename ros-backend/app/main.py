"""
Ponto de entrada principal da aplicação.

Inicializa o FastAPI, registra os roteadores da API
e configura o ciclo de vida (lifespan) para subir/descer o nó ROS.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gerencia o ciclo de vida da aplicação.

    - Na inicialização: conecta ao ROS (inicia o nó rospy).
    - No encerramento: desliga o nó ROS corretamente.

    Substitua os comentários abaixo pela inicialização real do nó ROS
    quando o ambiente ROS Noetic estiver disponível.
    """
    logger.info("Iniciando aplicação...")

    # TODO: iniciar o nó ROS aqui
    # from app.ros.node import ros_node
    # await ros_node.start()

    yield

    logger.info("Encerrando aplicação...")

    # TODO: desligar o nó ROS aqui
    # await ros_node.stop()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Backend FastAPI integrado ao ROS Noetic via rospy.",
    lifespan=lifespan,
)

app.include_router(api_router, prefix="/api/v1")
