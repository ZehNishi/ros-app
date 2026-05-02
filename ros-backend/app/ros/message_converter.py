"""
Conversor de mensagens ROS para dicionários Python serializáveis em JSON.

Ponto de entrada principal:
    convert_ros_message(msg, include_meta=False) -> dict

O conversor é totalmente genérico — não importa nenhum tipo específico de
mensagem (sem sensor_msgs, std_msgs, etc. em nível de módulo). A detecção
de tipo é feita em tempo de execução via duck-typing e introspecção de
``__slots__``.

Garantias:
- Nunca levanta exceção — qualquer valor não reconhecido é convertido via str().
- Protegido contra recursão infinita via ``_depth`` + ``MAX_DEPTH``.
- Retorna sempre um dict JSON-serializável (int, float, bool, str, list, dict, None).

Tipos suportados:
    Primitivos     → int, float, bool, str, bytes, None
    Listas/arrays  → list, tuple, e qualquer iterável com __iter__
    Nested msgs    → objetos com __slots__ (padrão rospy)
    rospy.Time     → {"secs": int, "nsecs": int, "total_seconds": float}
    rospy.Duration → mesmo padrão de Time
    Header         → {"seq": int, "stamp": {...}, "frame_id": str}

Metadados opcionais (include_meta=True):
    "_type"  → tipo da mensagem (ex. "sensor_msgs/LaserScan")
    "_time"  → timestamp Unix de conversão (float)
"""

import time
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Profundidade máxima de recursão para evitar loops em mensagens cíclicas.
MAX_DEPTH: int = 32


# ---------------------------------------------------------------------------
# Ponto de entrada público
# ---------------------------------------------------------------------------

def convert_ros_message(msg: Any, include_meta: bool = False) -> dict:
    """
    Converte uma mensagem ROS em um dicionário Python JSON-serializável.

    Detecta o tipo da mensagem em runtime via duck-typing e introspecção
    de ``__slots__``. Não faz nenhum import fixo de pacotes de mensagens ROS.

    Args:
        msg:
            Objeto de mensagem rospy (qualquer tipo — std_msgs, sensor_msgs,
            geometry_msgs, custom msgs, etc.).
        include_meta:
            Se True, adiciona os campos ``_type`` e ``_time`` ao dicionário
            resultante:
                ``_type`` → string com o tipo ROS (ex. "sensor_msgs/LaserScan").
                            Extraído de ``msg._type`` se disponível.
                ``_time`` → float com o timestamp Unix do momento da conversão.

    Returns:
        dict: Dicionário com todos os campos da mensagem convertidos para
              tipos primitivos Python compatíveis com JSON.
              Nunca retorna None — em caso de erro retorna
              ``{"_error": "<descrição>", "_raw": str(msg)}``.

    Exemplos:
        >>> from std_msgs.msg import String
        >>> msg = String(data="hello")
        >>> convert_ros_message(msg)
        {'data': 'hello'}

        >>> convert_ros_message(msg, include_meta=True)
        {'data': 'hello', '_type': 'std_msgs/String', '_time': 1714000000.0}

        >>> from sensor_msgs.msg import LaserScan
        >>> scan = LaserScan()
        >>> result = convert_ros_message(scan)
        >>> isinstance(result, dict)
        True
    """
    logger.debug(
        "convert_ros_message() chamado para tipo '%s' (include_meta=%s).",
        type(msg).__name__,
        include_meta,
    )

    try:
        result = _convert_value(msg, depth=0)

        # _convert_value pode retornar um não-dict para tipos primitivos raros
        # passados diretamente. Empacota para garantir retorno dict.
        if not isinstance(result, dict):
            result = {"value": result}

    except Exception as exc:
        logger.error(
            "Erro inesperado em convert_ros_message() para '%s': %s",
            type(msg).__name__,
            exc,
        )
        result = {"_error": str(exc), "_raw": _safe_str(msg)}

    if include_meta:
        result["_type"] = _extract_ros_type(msg)
        result["_time"] = time.time()
        logger.debug(
            "Metadados adicionados: _type='%s', _time=%.3f",
            result["_type"],
            result["_time"],
        )

    return result


# ---------------------------------------------------------------------------
# Conversão recursiva central
# ---------------------------------------------------------------------------

