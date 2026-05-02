"""
Configurações centrais da aplicação.

Carrega variáveis de ambiente com Pydantic Settings.
Crie um arquivo .env na raiz do projeto para sobrescrever os valores padrão.

Exemplo de .env:
    APP_NAME=meu-ros-backend
    ROS_MASTER_URI=http://localhost:11311
    LOG_LEVEL=DEBUG
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Aplicação ---
    APP_NAME: str = "ros-fastapi-backend"
    APP_VERSION: str = "0.1.0"

    # --- Servidor ---
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # --- ROS ---
    ROS_MASTER_URI: str = "http://localhost:11311"
    ROS_NODE_NAME: str = "fastapi_ros_node"

    # --- Logging ---
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Instância global de configurações — importe este objeto nos outros módulos.
settings = Settings()
