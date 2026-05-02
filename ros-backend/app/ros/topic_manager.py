"""
Gerenciador dinâmico de subscrições a tópicos ROS.

Permite que a API faça subscribe e unsubscribe em tempo de execução sem
reiniciar o nó ROS. Cada tópico subscrito mantém:

- ``latest_message`` — referência direta à última mensagem recebida (acesso O(1)).
- ``history``        — deque circular com até ``TOPIC_BUFFER_SIZE`` entradas,
                       cada uma no formato ``{"timestamp": float, "msg": <rospy msg>}``.

Características:
- Subscribe dinâmico a qualquer tópico (nome + tipo resolvido em runtime).
- Buffer histórico thread-safe por tópico usando collections.deque.
- Múltiplos tópicos simultâneos sem interferência entre si.
- Cada subscriber roda na thread de spin do rospy (não bloqueia a API).
- Mensagens ROS armazenadas brutas — sem serialização neste módulo.

Pré-requisito:
    O nó ROS deve estar inicializado (ros_client.init() chamado) antes de
    qualquer chamada a subscribe().

Fluxo típico:
    ros_client.init()
    topic_manager.subscribe("/chatter")
    msg   = topic_manager.get_latest("/chatter")      # última msg ou None
    hist  = topic_manager.get_history("/chatter", limit=10)  # últimas 10
    topic_manager.unsubscribe("/chatter")
"""

from __future__ import annotations

import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceções do domínio TopicManager
# ---------------------------------------------------------------------------

class TopicNotSubscribedError(KeyError):
    """Levantada quando get_latest/get_history/unsubscribe é chamado para tópico não subscrito."""


class TopicSubscribeError(RuntimeError):
    """Levantada quando não é possível criar o subscriber ROS."""


# ---------------------------------------------------------------------------
# Estrutura interna de cada subscrição
# ---------------------------------------------------------------------------

@dataclass
class _Subscription:
    """
    Representa uma subscrição ativa a um único tópico ROS.

    Buffer histórico:
        ``history`` é uma ``deque`` com ``maxlen=TOPIC_BUFFER_SIZE``.
        Quando cheia, a entrada mais antiga é descartada automaticamente
        (comportamento nativo do deque) sem necessidade de lógica extra.

        Cada entrada é um dict:
            {"timestamp": float,   # time.time() no momento do callback
             "msg":       Any}     # objeto rospy bruto

    ``latest_message`` é mantido como atalho de acesso O(1) à última
    mensagem, evitando indexação na deque a cada chamada a get_latest().
    """

    topic_name: str
    msg_type: str

    # Objeto rospy.Subscriber — preenchido após subscribe bem-sucedido.
    subscriber: Any = field(default=None, repr=False)

    # Atalho para a última mensagem recebida — atualizado junto com history.
    latest_message: Any = field(default=None, repr=False)

    # Timestamp da última mensagem (time.time()).
    latest_stamp: Optional[float] = field(default=None, repr=False)

    # Contador de mensagens recebidas desde o subscribe.
    message_count: int = 0

    # Buffer histórico circular — maxlen definido em runtime pelo __post_init__.
    history: deque = field(default_factory=deque, repr=False)

    # Lock individual por tópico — protege latest_message, history e contadores.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        # Recria o deque com o maxlen correto (settings pode ter sido alterado
        # entre imports; usando no momento da instanciação garante consistência).
        self.history = deque(maxlen=settings.TOPIC_BUFFER_SIZE)


# ---------------------------------------------------------------------------
# TopicManager
# ---------------------------------------------------------------------------

