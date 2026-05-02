"""
Gerenciamento do nó ROS principal.

Responsável por:
- Inicializar o nó rospy (rospy.init_node)
- Manter referências a publishers e subscribers
- Expor métodos de start/stop para o lifespan do FastAPI

Pré-requisito: ROS Noetic instalado e `source /opt/ros/noetic/setup.bash` executado
antes de iniciar a aplicação.
"""

# import rospy  # descomente quando o ambiente ROS estiver disponível

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class ROSNode:
    """
    Encapsula o ciclo de vida do nó ROS.

    Separa a lógica ROS do restante da aplicação para facilitar testes
    e para que a API funcione mesmo sem ROS disponível (modo mock).
    """

    def __init__(self) -> None:
        self._initialized = False

    def start(self) -> None:
        """
        Inicializa o nó ROS.

        Chame este método no lifespan de startup do FastAPI.
        Substitua o comentário abaixo pela chamada real ao rospy.
        """
        logger.info("Inicializando nó ROS '%s'...", settings.ROS_NODE_NAME)

        # TODO: inicializar o nó ROS
        # rospy.init_node(settings.ROS_NODE_NAME, anonymous=False, disable_signals=True)

        self._initialized = True
        logger.info("Nó ROS inicializado com sucesso.")

    def stop(self) -> None:
        """
        Desliga o nó ROS de forma segura.

        Chame este método no lifespan de shutdown do FastAPI.
        """
        if not self._initialized:
            return

        logger.info("Desligando nó ROS...")

        # TODO: encerrar o nó ROS
        # rospy.signal_shutdown("FastAPI shutting down")

        self._initialized = False
        logger.info("Nó ROS encerrado.")

    @property
    def is_running(self) -> bool:
        """Retorna True se o nó estiver ativo."""
        return self._initialized
        # TODO: substituir por: return not rospy.is_shutdown()


# Instância singleton — importe este objeto nos outros módulos ROS.
ros_node = ROSNode()
