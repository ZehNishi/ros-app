"""
Gerenciador dinâmico de subscrições a tópicos ROS.

Permite que a API faça subscribe e unsubscribe em tempo de execução sem
reiniciar o nó ROS. Cada tópico subscrito recebe um callback que armazena
a última mensagem recebida em um buffer protegido por lock.

Características:
- Subscribe dinâmico a qualquer tópico (nome + tipo resolvido em runtime).
- Buffer thread-safe: dicionário {topic_name → última mensagem ROS bruta}.
- Múltiplos tópicos simultâneos sem interferência entre si.
- Cada subscriber roda na thread de spin do rospy (não bloqueia a API).

Pré-requisito:
    O nó ROS deve estar inicializado (ros_client.init() chamado) antes de
    qualquer chamada a subscribe().

Fluxo típico:
    ros_client.init()
    topic_manager.subscribe("/chatter")
    msg = topic_manager.get_latest("/chatter")   # None até chegar a 1ª msg
    topic_manager.unsubscribe("/chatter")
"""

import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceções do domínio TopicManager
# ---------------------------------------------------------------------------

class TopicNotSubscribedError(KeyError):
    """Levantada quando get_latest/unsubscribe é chamado para tópico não subscrito."""


class TopicSubscribeError(RuntimeError):
    """Levantada quando não é possível criar o subscriber ROS."""


# ---------------------------------------------------------------------------
# Estrutura interna de cada subscrição
# ---------------------------------------------------------------------------

@dataclass
class _Subscription:
    """Representa uma subscrição ativa a um único tópico ROS."""

    topic_name: str
    msg_type: str

    # Objeto rospy.Subscriber — preenchido após subscribe bem-sucedido.
    subscriber: Any = field(default=None, repr=False)

    # Última mensagem ROS recebida (objeto rospy bruto, sem serialização).
    latest_message: Any = field(default=None, repr=False)

    # Timestamp (rospy.Time) da última mensagem recebida.
    latest_stamp: Any = field(default=None, repr=False)

    # Contador de mensagens recebidas desde o subscribe.
    message_count: int = 0

    # Lock individual por tópico — evita race condition no callback vs leitura.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


# ---------------------------------------------------------------------------
# TopicManager
# ---------------------------------------------------------------------------