class TopicManager:
    """
    Gerencia subscrições dinâmicas a tópicos ROS com buffer histórico.

    Thread-safety:
        - ``_registry_lock`` protege leituras/escritas no dicionário ``_registry``.
        - Cada ``_Subscription`` tem seu próprio ``lock`` para proteger o buffer
          e latest_message, permitindo que callbacks de tópicos distintos ocorram
          em paralelo sem se bloquearem.

    Uso:
        from app.ros.topic_manager import topic_manager

        topic_manager.subscribe("/chatter")
        msg  = topic_manager.get_latest("/chatter")
        hist = topic_manager.get_history("/chatter", limit=5)
        topic_manager.unsubscribe("/chatter")
    """

    def __init__(self) -> None:
        # Dicionário { topic_name: _Subscription }
        self._registry: Dict[str, _Subscription] = {}

        # Lock para operações no _registry (subscribe/unsubscribe/list)
        self._registry_lock: threading.Lock = threading.Lock()

        logger.debug(
            "TopicManager inicializado (TOPIC_BUFFER_SIZE=%d).",
            settings.TOPIC_BUFFER_SIZE,
        )

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
            topic_manager.subscribe("/scan")    # simultâneo, sem problema
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
            logger.debug("Tipo resolvido para '%s': %s", topic_name, msg_type_str)

            # 2. Importa a classe Python da mensagem (ex. std_msgs.msg.String)
            msg_class = self._import_msg_class(msg_type_str)
            logger.debug("Classe de mensagem importada: %s", msg_class)

            # 3. Cria a entrada no registry antes do subscriber
            #    (evita race condition se o callback disparar antes do return)
            subscription = _Subscription(
                topic_name=topic_name,
                msg_type=msg_type_str,
            )
            self._registry[topic_name] = subscription

            # 4. Cria o subscriber rospy com callback que alimenta o buffer
            try:
                import rospy  # noqa: PLC0415

                def _callback(msg: Any, sub: _Subscription = subscription) -> None:
                    """
                    Callback do subscriber rospy.

                    Executa na thread de spin do rospy. Atualiza atomicamente:
                    - ``sub.latest_message`` — atalho para a última msg
                    - ``sub.latest_stamp``   — timestamp da chegada
                    - ``sub.history``        — deque circular com histórico
                    - ``sub.message_count``  — contador total
                    """
                    ts = time.time()
                    with sub.lock:
                        sub.latest_message = msg
                        sub.latest_stamp = ts
                        sub.message_count += 1
                        sub.history.append({"timestamp": ts, "msg": msg})

                    logger.debug(
                        "Mensagem #%d recebida em '%s' (buffer=%d/%d).",
                        sub.message_count,
                        sub.topic_name,
                        len(sub.history),
                        sub.history.maxlen,
                    )

                subscription.subscriber = rospy.Subscriber(
                    topic_name,
                    msg_class,
                    _callback,
                    queue_size=10,
                )

                logger.info(
                    "Subscribe ao tópico '%s' (%s) ativo (buffer_size=%d).",
                    topic_name,
                    msg_type_str,
                    settings.TOPIC_BUFFER_SIZE,
                )

            except Exception as exc:
                # Rollback: remove do registry para não deixar entrada inválida
                del self._registry[topic_name]
                raise TopicSubscribeError(
                    f"Falha ao criar subscriber para '{topic_name}': {exc}"
                ) from exc

    def get_latest(self, topic_name: str) -> Optional[Any]:
        """
        Retorna a última mensagem ROS recebida no tópico.

        Acesso O(1) via ``latest_message`` — não percorre o buffer histórico.
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
                print(msg.data)
        """
        subscription = self._get_subscription(topic_name, caller="get_latest")

        with subscription.lock:
            msg = subscription.latest_message
            count = subscription.message_count

        logger.debug(
            "get_latest('%s'): msg #%d (None=%s).",
            topic_name,
            count,
            msg is None,
        )
        return msg

    def get_history(
        self,
        topic_name: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Retorna o histórico de mensagens recebidas no tópico.

        Cada entrada é um dicionário com:
            - ``"timestamp"`` (float) — ``time.time()`` do momento do callback.
            - ``"msg"``       (Any)   — objeto rospy bruto, sem serialização.

        As entradas são retornadas em ordem cronológica (mais antiga primeiro).
        O buffer armazena no máximo ``settings.TOPIC_BUFFER_SIZE`` entradas;
        mensagens mais antigas são descartadas automaticamente quando cheio.

        Args:
            topic_name: Nome completo do tópico (ex. ``'/chatter'``).
            limit:      Se informado, retorna apenas os últimos ``limit``
                        elementos. ``None`` retorna todo o buffer.

        Returns:
            Lista de dicts ``[{"timestamp": float, "msg": <rospy msg>}, ...]``.
            Lista vazia se nenhuma mensagem foi recebida ainda.

        Raises:
            TopicNotSubscribedError: se o tópico não estiver subscrito.
            ValueError: se ``limit`` for menor que 1.

        Exemplos:
            # Todas as mensagens no buffer
            history = topic_manager.get_history("/scan")

            # Últimas 10 mensagens
            recent = topic_manager.get_history("/scan", limit=10)

            # Acessar timestamp e msg de cada entrada
            for entry in recent:
                print(entry["timestamp"], entry["msg"].data)
        """
        if limit is not None and limit < 1:
            raise ValueError(f"limit deve ser >= 1, recebido: {limit}")

        subscription = self._get_subscription(topic_name, caller="get_history")

        with subscription.lock:
            if limit is None:
                snapshot = list(subscription.history)
            else:
                # deque não suporta slicing negativo diretamente;
                # itertools.islice na direção inversa seria O(n), então
                # convertemos apenas a fatia necessária.
                snapshot = list(subscription.history)[-limit:]
            count = subscription.message_count

        logger.debug(
            "get_history('%s'): retornando %d/%d entrada(s) (total recebido=%d).",
            topic_name,
            len(snapshot),
            settings.TOPIC_BUFFER_SIZE,
            count,
        )
        return snapshot

    def unsubscribe(self, topic_name: str) -> None:
        """
        Cancela o subscribe ao tópico e descarta o buffer histórico.

        Após este método, ``get_latest()`` e ``get_history()`` levantarão
        ``TopicNotSubscribedError``.

        Args:
            topic_name: Nome completo do tópico (ex. ``'/chatter'``).

        Raises:
            TopicNotSubscribedError: se o tópico não estiver subscrito.
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
                logger.warning(
                    "Erro ao desregistrar subscriber de '%s': %s",
                    topic_name,
                    exc,
                )
            finally:
                del self._registry[topic_name]
                logger.info("Tópico '%s' removido do registry.", topic_name)

    # ------------------------------------------------------------------
    # Métodos de inspeção
    # ------------------------------------------------------------------

    def list_subscribed(self) -> List[Dict[str, Any]]:
        """
        Lista todos os tópicos atualmente subscritos com estatísticas básicas.

        Returns:
            Lista de dicts com as chaves:
                - ``topic_name``    (str)  — nome completo do tópico.
                - ``msg_type``      (str)  — tipo da mensagem ROS.
                - ``message_count`` (int)  — total recebido desde o subscribe.
                - ``has_latest``    (bool) — True se ao menos 1 msg foi recebida.
                - ``buffer_size``   (int)  — entradas atualmente no buffer.
                - ``buffer_max``    (int)  — capacidade máxima do buffer.
        """
        with self._registry_lock:
            result = []
            for sub in self._registry.values():
                with sub.lock:
                    result.append({
                        "topic_name":    sub.topic_name,
                        "msg_type":      sub.msg_type,
                        "message_count": sub.message_count,
                        "has_latest":    sub.latest_message is not None,
                        "buffer_size":   len(sub.history),
                        "buffer_max":    sub.history.maxlen,
                    })

        logger.debug("list_subscribed(): %d tópico(s) ativo(s).", len(result))
        return result

    def unsubscribe_all(self) -> None:
        """
        Cancela todos os subscribes ativos e descarta todos os buffers.

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
            package, cls_name = msg_type_str.split("/", 1)
        except ValueError as exc:
            raise TopicSubscribeError(
                f"Formato de tipo inválido: '{msg_type_str}'. "
                "Esperado 'pacote/TipoMensagem'."
            ) from exc

        try:
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
