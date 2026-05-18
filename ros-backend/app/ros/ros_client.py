"""
Cliente ROS de alto nível.

Fornece uma interface limpa para operações ROS usadas pela camada de API,
isolando completamente as chamadas rospy do restante da aplicação.

Características:
- Inicialização lazy: o nó ROS só é criado na primeira chamada que precisar dele.
- Thread-safe: usa threading.Lock para proteger a inicialização concorrente.
- Graceful degradation: levanta ROSUnavailableError quando o ambiente ROS
  não está disponível, permitindo que a API retorne erros HTTP claros.

Pré-requisito:
    source /opt/ros/noetic/setup.bash
    roscore   # em outro terminal
"""

from __future__ import annotations

import threading
from typing import List, Optional, Tuple

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceções do domínio ROS
# ---------------------------------------------------------------------------

class ROSUnavailableError(RuntimeError):
    """
    Levantada quando o ambiente ROS não está acessível.

    Causas comuns:
    - rospy não está instalado (ROS Noetic não instalado).
    - roscore não está em execução.
    - ROS_MASTER_URI aponta para o endereço errado.
    """


class ROSNotInitializedError(RuntimeError):
    """Levantada quando um método é chamado antes de init() ter sido executado."""


# ---------------------------------------------------------------------------
# RosClient
# ---------------------------------------------------------------------------

