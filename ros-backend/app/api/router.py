"""
Roteador raiz da API.

Agrega todos os sub-roteadores de endpoints em um único objeto
que é registrado no FastAPI em app/main.py com o prefixo /api/v1.

Rotas registradas:
    GET  /api/v1/health             — status da API
    GET  /api/v1/health/ros         — status do nó ROS
    GET  /api/v1/topics             — lista tópicos ROS ativos
    POST /api/v1/subscribe          — inicia subscrição dinâmica
    POST /api/v1/unsubscribe        — cancela subscrição
    GET  /api/v1/subscriptions      — lista subscrições ativas
    GET  /api/v1/topic/{name}       — última mensagem de um tópico
"""

from fastapi import APIRouter

from app.api.endpoints import health, topics, subscriptions

api_router = APIRouter()

api_router.include_router(health.router,        prefix="/health",       tags=["health"])
api_router.include_router(topics.router,        prefix="/topics",       tags=["topics"])
api_router.include_router(subscriptions.router, prefix="",              tags=["subscriptions"])
