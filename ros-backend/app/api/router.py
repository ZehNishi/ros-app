"""
Roteador raiz da API.

Agrega todos os sub-roteadores de endpoints em um único objeto
que é registrado no FastAPI em app/main.py.

Para adicionar novos grupos de rotas:
    1. Crie um arquivo em app/api/endpoints/
    2. Importe e inclua o router aqui com um prefixo e tags adequados.
"""

from fastapi import APIRouter

from app.api.endpoints import health, ros

api_router = APIRouter()

api_router.include_router(health.router, prefix="/health", tags=["health"])
api_router.include_router(ros.router, prefix="/ros", tags=["ros"])
