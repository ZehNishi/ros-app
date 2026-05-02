"""
Gravador de dados de tópicos ROS em formato CSV.

Permite iniciar/parar uma sessão de gravação, coletar mensagens do buffer
do TopicManager e persistir os dados em arquivos CSV — um por tópico.

Fluxo típico:
    recorder.start_recording(["/chatter", "/scan"])

    # Em loop periódico (e.g. a cada segundo):
    recorder.record_from_buffer(topic_manager)

    recorder.stop_recording()
    paths = recorder.save_to_csv("output/recordings")

Formato do CSV:
    timestamp,campo1,campo2,...
    1714000000.1,hello,0
    1714000000.5,world,1

- A primeira coluna é sempre o timestamp Unix (time.time()) da chegada da msg.
- As colunas seguintes são os campos planos da mensagem ROS convertida
  (via convert_ros_message). Campos nested são aplanados com notação de ponto
  (ex: header.stamp.secs).
- Valores que não puderem ser representados como scalar são convertidos via str().

Thread-safety:
    Um único threading.Lock por instância protege ``recording``, ``topics``
    e ``_data``. O lock é adquirido o mínimo necessário para evitar bloquear
    callbacks de longa duração durante saves.
"""

import csv
import threading
from pathlib import Path
from typing import Any, Optional

from app.core.logging import get_logger
from app.ros.message_converter import convert_ros_message

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# DataRecorder
# ---------------------------------------------------------------------------

