"""
Configurações centrais da aplicação.

Carrega variáveis de ambiente com Pydantic Settings.
Crie um arquivo .env na raiz do projeto para sobrescrever os valores padrão.

Exemplo de .env:
    APP_NAME=meu-ros-backend
    ROS_MASTER_URI=http://localhost:11311
    LOG_LEVEL=DEBUG
"""

from pathlib import Path

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

    # Tamanho máximo do buffer histórico por tópico (número de mensagens).
    # Sobrescreva via TOPIC_BUFFER_SIZE no .env.
    TOPIC_BUFFER_SIZE: int = 1000

    # Intervalo entre coletas do background recorder (segundos).
    # Valores menores aumentam a fidelidade, mas consomem mais CPU.
    # Sobrescreva via RECORD_INTERVAL no .env.
    RECORD_INTERVAL: float = 0.2

    # --- Sistema de arquivos ---
    # Diretório raiz permitido para leitura/escrita de arquivos.
    # Sobrescreva via variável de ambiente FILES_BASE_PATH ou no .env.
    # Exemplo: FILES_BASE_PATH=/home/usuario/catkin_ws
    FILES_BASE_PATH: Path = Path.home() / "catkin_ws"

    # Tamanho máximo de leitura de arquivo (bytes). Padrão: 1 MB.
    FILES_MAX_READ_BYTES: int = 1 * 1024 * 1024

    # Tamanho máximo de escrita de arquivo (bytes). Padrão: 1 MB.
    FILES_MAX_WRITE_BYTES: int = 1 * 1024 * 1024

    # --- Execução de comandos do sistema ---
    # Diretório de trabalho para execução de comandos shell.
    # Sobrescreva via SYSTEM_WORKDIR no .env.
    # Exemplo: SYSTEM_WORKDIR=/home/usuario/catkin_ws
    SYSTEM_WORKDIR: Path = Path.home() / "catkin_ws"

    # Timeout padrão em segundos para POST /run (subprocess.run bloqueante).
    SYSTEM_DEFAULT_TIMEOUT: int = 10

    # Tamanho máximo de stdout/stderr capturados (bytes). Padrão: 1 MB.
    SYSTEM_MAX_OUTPUT_BYTES: int = 1 * 1024 * 1024

    # Lista de prefixos de executáveis permitidos.
    # Somente o primeiro elemento da lista de argumentos é verificado.
    # Uma lista vazia desativa a restrição (permite tudo — não recomendado).
    # Exemplo via .env: SYSTEM_ALLOWED_COMMANDS='["rosrun","roslaunch"]'
    SYSTEM_ALLOWED_COMMANDS: list[str] = [
        "rosrun",
        "roslaunch",
        "catkin",
        "catkin_make",
        "python",
        "python3",
    ]

    # --- Logging ---
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Instância global de configurações — importe este objeto nos outros módulos.
settings = Settings()
