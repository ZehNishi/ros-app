"""
Endpoint de visualização de tópicos ROS via gráfico PNG.

GET /api/v1/plot/{topic:path}

Parâmetros:
    topic      — nome do tópico (ex: /chatter, /scan, /imu/data)
    field      — campo a plotar; suporta notação de ponto (ex: data, header.stamp.secs)
    limit      — máximo de pontos no gráfico (padrão: 500, máximo: 1000)
    list_index — se o campo for lista, índice a extrair (ex: 0, 1, -1)
    list_agg   — se o campo for lista, agregação a aplicar: mean|min|max|sum|first|last
                 (ignorado se list_index for especificado; padrão: mean)

Resposta:
    image/png — gráfico com eixo X = timestamp Unix e eixo Y = valor do campo

Erros:
    HTTP 400 — campo não encontrado em nenhuma mensagem
    HTTP 404 — tópico não subscrito ou buffer vazio
    HTTP 503 — ROS indisponível (rospy não importável)

Notas de design:
    - Usa matplotlib com backend Agg (sem GUI) — seguro em servidores headless.
    - O gráfico é gerado em memória (io.BytesIO) — nenhum arquivo é escrito em disco.
    - matplotlib e numpy são importados de forma lazy para que o módulo carregue
      mesmo em ambientes sem essas dependências instaladas (retorna HTTP 503).
    - O FastAPI nunca bloqueia: todo o processamento (get_history, conversão,
      plot) ocorre de forma síncrona dentro do thread pool do uvicorn.
"""

import io
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.core.logging import get_logger
from app.ros.message_converter import convert_ros_message
from app.ros.topic_manager import topic_manager

logger = get_logger(__name__)
router = APIRouter()

# Limite absoluto de pontos para evitar plots lentos / imagens gigantes
_MAX_POINTS = 1000
_DEFAULT_LIMIT = 500

# Tipo de agregação para campos lista
_ListAgg = Literal["mean", "min", "max", "sum", "first", "last"]


# ---------------------------------------------------------------------------
# Rota principal
# ---------------------------------------------------------------------------