class DataRecorder:
    """
    Grava mensagens ROS em memória e exporta para CSV.

    Atributos públicos (somente leitura via propriedades):
        recording  — True se uma sessão de gravação está ativa.
        topics     — Lista de tópicos sendo gravados na sessão atual.

    Uso:
        from app.ros.data_recorder import data_recorder
        from app.ros.topic_manager import topic_manager

        data_recorder.start_recording(["/chatter"])
        data_recorder.record_from_buffer(topic_manager)
        paths = data_recorder.save_to_csv("recordings/")
        data_recorder.stop_recording()
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()

        self._recording: bool = False
        self._topics: list[str] = []

        # { topic_name: list[{"timestamp": float, "msg_dict": dict}] }
        self._data: dict[str, list[dict[str, Any]]] = {}

        # Rastreia o último timestamp gravado por tópico para deduplicação.
        # { topic_name: float }
        self._last_timestamps: dict[str, float] = {}

        logger.debug("DataRecorder inicializado.")

    # ------------------------------------------------------------------
    # Propriedades de estado (somente leitura, thread-safe)
    # ------------------------------------------------------------------

    @property
    def recording(self) -> bool:
        """True se uma sessão de gravação está ativa."""
        with self._lock:
            return self._recording

    @property
    def topics(self) -> list[str]:
        """Cópia da lista de tópicos gravados na sessão atual."""
        with self._lock:
            return list(self._topics)

    # ------------------------------------------------------------------
    # Controle de sessão
    # ------------------------------------------------------------------

    def start_recording(self, topics: list[str]) -> None:
        """
        Inicia uma nova sessão de gravação para os tópicos informados.

        Inicializa buffers vazios para cada tópico e reseta os rastreadores
        de timestamp para evitar duplicação de entradas de sessões anteriores.

        Se já houver uma sessão ativa, ela é descartada e substituída pela nova.

        Args:
            topics: Lista de nomes de tópicos ROS (ex. ["/chatter", "/scan"]).

        Raises:
            ValueError: Se a lista de tópicos estiver vazia.

        Exemplo:
            recorder.start_recording(["/chatter", "/scan"])
        """
        if not topics:
            raise ValueError("A lista de tópicos não pode ser vazia.")

        with self._lock:
            if self._recording:
                logger.warning(
                    "start_recording() chamado com sessão ativa — "
                    "descartando sessão anterior (%d tópico(s), %d entrada(s) total).",
                    len(self._topics),
                    sum(len(v) for v in self._data.values()),
                )

            self._topics = list(topics)
            self._data = {t: [] for t in topics}
            self._last_timestamps = {t: -1.0 for t in topics}
            self._recording = True

            logger.info(
                "Gravação iniciada para %d tópico(s): %s",
                len(topics),
                topics,
            )

    def stop_recording(self) -> None:
        """
        Para a sessão de gravação ativa.

        Os dados já gravados em memória são mantidos e podem ser exportados
        via ``save_to_csv()`` após chamar este método.

        Se nenhuma sessão estiver ativa, a chamada é ignorada silenciosamente.
        """
        with self._lock:
            if not self._recording:
                logger.debug("stop_recording() chamado sem sessão ativa — ignorando.")
                return

            total = sum(len(v) for v in self._data.values())
            self._recording = False

            logger.info(
                "Gravação encerrada. %d entrada(s) em memória para %d tópico(s).",
                total,
                len(self._topics),
            )

    # ------------------------------------------------------------------
    # Coleta de dados
    # ------------------------------------------------------------------

    def record_from_buffer(self, topic_manager: Any) -> dict[str, int]:
        """
        Copia as novas mensagens do buffer do TopicManager para o armazenamento
        interno, evitando duplicação pelo timestamp.

        Apenas mensagens com ``timestamp > last_recorded_timestamp`` são
        copiadas. Isso permite chamar este método repetidamente em loop
        sem duplicar entradas.

        Este método não bloqueia os callbacks do rospy — o lock é adquirido
        apenas para leitura da lista de tópicos e para escrita nas listas
        internas, não durante a conversão das mensagens.

        Args:
            topic_manager: Instância de TopicManager com histórico de mensagens.

        Returns:
            dict[str, int]: Número de novas entradas copiadas por tópico.
                            Exemplo: {"/chatter": 3, "/scan": 0}

        Raises:
            RuntimeError: Se chamado sem sessão de gravação ativa.

        Exemplo:
            counts = recorder.record_from_buffer(topic_manager)
            print(counts)  # {"/chatter": 5, "/scan": 2}
        """
        with self._lock:
            if not self._recording:
                raise RuntimeError(
                    "record_from_buffer() chamado sem sessão ativa. "
                    "Chame start_recording() primeiro."
                )
            topics_snapshot = list(self._topics)

        new_counts: dict[str, int] = {t: 0 for t in topics_snapshot}

        for topic in topics_snapshot:
            try:
                history = topic_manager.get_history(topic)
            except Exception as exc:
                logger.warning(
                    "record_from_buffer(): erro ao obter histórico de '%s': %s",
                    topic, exc,
                )
                continue

            if not history:
                continue

            # Filtra apenas entradas mais novas que o último timestamp gravado
            with self._lock:
                last_ts = self._last_timestamps.get(topic, -1.0)

            new_entries = [e for e in history if e["timestamp"] > last_ts]

            if not new_entries:
                logger.debug(
                    "record_from_buffer('%s'): sem novas entradas desde ts=%.3f.",
                    topic, last_ts,
                )
                continue

            # Converte as mensagens fora do lock para não bloquear outras threads
            converted_entries: list[dict[str, Any]] = []
            for entry in new_entries:
                try:
                    msg_dict = convert_ros_message(entry["msg"], include_meta=False)
                except Exception as exc:
                    logger.warning(
                        "record_from_buffer('%s'): erro ao converter msg ts=%.3f: %s",
                        topic, entry["timestamp"], exc,
                    )
                    msg_dict = {"_error": str(exc)}

                converted_entries.append({
                    "timestamp": entry["timestamp"],
                    "msg_dict": msg_dict,
                })

            # Grava no buffer interno com lock mínimo
            with self._lock:
                if topic in self._data:
                    self._data[topic].extend(converted_entries)
                    self._last_timestamps[topic] = converted_entries[-1]["timestamp"]
                    new_counts[topic] = len(converted_entries)

            logger.debug(
                "record_from_buffer('%s'): %d nova(s) entrada(s) gravada(s).",
                topic, len(converted_entries),
            )

        total_new = sum(new_counts.values())
        if total_new:
            logger.info(
                "record_from_buffer(): %d nova(s) entrada(s) total — %s",
                total_new, new_counts,
            )

        return new_counts

    # ------------------------------------------------------------------
    # Exportação
    # ------------------------------------------------------------------

    def save_to_csv(self, output_dir: str) -> dict[str, str]:
        """
        Exporta os dados gravados para arquivos CSV — um arquivo por tópico.

        Formato do CSV:
            timestamp,campo1,campo2,...
            1714000000.1,hello,0
            1714000000.5,world,1

        - A primeira coluna é sempre o ``timestamp`` Unix.
        - As demais colunas são os campos planos da mensagem convertida.
          Campos aninhados (dicts/listas) são aplanados via ``_flatten_dict()``.
        - O cabeçalho é determinado pela união de todos os campos encontrados
          nas mensagens do tópico (garante compatibilidade mesmo se campos
          variarem entre mensagens do mesmo tópico).
        - Campos ausentes em uma mensagem ficam vazios na linha correspondente.
        - Cria o diretório automaticamente se não existir.

        Args:
            output_dir: Caminho do diretório de saída (absoluto ou relativo).

        Returns:
            dict[str, str]: Mapeamento { topic_name: caminho_do_arquivo }.
                            Tópicos sem dados não geram arquivo.

        Raises:
            RuntimeError: Se não houver dados gravados (start_recording nunca
                          foi chamado ou nenhuma mensagem foi coletada).

        Exemplo:
            paths = recorder.save_to_csv("recordings/session_01")
            # {"/ chatter": "recordings/session_01/chatter.csv"}
        """
        with self._lock:
            if not self._topics:
                raise RuntimeError(
                    "Nenhuma sessão de gravação foi iniciada. "
                    "Chame start_recording() antes de save_to_csv()."
                )
            # Copia para fora do lock — save pode ser demorado
            data_snapshot: dict[str, list] = {
                t: list(entries) for t, entries in self._data.items()
            }
            topics_snapshot = list(self._topics)

        out_path = Path(output_dir).expanduser().resolve()
        out_path.mkdir(parents=True, exist_ok=True)
        logger.info("save_to_csv(): diretório de saída: %s", out_path)

        saved_paths: dict[str, str] = {}

        for topic in topics_snapshot:
            entries = data_snapshot.get(topic, [])

            if not entries:
                logger.info("save_to_csv(): tópico '%s' sem dados — ignorado.", topic)
                continue

            # Nome do arquivo: remove "/" iniciais e substitui "/" por "_"
            filename = topic.lstrip("/").replace("/", "_") + ".csv"
            file_path = out_path / filename

            # Aplana todas as mensagens e descobre o conjunto de colunas
            flat_rows: list[dict[str, Any]] = []
            all_columns: dict[str, None] = {}  # dict preserva inserção (Python 3.7+)

            for entry in entries:
                flat = _flatten_dict(entry["msg_dict"])
                flat_rows.append({"timestamp": entry["timestamp"], **flat})
                for key in flat:
                    all_columns[key] = None

            fieldnames = ["timestamp"] + list(all_columns.keys())

            try:
                with open(file_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(
                        f,
                        fieldnames=fieldnames,
                        extrasaction="ignore",
                        restval="",   # campos ausentes ficam vazios
                    )
                    writer.writeheader()
                    writer.writerows(flat_rows)

                saved_paths[topic] = str(file_path)
                logger.info(
                    "save_to_csv(): '%s' → %s (%d linha(s), %d coluna(s)).",
                    topic, file_path, len(flat_rows), len(fieldnames),
                )

            except OSError as exc:
                logger.error(
                    "save_to_csv(): erro ao gravar '%s': %s", file_path, exc
                )

        logger.info(
            "save_to_csv(): %d/%d tópico(s) exportado(s).",
            len(saved_paths), len(topics_snapshot),
        )
        return saved_paths

    # ------------------------------------------------------------------
    # Inspeção
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """
        Retorna estatísticas da sessão de gravação atual.

        Returns:
            dict com:
                - ``recording``     — bool
                - ``topics``        — lista de tópicos
                - ``entry_counts``  — { topic: n_entradas }
                - ``total_entries`` — total de entradas em memória
        """
        with self._lock:
            counts = {t: len(v) for t, v in self._data.items()}
            return {
                "recording":     self._recording,
                "topics":        list(self._topics),
                "entry_counts":  counts,
                "total_entries": sum(counts.values()),
            }


# ---------------------------------------------------------------------------
# Helpers de aplanamento
# ---------------------------------------------------------------------------

def _flatten_dict(
    d: Any,
    parent_key: str = "",
    sep: str = ".",
    _depth: int = 0,
    _max_depth: int = 8,
) -> dict[str, Any]:
    """
    Aplana um dicionário aninhado em um dicionário plano com chaves compostas.

    Exemplo:
        {"header": {"stamp": {"secs": 1}}, "data": 42}
        → {"header.stamp.secs": 1, "data": 42}

    Listas são convertidas para string JSON-like para manter uma coluna única.
    Profundidade máxima de recursão: 8 níveis.

    Args:
        d:          Valor a aplanar (dict, list, ou scalar).
        parent_key: Prefixo acumulado da chave (usado na recursão).
        sep:        Separador entre níveis (padrão: ".").

    Returns:
        dict plano com valores escalares (str, int, float, bool, None).
    """
    if _depth > _max_depth:
        return {parent_key: str(d)} if parent_key else {"_value": str(d)}

    if not isinstance(d, dict):
        # Chamada com valor não-dict — empacota
        return {parent_key: _to_scalar(d)} if parent_key else {}

    items: dict[str, Any] = {}
    for key, value in d.items():
        new_key = f"{parent_key}{sep}{key}" if parent_key else key

        if isinstance(value, dict):
            items.update(
                _flatten_dict(value, new_key, sep, _depth + 1, _max_depth)
            )
        elif isinstance(value, (list, tuple)):
            # Listas viram string — evita explosão de colunas para arrays longos
            items[new_key] = str(value)
        else:
            items[new_key] = _to_scalar(value)

    return items


def _to_scalar(value: Any) -> Any:
    """
    Converte um valor para um tipo CSV-seguro (str, int, float, bool, None).

    Retorna o valor diretamente se já for escalar; caso contrário usa str().
    """
    if isinstance(value, (int, float, bool, str, type(None))):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# Instância singleton — importe este objeto nos outros módulos.
# ---------------------------------------------------------------------------

data_recorder = DataRecorder()
