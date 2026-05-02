"""
Roteador raiz da API.

Agrega todos os sub-roteadores de endpoints em um único objeto
que é registrado no FastAPI em app/main.py com o prefixo /api/v1.

Rotas registradas:
    GET  /api/v1/health                 — status da API
    GET  /api/v1/health/ros             — status do nó ROS
    GET  /api/v1/topics                 — lista tópicos ROS ativos
    POST /api/v1/subscribe              — inicia subscrição dinâmica
    POST /api/v1/unsubscribe            — cancela subscrição
    GET  /api/v1/subscriptions          — lista subscrições ativas
    GET  /api/v1/topic/{name}/history   — histórico do buffer de um tópico
    GET  /api/v1/topic/{name}/stream    — stream SSE de mensagens em tempo real
    GET  /api/v1/topic/{name}           — última mensagem de um tópico
    GET  /api/v1/files?path=            — lista arquivos/diretórios no workspace
    GET  /api/v1/file?path=             — lê conteúdo de arquivo
    POST /api/v1/file                   — cria ou substitui arquivo
    POST /api/v1/run                    — executa comando e aguarda conclusão
    POST /api/v1/run/background         — executa comando em background
    POST /api/v1/kill                   — encerra processo por PID
    POST /api/v1/recording/start        — inicia sessão de gravação
    POST /api/v1/recording/stop         — para sessão de gravação
    POST /api/v1/recording/save         — exporta dados para CSV
    GET  /api/v1/recording/status       — estado atual da gravação
    GET  /api/v1/plot/gps/compare       — múltiplas trajetórias GPS sobrepostas
    GET  /api/v1/plot/gps/{topic}       — trajetória GPS única (lat × lon)
    GET  /api/v1/plot/{topic}           — gráfico PNG de campo de tópico ROS
"""

from fastapi import APIRouter

from app.api.endpoints import health, topics, subscriptions, recording, plot
from app.api.routes_files import router as files_router
from app.api.routes_system import router as system_router

api_router = APIRouter()

api_router.include_router(health.router,        prefix="/health",       tags=["health"])
api_router.include_router(topics.router,        prefix="/topics",       tags=["topics"])
api_router.include_router(subscriptions.router, prefix="",              tags=["subscriptions"])
api_router.include_router(files_router,         prefix="",              tags=["files"])
api_router.include_router(system_router,        prefix="",              tags=["system"])
api_router.include_router(recording.router,     prefix="/recording",    tags=["recording"])
api_router.include_router(plot.router,          prefix="",              tags=["plot"])