@router.get(
    "/plot/{topic:path}",
    response_class=Response,
    responses={
        200: {"content": {"image/png": {}}, "description": "Gráfico PNG"},
        400: {"description": "Campo não encontrado ou sem dados numéricos"},
        404: {"description": "Tópico não subscrito ou buffer vazio"},
        503: {"description": "matplotlib/numpy não instalados"},
    },
    summary="Gera gráfico PNG de um campo de tópico ROS",
    tags=["plot"],
)
def get_plot(
    topic: str,
    field: Annotated[
        str,
        Query(description="Campo a plotar. Suporta notação de ponto: header.stamp.secs"),
    ],
    limit: Annotated[
        int,
        Query(ge=1, le=_MAX_POINTS, description=f"Máximo de pontos (padrão {_DEFAULT_LIMIT})"),
    ] = _DEFAULT_LIMIT,
    list_index: Annotated[
        Optional[int],
        Query(description="Índice a extrair quando o campo é lista (ex: 0, -1)"),
    ] = None,
    list_agg: Annotated[
        _ListAgg,
        Query(description="Agregação para campos lista: mean|min|max|sum|first|last"),
    ] = "mean",
):
    """
    Gera e retorna um gráfico PNG do histórico de um campo de tópico ROS.

    O histórico é obtido do buffer circular do TopicManager. Cada ponto do
    gráfico corresponde a uma mensagem recebida; o eixo X é o timestamp Unix
    (float) e o eixo Y é o valor numérico do campo informado.

    Suporte a campos lista:
        Se o campo extraído for uma lista Python, use ``list_index`` para
        selecionar um índice específico (ex: ``ranges[0]``), ou ``list_agg``
        para agregar todos os elementos (padrão: média).
        ``list_index`` tem prioridade sobre ``list_agg`` quando ambos forem
        fornecidos.
    """
    # Garante prefixo '/'
    if not topic.startswith("/"):
        topic = f"/{topic}"

    limit = min(limit, _MAX_POINTS)

    # Importação lazy — falha explícita se libs não estiverem instaladas
    try:
        import matplotlib  # noqa: PLC0415
        matplotlib.use("Agg")  # backend sem GUI, seguro para servidores headless
        import matplotlib.pyplot as plt  # noqa: PLC0415
        import matplotlib.dates as mdates  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError as exc:
        logger.error("matplotlib/numpy não instalados: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "matplotlib e numpy são necessários para o endpoint de plot. "
                f"Instale via: pip install matplotlib numpy  ({exc})"
            ),
        ) from exc

    # ------------------------------------------------------------------
    # 1. Obtém histórico do tópico
    # ------------------------------------------------------------------
    try:
        history = topic_manager.get_history(topic)
    except Exception as exc:
        logger.warning("get_plot('%s'): erro ao obter histórico: %s", topic, exc)
        raise HTTPException(
            status_code=404,
            detail=f"Tópico '{topic}' não encontrado ou não subscrito: {exc}",
        ) from exc

    if not history:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Tópico '{topic}' não tem mensagens no buffer. "
                "Verifique se o tópico está publicando e subscrito via POST /subscribe."
            ),
        )

    # Fatia os últimos `limit` pontos para não sobrecarregar o plot
    history = history[-limit:]

    # ------------------------------------------------------------------
    # 2. Converte e extrai o campo desejado
    # ------------------------------------------------------------------
    timestamps: list[float] = []
    values: list[float] = []
    extraction_errors = 0

    for entry in history:
        ts = entry["timestamp"]
        try:
            msg_dict = convert_ros_message(entry["msg"], include_meta=False)
        except Exception as exc:
            logger.debug("get_plot('%s'): erro ao converter msg ts=%.3f: %s", topic, ts, exc)
            extraction_errors += 1
            continue

        raw = _extract_field(msg_dict, field)
        if raw is None:
            extraction_errors += 1
            continue

        scalar = _resolve_value(raw, list_index, list_agg)
        if scalar is None:
            extraction_errors += 1
            continue

        timestamps.append(ts)
        values.append(scalar)

    if not values:
        subscribed_fields = _sample_fields(history[0]["msg"] if history else None)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Campo '{field}' não encontrado ou não numérico em nenhuma "
                f"das {len(history)} mensagem(ns) de '{topic}'. "
                f"Campos disponíveis: {subscribed_fields}"
            ),
        )

    if extraction_errors:
        logger.warning(
            "get_plot('%s', field='%s'): %d/%d mensagem(ns) ignorada(s) por erro de extração.",
            topic, field, extraction_errors, len(history),
        )

    x = np.array(timestamps, dtype=np.float64)
    y = np.array(values, dtype=np.float64)

    # ------------------------------------------------------------------
    # 3. Gera o gráfico em memória
    # ------------------------------------------------------------------
    png_bytes = _render_plot(plt, np, x, y, topic=topic, field=field,
                             list_index=list_index, list_agg=list_agg)

    logger.info(
        "get_plot('%s', field='%s'): %d ponto(s) plotado(s) → PNG %d bytes.",
        topic, field, len(values), len(png_bytes),
    )

    return Response(content=png_bytes, media_type="image/png")


# ---------------------------------------------------------------------------
# Helpers de extração
# ---------------------------------------------------------------------------