class TopicManager:
    """
    Gerencia subscrições dinâmicas a tópicos ROS.

    O manager mantém um dicionário interno de ``_Subscription`` e cria/destrói
    objetos ``rospy.Subscriber`` conforme solicitado pela camada de API.

    Thread-safety:
        - ``_registry_lock`` protege leituras/escritas no dicionário ``_registry``.
        - Cada ``_Subscription`` tem seu próprio ``lock`` para proteger o buffer
          de mensagens, permitindo que callbacks de tópicos distintos ocorram
          em paralelo sem se bloquearem.

    Uso:
        from app.ros.topic_manager import topic_manager

        topic_manager.subscribe("/chatter")
        msg = topic_manager.get_latest("/chatter")
        topic_manager.unsubscribe("/chatter")
    """

    def __init__(self) -> None:
        # Dicionário { topic_name: _Subscription }
        self._registry: dict[str, _Subscription] = {}

        # Lock para operações no _registry (subscribe/unsubscribe/list)
        self._registry_lock: threading.Lock = threading.Lock()

        logger.debug("TopicManager inicializado.")

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def subscribe(self, topic_name: str) -> None:
        """
        Faz subscribe dinâmico a um tópico ROS.

        O tipo da mensagem é resolvido automaticamente consultando o rosmaster
        via ``rostopic.get_topic_type()``. Isso significa que deve haver ao
        menos um publisher ativo no tópico no momento do subscribe.

        Se o tópico já estiver subscrito, a chamada é ignorada silenciosamente.

        Args:
            topic_name: Nome completo do tópico (ex. ``'/chatter'``).

        Raises:
            TopicSubscribeError: se rospy não estiver disponível, se o tipo do
                                 tópico não puder ser resolvido, ou se a classe
                                 de mensagem não puder ser importada.

        Exemplo:
            topic_manager.subscribe("/chatter")
            topic_manager.subscribe("/scan")        # simultâneo, sem problema
        """
        with self._registry_lock:
            if topic_name in self._registry:
                logger.debug(
                    "subscribe('%s'): já subscrito — ignorando.", topic_name
                )
                return

            logger.info("Iniciando subscribe ao tópico '%s'...", topic_name)

            # 1. Resolve o tipo da mensagem via rosmaster
            msg_type_str = self._resolve_msg_type(topic_name)
            logger.debug(
                "Tipo resolvido para '%s': %s", topic_name, msg_type_str
            )

            # 2. Importa a classe Python da mensagem (ex. std_msgs.msg.String)
            msg_class = self._import_msg_class(msg_type_str)
            logger.debug(
                "Classe de mensagem importada: %s", msg_class
            )

            # 3. Cria a entrada no registry antes do subscriber
            #    (evita race condition se o callback disparar antes do return)
            subscription = _Subscription(
                topic_name=topic_name,
                msg_type=msg_type_str,
            )
            self._registry[topic_name] = subscription

            # 4. Cria o subscriber rospy
            try:
                import rospy  # noqa: PLC0415

                def _callback(msg: Any, sub: _Subscription = subscription) -> None:
                    """Armazena a mensagem mais recente no buffer da subscrição."""
                    with sub.lock:
                        sub.latest_message = msg
                        sub.latest_stamp = rospy.Time.now()
                        sub.message_count += 1

                    logger.debug(
                        "Mensagem #%d recebida em '%s'.",
                        sub.message_count,
                        sub.topic_name,
                    )

                subscription.subscriber = rospy.Subscriber(
                    topic_name,
                    msg_class,
                    _callback,
                    queue_size=10,
                )

                logger.info(
                    "Subscribe ao tópico '%s' (%s) ativo.",
                    topic_name,
                    msg_type_str,
                )

            except Exception as exc:
                # Rollback: remove do registry para não deixar entrada inválida
                del self._registry[topic_name]
                msg = (
                    f"Falha ao criar subscriber para '{topic_name}': {exc}"
                )
                logger.error(msg)
                raise TopicSubscribeError(msg) from exc

    def get_latest(self, topic_name: str) -> Optional[Any]:
        """
        Retorna a última mensagem ROS recebida no tópico.

        A mensagem é o objeto rospy bruto (ex. ``std_msgs.msg.String``).
        Nenhuma serialização ou conversão para dict/JSON é realizada aqui.

        Args:
            topic_name: Nome completo do tópico (ex. ``'/chatter'``).

        Returns:
            Objeto de mensagem ROS ou ``None`` se nenhuma mensagem tiver
            sido recebida desde o subscribe.

        Raises:
            TopicNotSubscribedError: se o tópico não estiver subscrito.

        Exemplo:
            msg = topic_manager.get_latest("/chatter")
            if msg is not None:
                print(msg.data)    # acesso ao campo da mensagem ROS
        """
        subscription = self._get_subscription(topic_name, caller="get_latest")

        with subscription.lock:
            msg = subscription.latest_message
            count = subscription.message_count

        logger.debug(
            "get_latest('%s'): retornando mensagem #%d (None=%s).",
            topic_name,
            count,
            msg is None,
        )
        return msg

    def unsubscribe(self, topic_name: str) -> None:
        """
        Cancela o subscribe ao tópico e remove o buffer de mensagens.

        Após este método, ``get_latest(topic_name)`` levantará
        ``TopicNotSubscribedError``.

        Args:
            topic_name: Nome completo do tópico (ex. ``'/chatter'``).

        Raises:
            TopicNotSubscribedError: se o tópico não estiver subscrito.

        Exemplo:
            topic_manager.unsubscribe("/chatter")
        """
        with self._registry_lock:
            subscription = self._get_subscription(topic_name, caller="unsubscribe")

            logger.info("Cancelando subscribe ao tópico '%s'...", topic_name)

            try:
                if subscription.subscriber is not None:
                    subscription.subscriber.unregister()
                    logger.debug(
                        "rospy.Subscriber de '%s' desregistrado.", topic_name
                    )
            except Exception as exc:
                # Loga mas prossegue — queremos remover do registry de qualquer forma
                logger.warning(
                    "Erro ao desregistrar subscriber de '%s': %s",
                    topic_name,
                    exc,
                )
            finally:
                del self._registry[topic_name]
                logger.info(
                    "Tópico '%s' removido do registry.", topic_name
                )

    # ------------------------------------------------------------------
    # Métodos de inspeção
    # ------------------------------------------------------------------

    def list_subscribed(self) -> list[dict[str, Any]]:
        """
        Lista todos os tópicos atualmente subscritos com estatísticas básicas.

        Returns:
            Lista de dicts com as chaves:
                - ``topic_name`` (str)
                - ``msg_type`` (str)
                - ``message_count`` (int)
                - ``has_latest`` (bool) — True se ao menos 1 mensagem foi recebida

        Exemplo:
            for info in topic_manager.list_subscribed():
                print(info["topic_name"], info["message_count"])
        """
        with self._registry_lock:
            result = []
            for sub in self._registry.values():
                with sub.lock:
                    result.append({
                        "topic_name": sub.topic_name,
                        "msg_type": sub.msg_type,
                        "message_count": sub.message_count,
                        "has_latest": sub.latest_message is not None,
                    })

        logger.debug("list_subscribed(): %d tópico(s) ativo(s).", len(result))
        return result

    def unsubscribe_all(self) -> None:
        """
        Cancela todos os subscribes ativos.

        Útil para o teardown da aplicação (lifespan shutdown).
        """
        with self._registry_lock:
            topic_names = list(self._registry.keys())

        logger.info(
            "unsubscribe_all(): cancelando %d subscrição(ões).", len(topic_names)
        )

        for topic_name in topic_names:
            try:
                self.unsubscribe(topic_name)
            except TopicNotSubscribedError:
                pass  # pode ter sido removido por outra thread
            except Exception as exc:
                logger.warning(
                    "Erro ao cancelar subscribe de '%s': %s", topic_name, exc
                )

        logger.info("unsubscribe_all() concluído.")

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    def _get_subscription(self, topic_name: str, caller: str) -> _Subscription:
        """
        Retorna a _Subscription para o tópico ou levanta TopicNotSubscribedError.

        Não adquire _registry_lock — o chamador é responsável pelo lock
        quando necessário.
        """
        sub = self._registry.get(topic_name)
        if sub is None:
            msg = (
                f"TopicManager.{caller}(): tópico '{topic_name}' não está subscrito. "
                "Chame subscribe() primeiro."
            )
            logger.error(msg)
            raise TopicNotSubscribedError(msg)
        return sub

    @staticmethod
    def _resolve_msg_type(topic_name: str) -> str:
        """
        Consulta o rosmaster para descobrir o tipo do tópico.

        Returns:
            str: Tipo no formato 'pacote/TipoMensagem' (ex. 'std_msgs/String').

        Raises:
            TopicSubscribeError: se o tipo não puder ser resolvido.
        """
        try:
            from rostopic import get_topic_type as _get_type  # noqa: PLC0415

            topic_type, _, _ = _get_type(topic_name, blocking=False)

        except ImportError as exc:
            raise TopicSubscribeError(
                "Módulo rostopic não encontrado. ROS Noetic instalado?"
            ) from exc
        except Exception as exc:
            raise TopicSubscribeError(
                f"Erro ao resolver tipo do tópico '{topic_name}': {exc}"
            ) from exc

        if topic_type is None:
            raise TopicSubscribeError(
                f"Tipo do tópico '{topic_name}' não pôde ser resolvido. "
                "Verifique se há um publisher ativo no tópico."
            )

        return topic_type

    @staticmethod
    def _import_msg_class(msg_type_str: str) -> Any:
        """
        Importa dinamicamente a classe Python de uma mensagem ROS.

        Args:
            msg_type_str: Tipo no formato 'pacote/TipoMensagem'
                          (ex. 'std_msgs/String', 'sensor_msgs/LaserScan').

        Returns:
            A classe da mensagem (ex. ``std_msgs.msg.String``).

        Raises:
            TopicSubscribeError: se o pacote ou a classe não existirem.
        """
        try:
            # Formato esperado: "pacote/TipoMensagem"
            package, cls_name = msg_type_str.split("/", 1)
        except ValueError as exc:
            raise TopicSubscribeError(
                f"Formato de tipo inválido: '{msg_type_str}'. "
                "Esperado 'pacote/TipoMensagem'."
            ) from exc

        try:
            # rospy usa a convenção: <pacote>.msg.<TipoMensagem>
            import importlib  # noqa: PLC0415

            module = importlib.import_module(f"{package}.msg")
            msg_class = getattr(module, cls_name)
            return msg_class

        except (ImportError, AttributeError) as exc:
            raise TopicSubscribeError(
                f"Não foi possível importar '{package}.msg.{cls_name}'. "
                "Verifique se o pacote ROS correspondente está instalado."
            ) from exc


# ---------------------------------------------------------------------------
# Instância singleton — importe este objeto nos outros módulos.
# ---------------------------------------------------------------------------

topic_manager = TopicManager()
