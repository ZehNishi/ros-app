"""
Publishers e Subscribers dos tópicos ROS.

Cada tópico deve ter:
- Uma função ou classe de publisher para enviar mensagens.
- Uma função de callback para receber mensagens.

Organize um tópico por função/classe para manter a legibilidade.

Pré-requisito: o nó ROS já deve estar inicializado (ros_node.start() chamado).
"""

# import rospy
# from std_msgs.msg import String, Float64
# from sensor_msgs.msg import Image, LaserScan

from app.core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Exemplo: tópico /chatter (std_msgs/String)
# ---------------------------------------------------------------------------

# publisher_chatter = None  # instância do rospy.Publisher


def setup_chatter_publisher():
    """
    Cria o publisher para o tópico /chatter.

    Chame esta função após ros_node.start().
    """
    # global publisher_chatter
    # publisher_chatter = rospy.Publisher("/chatter", String, queue_size=10)
    logger.info("Publisher /chatter configurado.")


def publish_chatter(message: str) -> None:
    """
    Publica uma mensagem no tópico /chatter.

    Args:
        message: Texto a ser publicado.
    """
    # if publisher_chatter is None:
    #     raise RuntimeError("Publisher /chatter não inicializado.")
    # publisher_chatter.publish(String(data=message))
    logger.debug("Publicando em /chatter: %s", message)


def chatter_callback(msg) -> None:
    """
    Callback chamado ao receber uma mensagem em /chatter.

    Args:
        msg: Objeto std_msgs/String recebido do tópico.
    """
    # logger.info("Recebido em /chatter: %s", msg.data)
    pass


# ---------------------------------------------------------------------------
# Adicione novos tópicos abaixo seguindo o mesmo padrão
# ---------------------------------------------------------------------------