def _extract_field(msg_dict: dict, field: str):
    """
    Extrai um campo de um dicionário de mensagem usando notação de ponto.

    Exemplos:
        _extract_field({"data": 3.14}, "data")           → 3.14
        _extract_field({"header": {"seq": 1}}, "header.seq") → 1

    Retorna None se o campo não existir.
    """
    keys = field.split(".")
    current = msg_dict
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _resolve_value(
    raw,
    list_index: Optional[int],
    list_agg: str,
) -> Optional[float]:
    """
    Converte um valor extraído para float.

    - Scalar numérico: retorna diretamente.
    - Lista/tupla:
        - Se ``list_index`` for informado: retorna o elemento no índice.
        - Caso contrário aplica ``list_agg`` (mean, min, max, sum, first, last).
    - String numérica: tenta float().
    - Qualquer outro tipo: retorna None.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)

    if isinstance(raw, bool):
        return float(raw)  # True→1.0, False→0.0

    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return None

    if isinstance(raw, (list, tuple)) and raw:
        # Filtra apenas elementos numéricos
        nums = [v for v in raw if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not nums:
            return None

        if list_index is not None:
            try:
                return float(nums[list_index])
            except IndexError:
                return None

        agg = list_agg
        if agg == "mean":
            return sum(nums) / len(nums)
        if agg == "min":
            return float(min(nums))
        if agg == "max":
            return float(max(nums))
        if agg == "sum":
            return float(sum(nums))
        if agg == "first":
            return float(nums[0])
        if agg == "last":
            return float(nums[-1])

    return None


def _sample_fields(msg) -> list[str]:
    """
    Retorna os campos de nível superior de uma mensagem para dicas de erro.
    Não falha — retorna lista vazia em caso de qualquer exceção.
    """
    try:
        if msg is None:
            return []
        d = convert_ros_message(msg, include_meta=False)
        return list(d.keys())[:20]  # máximo 20 para não poluir a mensagem de erro
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Renderização do gráfico
# ---------------------------------------------------------------------------

def _render_plot(
    plt,
    np,
    x: "np.ndarray",
    y: "np.ndarray",
    *,
    topic: str,
    field: str,
    list_index: Optional[int],
    list_agg: str,
) -> bytes:
    """
    Gera o gráfico matplotlib e retorna os bytes PNG.

    Sempre fecha a figura após uso para liberar memória — essencial em
    servidores long-running onde plt.close() não é chamado automaticamente.
    """
    fig, ax = plt.subplots(figsize=(10, 4), dpi=100)

    try:
        # ------------------------------------------------------------------
        # Linha principal
        # ------------------------------------------------------------------
        ax.plot(x, y, linewidth=1.0, color="#2196F3", alpha=0.9, label=field)

        # Pontos sobre a linha (apenas para pequenos conjuntos de dados)
        if len(x) <= 200:
            ax.scatter(x, y, s=12, color="#2196F3", alpha=0.7, zorder=3)

        # ------------------------------------------------------------------
        # Estatísticas rápidas na legenda
        # ------------------------------------------------------------------
        stats_label = (
            f"n={len(y)}  "
            f"min={y.min():.4g}  "
            f"max={y.max():.4g}  "
            f"mean={y.mean():.4g}"
        )
        ax.plot([], [], " ", label=stats_label)  # linha invisível só para legenda

        # ------------------------------------------------------------------
        # Formatação do eixo X (timestamps Unix → legível)
        # ------------------------------------------------------------------
        _format_x_axis(ax, x)

        # ------------------------------------------------------------------
        # Rótulos e título
        # ------------------------------------------------------------------
        field_label = _field_display_name(field, list_index, list_agg, y)
        ax.set_xlabel("Tempo", fontsize=10)
        ax.set_ylabel(field_label, fontsize=10)
        ax.set_title(
            f"{topic}  ·  {field}",
            fontsize=11,
            fontweight="bold",
            pad=10,
        )

        ax.legend(fontsize=8, loc="upper left", framealpha=0.7)
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.tick_params(axis="both", labelsize=8)

        fig.tight_layout()

        # ------------------------------------------------------------------
        # Serializa em memória → bytes PNG
        # ------------------------------------------------------------------
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
        buf.seek(0)
        return buf.getvalue()

    finally:
        plt.close(fig)


def _format_x_axis(ax, x: "np.ndarray") -> None:
    """
    Formata o eixo X com rótulos de tempo legíveis.

    Janelas curtas (<120s): mostra segundos relativos ao primeiro ponto.
    Janelas longas:         mostra HH:MM:SS absoluto.
    """
    import matplotlib.ticker as ticker  # noqa: PLC0415

    duration = float(x[-1] - x[0]) if len(x) > 1 else 0.0

    if duration <= 120.0:
        # Eixo em segundos relativos — mais legível para janelas curtas
        x_rel = x - x[0]
        ax.set_xticks(ax.get_xticks())  # reseta ticks automáticos
        ax.xaxis.set_major_formatter(
            ticker.FuncFormatter(lambda v, _: f"{v:.1f}s")
        )
        # Substitui os dados do eixo pelo valor relativo
        for line in ax.get_lines():
            if len(line.get_xdata()) == len(x):
                line.set_xdata(x_rel)
        ax.set_xlim(x_rel[0] - duration * 0.02, x_rel[-1] + duration * 0.02)
    else:
        # Converte Unix timestamps para datetime e formata
        import datetime  # noqa: PLC0415

        dates = [datetime.datetime.fromtimestamp(t) for t in x]
        import matplotlib.dates as mdates  # noqa: PLC0415
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        for line in ax.get_lines():
            if len(line.get_xdata()) == len(x):
                line.set_xdata(dates)
        ax.set_xlim(dates[0], dates[-1])


def _field_display_name(
    field: str,
    list_index: Optional[int],
    list_agg: str,
    y: "np.ndarray",
) -> str:
    """Gera rótulo do eixo Y incluindo informação de agregação, se aplicável."""
    if list_index is not None:
        return f"{field}[{list_index}]"
    return field