def _convert_value(value: Any, depth: int) -> Any:
    """
    Converte recursivamente um valor arbitrário para um tipo JSON-serializável.

    A recursão é protegida por ``depth``. Ao atingir ``MAX_DEPTH``, retorna
    a representação string do valor em vez de continuar descendo.

    Args:
        value: Qualquer valor Python ou objeto rospy.
        depth: Nível atual de recursão (começa em 0).

    Returns:
        int | float | bool | str | None | list | dict
    """
    # --- Proteção contra recursão excessiva --------------------------------
    if depth > MAX_DEPTH:
        logger.warning(
            "MAX_DEPTH (%d) atingido. Usando str() como fallback.", MAX_DEPTH
        )
        return _safe_str(value)

    # --- None ---------------------------------------------------------------
    if value is None:
        return None

    # --- Primitivos diretos --------------------------------------------------
    if isinstance(value, bool):
        # bool deve vir antes de int (bool é subclasse de int em Python)
        return value

    if isinstance(value, (int, float)):
        # Protege contra NaN e Inf que não são válidos em JSON
        import math  # noqa: PLC0415

        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            logger.debug("float NaN/Inf detectado — substituindo por None.")
            return None
        return value

    if isinstance(value, str):
        return value

    if isinstance(value, bytes):
        # bytes não são JSON-serializáveis diretamente — converte para lista de ints
        return list(value)

    # --- rospy.Time / rospy.Duration ----------------------------------------
    if _is_ros_time_or_duration(value):
        return _convert_time(value)

    # --- Mensagem ROS (tem __slots__) ----------------------------------------
    if _is_ros_message(value):
        return _convert_ros_slots(value, depth)

    # --- Listas, tuplas e arrays NumPy-like ---------------------------------
    if isinstance(value, (list, tuple)):
        return [_convert_value(item, depth + 1) for item in value]

    # Suporte a arrays NumPy e similares (ex: float32[], uint8[])
    if _is_array_like(value):
        try:
            return [_convert_value(item, depth + 1) for item in value]
        except Exception:
            return _safe_str(value)

    # --- Dicionários --------------------------------------------------------
    if isinstance(value, dict):
        return {
            str(k): _convert_value(v, depth + 1)
            for k, v in value.items()
        }

    # --- Fallback seguro ----------------------------------------------------
    logger.debug(
        "Tipo '%s' não reconhecido (depth=%d) — usando str() como fallback.",
        type(value).__name__,
        depth,
    )
    return _safe_str(value)


# ---------------------------------------------------------------------------
# Conversão de mensagens com __slots__
# ---------------------------------------------------------------------------

def _convert_ros_slots(msg: Any, depth: int) -> dict:
    """
    Converte um objeto rospy com ``__slots__`` em dicionário.

    Itera sobre todos os campos em ``__slots__`` e converte cada um
    recursivamente. Campos que falharem individualmente são substituídos
    por ``{"_error": ..., "_raw": ...}`` sem interromper os demais.

    Args:
        msg: Objeto rospy com ``__slots__``.
        depth: Nível atual de recursão.

    Returns:
        dict com todos os campos convertidos.
    """
    result: dict = {}
    slots: list[str] = getattr(msg, "__slots__", [])

    for field_name in slots:
        try:
            field_value = getattr(msg, field_name)
            result[field_name] = _convert_value(field_value, depth + 1)
        except Exception as exc:
            logger.warning(
                "Falha ao converter campo '%s' de '%s': %s",
                field_name,
                type(msg).__name__,
                exc,
            )
            result[field_name] = {"_error": str(exc), "_raw": _safe_str(getattr(msg, field_name, None))}

    return result


# ---------------------------------------------------------------------------
# Conversão de Time e Duration
# ---------------------------------------------------------------------------

def _convert_time(value: Any) -> dict:
    """
    Converte rospy.Time ou rospy.Duration em dicionário.

    O campo ``total_seconds`` facilita o uso direto em cálculos e
    exibição sem ter que reconstruir o valor manualmente.

    Returns:
        dict: {"secs": int, "nsecs": int, "total_seconds": float}
    """
    secs: int = int(getattr(value, "secs", 0))
    nsecs: int = int(getattr(value, "nsecs", 0))
    total_seconds: float = secs + nsecs * 1e-9

    return {
        "secs": secs,
        "nsecs": nsecs,
        "total_seconds": round(total_seconds, 9),
    }


# ---------------------------------------------------------------------------
# Detecção de tipos ROS
# ---------------------------------------------------------------------------

def _is_ros_message(value: Any) -> bool:
    """
    Retorna True se o valor for uma mensagem rospy.

    Mensagens rospy sempre têm ``__slots__`` e ``_type`` como atributos
    de classe. Usa duck-typing para não depender de imports.
    """
    return (
        hasattr(value, "__slots__")
        and hasattr(type(value), "_type")
        and not _is_ros_time_or_duration(value)
    )


def _is_ros_time_or_duration(value: Any) -> bool:
    """
    Retorna True se o valor for rospy.Time ou rospy.Duration.

    Detecta pelo nome da classe e pela presença dos atributos ``secs``/``nsecs``
    para não depender de imports diretos de rospy.
    """
    cls_name = type(value).__name__
    return (
        cls_name in ("Time", "Duration")
        and hasattr(value, "secs")
        and hasattr(value, "nsecs")
    )


def _is_array_like(value: Any) -> bool:
    """
    Retorna True para objetos iteráveis não-string que parecem arrays.

    Captura arrays NumPy, array.array e tipos similares usados em mensagens
    ROS como float32[], uint8[], etc.
    """
    if isinstance(value, (str, bytes, dict)):
        return False
    return hasattr(value, "__iter__") and hasattr(value, "__len__")


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------

def _extract_ros_type(msg: Any) -> str:
    """
    Extrai o tipo ROS de uma mensagem.

    Usa o atributo ``_type`` da classe (ex. "sensor_msgs/LaserScan").
    Cai para o nome simples da classe Python se ``_type`` não existir.
    """
    ros_type = getattr(type(msg), "_type", None) or getattr(msg, "_type", None)
    if ros_type:
        return str(ros_type)
    return type(msg).__qualname__


def _safe_str(value: Any) -> str:
    """
    Converte um valor para string de forma segura.

    Usado como fallback final — nunca levanta exceção.
    """
    try:
        return str(value)
    except Exception:
        return "<não serializável>"