class RosClient:
    """
    Cliente singleton para operações ROS via rospy.

    Uso típico
    ----------
    O objeto ``ros_client`` já está instanciado no final deste módulo.
    Importe-o diretamente:

        from app.ros.ros_client import ros_client

        ros_client.init()            # idempotente — pode chamar várias vezes
        topics = ros_client.get_topics()

    Ciclo de vida
    -------------
    - ``init()``     → inicializa o nó rospy (lazy, thread-safe).
    - ``shutdown()`` → desliga o nó corretamente; necessário no teardown da app.
    - ``is_ready``   → propriedade que indica se o nó está ativo.
    """

    def __init__(self, node_name: Optional[str] = None) -> None:
        self._node_name: str = node_name or settings.ROS_NODE_NAME
        self._initialized: bool = False
        self._lock: threading.Lock = threading.Lock()
        logger.debug(
            "RosClient criado (node_name='%s'). Ainda não inicializado.",
            self._node_name,
        )

    # ------------------------------------------------------------------
    # Inicialização e encerramento
    # ------------------------------------------------------------------

    def init(self) -> None:
        """
        Inicializa o nó rospy (lazy).

        Seguro para chamar múltiplas vezes — executa apenas uma vez.
        Deve ser chamado antes de qualquer outro método.

        Raises:
            ROSUnavailableError: se rospy não estiver instalado ou se
                                 o roscore não estiver acessível.
        """
        with self._lock:
            if self._initialized:
                logger.debug(
                    "init() chamado novamente — nó '%s' já está ativo. Ignorando.",
                    self._node_name,
                )
                return

            logger.info(
                "Inicializando nó ROS '%s' (ROS_MASTER_URI=%s)...",
                self._node_name,
                settings.ROS_MASTER_URI,
            )

            try:
                import rospy  # noqa: PLC0415 — importação intencional lazy

                # disable_signals=True é necessário quando rospy roda dentro
                # de uma thread (e.g. uvicorn já registra seus próprios sinais).
                rospy.init_node(
                    self._node_name,
                    anonymous=False,
                    disable_signals=True,
                )
                self._initialized = True
                logger.info("Nó ROS '%s' inicializado com sucesso.", self._node_name)

            except ImportError as exc:
                msg = (
                    "rospy não encontrado. Certifique-se de que o ROS Noetic está "
                    "instalado e que você executou: "
                    "source /opt/ros/noetic/setup.bash"
                )
                logger.error(msg)
                raise ROSUnavailableError(msg) from exc

            except Exception as exc:
                msg = (
                    f"Falha ao inicializar o nó ROS '{self._node_name}'. "
                    f"Verifique se o roscore está rodando em {settings.ROS_MASTER_URI}. "
                    f"Erro original: {exc}"
                )
                logger.error(msg)
                raise ROSUnavailableError(msg) from exc

    def shutdown(self) -> None:
        """
        Desliga o nó ROS de forma segura.

        Deve ser chamado no lifespan de shutdown do FastAPI.
        Seguro para chamar mesmo se init() nunca foi executado.
        """
        with self._lock:
            if not self._initialized:
                logger.debug("shutdown() chamado, mas o nó ainda não foi inicializado.")
                return

            logger.info("Desligando nó ROS '%s'...", self._node_name)

            try:
                import rospy  # noqa: PLC0415

                rospy.signal_shutdown("RosClient.shutdown() chamado — aplicação encerrando.")
                logger.info("Nó ROS '%s' encerrado.", self._node_name)

            except Exception as exc:
                logger.warning(
                    "Erro ao encerrar o nó ROS '%s': %s", self._node_name, exc
                )

            finally:
                self._initialized = False

    # ------------------------------------------------------------------
    # Propriedades de estado
    # ------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        """
        Retorna True se o nó foi inicializado e o roscore está acessível.

        Consulta rospy.is_shutdown() para detectar desconexões inesperadas.
        """
        if not self._initialized:
            return False

        try:
            import rospy  # noqa: PLC0415

            running = not rospy.is_shutdown()
            if not running:
                logger.warning(
                    "rospy.is_shutdown() retornou True — nó '%s' foi desligado externamente.",
                    self._node_name,
                )
            return running

        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Métodos de consulta ao master ROS
    # ------------------------------------------------------------------

    def get_topics(self) -> List[Tuple[str, str]]:
        """
        Retorna a lista de tópicos publicados ativos no roscore.

        Cada item é uma tupla ``(nome_do_tópico, tipo_da_mensagem)``.

        Returns:
            List[Tuple[str, str]]: ex. [('/chatter', 'std_msgs/String'), ...]

        Raises:
            ROSNotInitializedError: se init() não foi chamado antes.
            ROSUnavailableError: se a consulta ao master falhar.

        Exemplo:
            >>> topics = ros_client.get_topics()
            >>> for name, msg_type in topics:
            ...     print(name, msg_type)
        """
        self._assert_ready("get_topics")
        logger.debug("Consultando lista de tópicos no roscore...")

        try:
            import rospy  # noqa: PLC0415

            # get_published_topics() retorna List[List[str, str]]
            raw: List[List[str]] = rospy.get_published_topics()
            topics: List[Tuple[str, str]] = [(name, t) for name, t in raw]

            logger.info(
                "get_topics(): %d tópico(s) encontrado(s).",
                len(topics),
            )
            logger.debug("Tópicos: %s", topics)
            return topics

        except Exception as exc:
            msg = f"Falha ao obter lista de tópicos: {exc}"
            logger.error(msg)
            raise ROSUnavailableError(msg) from exc

    def get_topic_type(self, topic_name: str) -> str:
        """
        Retorna o tipo da mensagem de um tópico específico.

        Usa ``rostopic.get_topic_type`` que consulta o rosmaster diretamente,
        sem precisar de um subscriber ativo.

        Args:
            topic_name: Nome completo do tópico (ex. ``'/chatter'``).

        Returns:
            str: Tipo da mensagem (ex. ``'std_msgs/String'``).

        Raises:
            ROSNotInitializedError: se init() não foi chamado antes.
            ROSUnavailableError: se o roscore não responder.
            ValueError: se o tópico não existir ou não tiver publishers ativos.

        Exemplo:
            >>> msg_type = ros_client.get_topic_type('/chatter')
            >>> print(msg_type)  # 'std_msgs/String'
        """
        self._assert_ready("get_topic_type")
        logger.debug("Consultando tipo do tópico '%s'...", topic_name)

        try:
            from rostopic import get_topic_type as _get_topic_type  # noqa: PLC0415

            # _get_topic_type retorna (tipo, nome_real, fn_avaliação)
            # O terceiro elemento é uma função de avaliação de campo — ignoramos.
            topic_type, real_topic, _ = _get_topic_type(topic_name, blocking=False)

        except ImportError as exc:
            msg = (
                "Módulo rostopic não encontrado. "
                "Verifique se o ROS Noetic está instalado corretamente."
            )
            logger.error(msg)
            raise ROSUnavailableError(msg) from exc

        except Exception as exc:
            msg = f"Erro ao consultar o tipo do tópico '{topic_name}': {exc}"
            logger.error(msg)
            raise ROSUnavailableError(msg) from exc

        if topic_type is None:
            msg = (
                f"Tópico '{topic_name}' não encontrado ou sem publishers ativos. "
                "Verifique se o nome está correto e se há um nó publicando nele."
            )
            logger.warning(msg)
            raise ValueError(msg)

        logger.info(
            "get_topic_type('%s'): tipo='%s' (nome real='%s').",
            topic_name,
            topic_type,
            real_topic,
        )
        return topic_type

    def get_message_fields(self, msg_type: str) -> List[str]:
        """
        Retorna a lista de campos (schema) em notação de ponto para um tipo de mensagem.

        Inspeciona recursivamente os tipos aninhados usando roslib.message.

        Args:
            msg_type: Tipo da mensagem (ex. ``'geometry_msgs/Twist'``).

        Returns:
            List[str]: Lista de caminhos de dados possíveis (ex. ``['linear.x', 'linear.y', ...]``).
        """
        self._assert_ready("get_message_fields")
        
        try:
            import roslib.message  # noqa: PLC0415
        except ImportError as exc:
            msg = "Módulo roslib não encontrado."
            logger.error(msg)
            raise ROSUnavailableError(msg) from exc

        def _get_fields(m_type: str, prefix: str = "") -> List[str]:
            cls = roslib.message.get_message_class(m_type)
            if cls is None:
                return [prefix] if prefix else []
            
            fields = []
            if hasattr(cls, "__slots__") and hasattr(cls, "_slot_types"):
                for slot_name, slot_type in zip(cls.__slots__, cls._slot_types):
                    slot_path = f"{prefix}.{slot_name}" if prefix else slot_name
                    
                    # Ignora matrizes/arrays (ex: float64[9]), pois ainda não são suportados
                    # nos gráficos e causam sobrecarga de conexões SSE.
                    if "[" in slot_type:
                        continue
                        
                    base_type = slot_type.split("[")[0]
                    
                    if "/" in base_type:
                        fields.extend(_get_fields(base_type, slot_path))
                    elif base_type in ("time", "duration"):
                        fields.extend([f"{slot_path}.secs", f"{slot_path}.nsecs", f"{slot_path}.total_seconds"])
                    else:
                        fields.append(slot_path)
            else:
                if prefix:
                    fields.append(prefix)
            return fields

        return _get_fields(msg_type)

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    def _assert_ready(self, method_name: str) -> None:
        """
        Garante que o nó está inicializado antes de prosseguir.

        Raises:
            ROSNotInitializedError: se init() não foi chamado.
        """
        if not self._initialized:
            msg = (
                f"RosClient.{method_name}() chamado antes de init(). "
                "Chame ros_client.init() durante o startup da aplicação."
            )
            logger.error(msg)
            raise ROSNotInitializedError(msg)


# ---------------------------------------------------------------------------
# Instância singleton — importe este objeto nos outros módulos.
# ---------------------------------------------------------------------------

ros_client = RosClient()
