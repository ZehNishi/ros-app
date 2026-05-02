"""
Ponto de entrada principal da aplicação.

Responsabilidades deste módulo:
- Criar a instância FastAPI.
- Registrar exception handlers globais.
- Gerenciar o ciclo de vida (startup / shutdown) do nó ROS.
- Incluir todos os roteadores da API.

Toda a lógica de rotas fica em app/api/endpoints/.
Toda a lógica ROS fica em app/ros/.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import settings
from app.core.logging import get_logger
from app.ros.ros_client import ROSUnavailableError, ROSNotInitializedError, ros_client
from app.ros.topic_manager import topic_manager

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Ciclo de vida (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gerencia o ciclo de vida da aplicação.

    Startup:
        Tenta inicializar o nó ROS via ros_client.init().
        Se o ROS não estiver disponível (roscore ausente, rospy não instalado),
        a API sobe normalmente e retorna HTTP 503 nas rotas que precisam do ROS.

    Shutdown:
        Cancela todas as subscrições ativas e desliga o nó ROS.
    """
    # --- Startup ---
    logger.info(
        "Iniciando %s v%s...", settings.APP_NAME, settings.APP_VERSION
    )

    try:
        ros_client.init()
        logger.info("Nó ROS inicializado com sucesso no startup.")
    except ROSUnavailableError as exc:
        logger.warning(
            "ROS não disponível no startup: %s. "
            "A API ficará operacional, mas rotas ROS retornarão HTTP 503.",
            exc,
        )

    yield

    # --- Shutdown ---
    logger.info("Encerrando aplicação...")

    try:
        topic_manager.unsubscribe_all()
    except Exception as exc:
        logger.warning("Erro ao cancelar subscrições no shutdown: %s", exc)

    try:
        ros_client.shutdown()
    except Exception as exc:
        logger.warning("Erro ao desligar o nó ROS: %s", exc)

    logger.info("Aplicação encerrada.")


# ---------------------------------------------------------------------------
# Instância FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "Backend FastAPI integrado ao ROS Noetic via rospy. "
        "Permite consultar tópicos, fazer subscribe dinâmico e "
        "recuperar mensagens ROS serializadas como JSON."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Exception handlers globais
# ---------------------------------------------------------------------------

@app.exception_handler(ROSUnavailableError)
async def ros_unavailable_handler(request: Request, exc: ROSUnavailableError):
    """
    Converte ROSUnavailableError em HTTP 503 Service Unavailable.

    Causas comuns: roscore não está rodando, rospy não instalado,
    ROS_MASTER_URI incorreto.
    """
    logger.error("ROSUnavailableError em %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={
            "error": "ros_unavailable",
            "detail": str(exc),
            "hint": (
                "Verifique se o roscore está rodando e se "
                f"ROS_MASTER_URI={settings.ROS_MASTER_URI} está correto."
            ),
        },
    )


@app.exception_handler(ROSNotInitializedError)
async def ros_not_initialized_handler(request: Request, exc: ROSNotInitializedError):
    """
    Converte ROSNotInitializedError em HTTP 500 Internal Server Error.

    Indica que ros_client.init() não foi chamado antes de uma operação ROS.
    Normalmente não deve ocorrer em produção — é um erro de programação.
    """
    logger.error("ROSNotInitializedError em %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": "ros_not_initialized",
            "detail": str(exc),
        },
    )


# ---------------------------------------------------------------------------
# Roteadores
# ---------------------------------------------------------------------------

app.include_router(api_router, prefix="/api/v1")
