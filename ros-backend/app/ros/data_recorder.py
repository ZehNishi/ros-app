"""
Gravador de dados de tópicos ROS em formato CSV com coleta automática em background.

O DataRecorder mantém uma thread interna que chama ``record_from_buffer()``
periodicamente enquanto a gravação estiver ativa, eliminando a necessidade
de polling externo.

Fluxo típico:
    recorder.start_recording(["/chatter", "/scan"])
    # → thread de background inicia automaticamente

    # Aguarda mensagens chegarem...

    recorder.stop_recording()
    # → thread encerra de forma segura (join com timeout)

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
    - ``_lock`` protege todo o estado mutável (_recording, _topics, _data, etc.).
    - ``_stop_event`` (threading.Event) sinaliza à thread de background para encerrar.
      Usar Event.wait(timeout) ao invés de time.sleep permite resposta imediata
      ao sinal de parada.
    - Apenas uma thread de background pode existir por vez; start_recording()
      encerra a thread anterior antes de criar uma nova.
"""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.ros.message_converter import convert_ros_message

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# DataRecorder
# ---------------------------------------------------------------------------

class DataRecorder:
    """
    Grava mensagens ROS em memória com coleta automática em background e exporta CSV.

    A thread de background é iniciada automaticamente por ``start_recording()``
    e encerrada por ``stop_recording()``. Não é necessário nenhum loop externo.

    Atributos públicos (somente leitura via propriedades):
        recording       — True se uma sessão de gravação está ativa.
        topics          — Lista de tópicos sendo gravados na sessão atual.
        thread_running  — True se a thread de background está viva.

    Uso:
        from app.ros.data_recorder import data_recorder

        data_recorder.start_recording(["/chatter"])
        time.sleep(5)                          # coleta acontece automaticamente
        data_recorder.stop_recording()
        paths = data_recorder.save_to_csv("recordings/")
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()

        self._recording: bool = False
        self._topics: list[str] = []

        # { topic_name: list[{"timestamp": float, "msg_dict": dict}] }
        self._data: dict[str, list[dict[str, Any]]] = {}

        # Rastreia o último timestamp gravado por tópico (deduplicação).
        self._last_timestamps: dict[str, float] = {}

        # Thread de background e evento de parada.
        self._thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()

        logger.debug(
            "DataRecorder inicializado (RECORD_INTERVAL=%.2fs).",
            settings.RECORD_INTERVAL,
        )

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

    @property
    def thread_running(self) -> bool:
        """True se a thread de background de coleta está viva."""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Controle de sessão
    # ------------------------------------------------------------------

    def start_recording(
        self,
        topics: list[str],
        interval: Optional[float] = None,
    ) -> None:
        """
        Inicia uma nova sessão de gravação e dispara a thread de background.

        A thread executa ``record_from_buffer()`` a cada ``interval`` segundos
        usando ``threading.Event.wait()`` — isso permite que o sinal de parada
        seja respondido imediatamente sem aguardar o sleep completo.

        Se já houver uma sessão ativa, ela é encerrada (thread joined) antes
        de iniciar a nova. Os dados da sessão anterior são descartados.

        Args:
            topics:   Lista de tópicos ROS (ex. ["/chatter", "/scan"]).
            interval: Intervalo entre coletas em segundos. Usa
                      ``settings.RECORD_INTERVAL`` (padrão 0.2s) se omitido.

        Raises:
            ValueError: Se a lista de tópicos estiver vazia ou o intervalo
                        for menor ou igual a zero.
        """
        if not topics:
            raise ValueError("A lista de tópicos não pode ser vazia.")

        _interval = interval if interval is not None else settings.RECORD_INTERVAL
        if _interval <= 0:
            raise ValueError(f"interval deve ser > 0, recebido: {_interval}")

        # Encerra sessão anterior (se houver) de forma segura
        if self.thread_running or self.recording:
            logger.warning(
                "start_recording(): encerrando sessão anterior antes de iniciar nova."
            )
            self._signal_stop_and_join()

        with self._lock:
            self._topics = list(topics)
            self._data = {t: [] for t in topics}
            self._last_timestamps = {t: -1.0 for t in topics}
            self._recording = True

        # Prepara o evento de parada para a nova sessão
        self._stop_event.clear()

        # Cria thread daemon — não impede o processo de encerrar
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(_interval,),
            name=f"DataRecorder-{id(self)}",
            daemon=True,
        )
        self._thread.start()

        logger.info(
            "Gravação iniciada para %d tópico(s) %s | interval=%.2fs | thread=%s",
            len(topics),
            topics,
            _interval,
            self._thread.name,
        )

    def stop_recording(self) -> None:
        """
        Para a sessão de gravação e aguarda a thread de background encerrar.

        A thread recebe o sinal via ``_stop_event`` e termina no próximo tick,
        sem aguardar o intervalo completo. O ``join()`` garante que não há coleta
        parcial em andamento quando este método retorna.

        Se nenhuma sessão estiver ativa, a chamada é ignorada silenciosamente.
        """
        with self._lock:
            if not self._recording:
                logger.debug("stop_recording(): nenhuma sessão ativa — ignorando.")
                return
            total = sum(len(v) for v in self._data.values())

        self._signal_stop_and_join()

        logger.info(
            "Gravação encerrada. %d entrada(s) em memória para %d tópico(s).",
            total,
            len(self.topics),
        )

    # ------------------------------------------------------------------
    # Thread de background
    # ------------------------------------------------------------------

    def _run_loop(self, interval: float) -> None:
        """
        Loop de coleta executado pela thread de background.

        Usa ``_stop_event.wait(interval)`` em vez de ``time.sleep(interval)``:
        - Retorna imediatamente (True) quando ``_stop_event`` é sinalizado.
        - Retorna após o timeout (False) no caso normal, permitindo coleta.

        Importa ``topic_manager`` lazily para evitar importação circular
        (data_recorder → topic_manager → data_recorder).
        """
        # Importação lazy para evitar circular import no nível de módulo
        from app.ros.topic_manager import topic_manager  # noqa: PLC0415

        logger.debug("Thread de background iniciada (interval=%.2fs).", interval)
        cycle = 0

        while not self._stop_event.wait(timeout=interval):
            # _stop_event.wait retorna False no timeout → executa coleta
            if not self.recording:
                logger.debug("_run_loop: recording=False detectado — encerrando loop.")
                break

            try:
                counts = self.record_from_buffer(topic_manager)
                cycle += 1
                total_new = sum(counts.values())
                if total_new:
                    logger.debug(
                        "_run_loop ciclo #%d: %d nova(s) entrada(s) — %s",
                        cycle, total_new, counts,
                    )
            except RuntimeError:
                # recording foi desligado entre o check acima e record_from_buffer
                logger.debug("_run_loop: RuntimeError — sessão encerrada. Saindo.")
                break
            except Exception as exc:
                logger.warning("_run_loop ciclo #%d: erro inesperado: %s", cycle, exc)

        logger.debug("Thread de background encerrada após %d ciclo(s).", cycle)

    def _signal_stop_and_join(self, join_timeout: float = 5.0) -> None:
        """
        Sinaliza a thread para parar e aguarda sua conclusão.

        Seta ``_stop_event`` e ``_recording=False`` (nessa ordem para
        que o loop detecte o encerramento mesmo que esteja bloqueado no wait).

        Args:
            join_timeout: Segundos máximos para aguardar a thread. Após o
                          timeout, loga aviso mas prossegue.
        """
        with self._lock:
            self._recording = False

        self._stop_event.set()

        if self._thread is not None and self._thread.is_alive():
            logger.debug(
                "Aguardando thread '%s' encerrar (timeout=%.1fs)...",
                self._thread.name, join_timeout,
            )
            self._thread.join(timeout=join_timeout)

            if self._thread.is_alive():
                logger.warning(
                    "Thread '%s' não encerrou dentro de %.1fs.",
                    self._thread.name, join_timeout,
                )
            else:
                logger.debug("Thread '%s' encerrada.", self._thread.name)

        self._thread = None

    # ------------------------------------------------------------------
    # Coleta de dados (pode ser chamada externamente ou pela thread)
    # ------------------------------------------------------------------

    def record_from_buffer(self, topic_manager: Any) -> dict[str, int]:
        """
        Copia as novas mensagens do buffer do TopicManager para o armazenamento
        interno, evitando duplicação pelo timestamp.

        Apenas mensagens com ``timestamp > last_recorded_timestamp`` são
        copiadas. Chamadas repetidas são seguras e idempotentes.

        O lock é adquirido apenas para leitura da lista de tópicos e para
        escrita nas listas internas — a conversão de mensagens ocorre fora
        do lock para não bloquear callbacks do rospy nem outros threads.

        Args:
            topic_manager: Instância de TopicManager com histórico de mensagens.

        Returns:
            dict[str, int]: Número de novas entradas copiadas por tópico.
                            Exemplo: {"/chatter": 3, "/scan": 0}

        Raises:
            RuntimeError: Se chamado sem sessão de gravação ativa.
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

            with self._lock:
                last_ts = self._last_timestamps.get(topic, -1.0)

            new_entries = [e for e in history if e["timestamp"] > last_ts]

            if not new_entries:
                continue

            # Conversão fora do lock — pode demorar para mensagens grandes
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
                    "msg_dict":  msg_dict,
                })

            with self._lock:
                if topic in self._data:
                    self._data[topic].extend(converted_entries)
                    self._last_timestamps[topic] = converted_entries[-1]["timestamp"]
                    new_counts[topic] = len(converted_entries)

            logger.debug(
                "record_from_buffer('%s'): %d nova(s) entrada(s).",
                topic, len(converted_entries),
            )

        total_new = sum(new_counts.values())
        if total_new:
            logger.debug(
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

        - Primeira coluna é sempre o timestamp Unix.
        - Demais colunas são os campos planos da mensagem (notação de ponto
          para campos aninhados).
        - Cabeçalho é a união de todos os campos encontrados no tópico.
        - Campos ausentes em uma linha ficam vazios.
        - Cria o diretório automaticamente se não existir.

        Args:
            output_dir: Caminho do diretório de saída (absoluto ou relativo).

        Returns:
            dict[str, str]: { topic_name: caminho_do_arquivo }.
                            Tópicos sem dados não geram arquivo.

        Raises:
            RuntimeError: Se nenhuma sessão foi iniciada.
        """
        with self._lock:
            if not self._topics:
                raise RuntimeError(
                    "Nenhuma sessão de gravação foi iniciada. "
                    "Chame start_recording() antes de save_to_csv()."
                )
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

            filename = topic.lstrip("/").replace("/", "_") + ".csv"
            file_path = out_path / filename

            flat_rows: list[dict[str, Any]] = []
            all_columns: dict[str, None] = {}

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
                        restval="",
                    )
                    writer.writeheader()
                    writer.writerows(flat_rows)

                saved_paths[topic] = str(file_path)
                logger.info(
                    "save_to_csv(): '%s' → %s (%d linha(s), %d coluna(s)).",
                    topic, file_path, len(flat_rows), len(fieldnames),
                )

            except OSError as exc:
                logger.error("save_to_csv(): erro ao gravar '%s': %s", file_path, exc)

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
                - ``recording``      — bool: sessão ativa
                - ``thread_running`` — bool: thread de background viva
                - ``topics``         — lista de tópicos
                - ``entry_counts``   — { topic: n_entradas }
                - ``total_entries``  — soma de todas as entradas
        """
        with self._lock:
            counts = {t: len(v) for t, v in self._data.items()}
            return {
                "recording":      self._recording,
                "thread_running": self.thread_running,
                "topics":         list(self._topics),
                "entry_counts":   counts,
                "total_entries":  sum(counts.values()),
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

    Listas são convertidas para string para manter uma coluna única.
    Profundidade máxima de recursão: 8 níveis.
    """
    if _depth > _max_depth:
        return {parent_key: str(d)} if parent_key else {"_value": str(d)}

    if not isinstance(d, dict):
        return {parent_key: _to_scalar(d)} if parent_key else {}

    items: dict[str, Any] = {}
    for key, value in d.items():
        new_key = f"{parent_key}{sep}{key}" if parent_key else key

        if isinstance(value, dict):
            items.update(_flatten_dict(value, new_key, sep, _depth + 1, _max_depth))
        elif isinstance(value, (list, tuple)):
            items[new_key] = str(value)
        else:
            items[new_key] = _to_scalar(value)

    return items


def _to_scalar(value: Any) -> Any:
    """Converte para tipo CSV-seguro. Retorna o valor se já for escalar."""
    if isinstance(value, (int, float, bool, str, type(None))):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# Instância singleton — importe este objeto nos outros módulos.
# ---------------------------------------------------------------------------

data_recorder = DataRecorder()
