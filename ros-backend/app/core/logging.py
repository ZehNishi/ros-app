"""
Configuração centralizada de logging.

Usa o módulo padrão `logging` do Python.
Chame `get_logger(__name__)` em qualquer módulo para obter um logger nomeado.
"""

import logging
import sys

from app.core.config import settings


def configure_logging() -> None:
    """Configura o handler raiz uma única vez na inicialização."""
    logging.basicConfig(
        level=settings.LOG_LEVEL.upper(),
        format="%(asctime)s | %(levelname)-8s | %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def get_logger(name: str) -> logging.Logger:
    """Retorna um logger nomeado para o módulo chamador."""
    configure_logging()
    return logging.getLogger(name)
