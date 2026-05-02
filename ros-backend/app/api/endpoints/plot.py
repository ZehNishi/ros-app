"""
Endpoints de visualização de tópicos ROS via gráfico PNG.

Rotas (ordem de registro — mais específica primeiro):
    GET /api/v1/plot/gps/compare       — múltiplas trajetórias sobrepostas
    GET /api/v1/plot/gps/{topic:path}  — trajetória única lat/lon
    GET /api/v1/plot/{topic:path}      — série temporal de campo escalar

GET /api/v1/plot/gps/{topic:path}
    Parâmetros:
        topic     — tópico GPS (ex: /fix, /gps/fix)
        lat_field — campo latitude (padrão: latitude)
        lon_field — campo longitude (padrão: longitude)
        limit     — máximo de pontos (padrão: 2000, máximo: 5000)

GET /api/v1/plot/{topic:path}
    Parâmetros:
        topic      — nome do tópico (ex: /chatter, /scan, /imu/data)
        field      — campo a plotar; suporta notação de ponto (ex: data, header.stamp.secs)
        limit      — máximo de pontos no gráfico (padrão: 500, máximo: 1000)
        list_index — se o campo for lista, índice a extrair (ex: 0, 1, -1)
        list_agg   — se o campo for lista, agregação a aplicar: mean|min|max|sum|first|last

Resposta:
    image/png

Erros:
    HTTP 400 — campo não encontrado em nenhuma mensagem
    HTTP 404 — tópico não subscrito ou buffer vazio
    HTTP 503 — matplotlib/numpy não instalados

Notas de design:
    - Usa matplotlib com backend Agg (sem GUI) — seguro em servidores headless.
    - O gráfico é gerado em memória (io.BytesIO) — nenhum arquivo é escrito em disco.
    - matplotlib e numpy são importados de forma lazy para que o módulo carregue
      mesmo em ambientes sem essas dependências instaladas (retorna HTTP 503).
    - A rota GPS é registrada ANTES da genérica porque o path converter :path
      engole tudo, incluindo "gps/algo" — ordem de registro determina precedência.
"""

from __future__ import annotations

import io
try:
    from typing import Annotated
except ImportError:
    from typing_extensions import Annotated  # Python 3.8
from typing import Dict, List, Literal, Optional

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

# Limite para o plot GPS (trajetórias podem ser muito longas)
_GPS_MAX_POINTS = 5000
_GPS_DEFAULT_LIMIT = 2000

# Tipo de agregação para campos lista
_ListAgg = Literal["mean", "min", "max", "sum", "first", "last"]


# ---------------------------------------------------------------------------
# Rota GPS compare — DEVE ser registrada antes de /plot/gps/{topic:path}
# ---------------------------------------------------------------------------

@router.get(
    "/plot/gps/compare",
    response_class=Response,
    responses={
        200: {"content": {"image/png": {}}, "description": "Gráfico PNG com múltiplas trajetórias"},
        400: {"description": "Nenhum tópico retornou pontos válidos"},
        422: {"description": "Parâmetro 'topics' ausente ou malformado"},
        503: {"description": "matplotlib/numpy não instalados"},
    },
    summary="Sobrepõe múltiplas trajetórias GPS no mesmo gráfico",
    tags=["plot"],
)
def get_gps_compare(
    topics: Annotated[
        str,
        Query(
            description=(
                "Tópicos separados por vírgula. "
                "Ex: topics=/fix1,/fix2,/fix3"
            )
        ),
    ],
    lat_field: Annotated[
        str,
        Query(description="Campo da latitude para todos os tópicos (notação de ponto)"),
    ] = "latitude",
    lon_field: Annotated[
        str,
        Query(description="Campo da longitude para todos os tópicos (notação de ponto)"),
    ] = "longitude",
    limit: Annotated[
        int,
        Query(ge=1, le=_GPS_MAX_POINTS, description=f"Máximo de pontos por tópico (padrão {_GPS_DEFAULT_LIMIT})"),
    ] = _GPS_DEFAULT_LIMIT,
    show_markers: Annotated[
        bool,
        Query(description="Marcar ponto inicial (▲) e final (■) de cada trajetória"),
    ] = True,
):
    """
    Plota múltiplas trajetórias GPS sobrepostas no mesmo gráfico.

    Cada tópico recebe uma cor distinta da paleta ``tab10`` do matplotlib.
    Tópicos sem dados ou sem campos lat/lon válidos são ignorados com aviso
    no log — o gráfico é gerado com os tópicos restantes.

    O parâmetro ``topics`` aceita nomes separados por vírgula, com ou sem
    espaço, com ou sem ``/`` inicial::

        topics=/fix1,/fix2,/fix3
        topics=fix1, fix2, fix3   (normalizado automaticamente)

    O gráfico retorna HTTP 400 apenas se **nenhum** tópico produziu pontos
    válidos.

    Raises:
        HTTP 400  — todos os tópicos estavam vazios ou sem campos válidos.
        HTTP 422  — parâmetro ``topics`` ausente.
        HTTP 503  — matplotlib/numpy não instalados.
    """
    # ------------------------------------------------------------------
    # Parse e normalização da lista de tópicos
    # ------------------------------------------------------------------
    topic_list = [
        (t.strip() if t.strip().startswith("/") else f"/{t.strip()}")
        for t in topics.split(",")
        if t.strip()
    ]
    if not topic_list:
        raise HTTPException(
            status_code=422,
            detail="O parâmetro 'topics' está vazio. Informe ao menos um tópico.",
        )
    # Remove duplicatas preservando ordem
    seen: Dict[str, None] = {}
    for t in topic_list:
        seen[t] = None
    topic_list = list(seen.keys())

    limit = min(limit, _GPS_MAX_POINTS)

    # ------------------------------------------------------------------
    # Importação lazy
    # ------------------------------------------------------------------
    try:
        import matplotlib  # noqa: PLC0415
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"matplotlib e numpy são necessários. Instale: pip install matplotlib numpy  ({exc})",
        ) from exc

    # ------------------------------------------------------------------
    # Coleta de dados por tópico
    # ------------------------------------------------------------------
    # { topic: {"lats": np.ndarray, "lons": np.ndarray} }
    trajectories: Dict[str, dict] = {}
    skipped_topics: List[str] = []

    for topic in topic_list:
        try:
            history = topic_manager.get_history(topic)
        except Exception as exc:
            logger.warning("compare: tópico '%s' não encontrado: %s", topic, exc)
            skipped_topics.append(topic)
            continue

        if not history:
            logger.warning("compare: tópico '%s' sem mensagens no buffer.", topic)
            skipped_topics.append(topic)
            continue

        history = history[-limit:]

        lats: List[float] = []
        lons: List[float] = []
        for entry in history:
            try:
                msg_dict = convert_ros_message(entry["msg"], include_meta=False)
            except Exception:
                continue

            lat = _to_coord(_extract_field(msg_dict, lat_field))
            lon = _to_coord(_extract_field(msg_dict, lon_field))

            if lat is None or lon is None:
                continue
            if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
                continue

            lats.append(lat)
            lons.append(lon)

        if not lats:
            logger.warning(
                "compare: tópico '%s' sem pontos válidos (lat_field='%s', lon_field='%s').",
                topic, lat_field, lon_field,
            )
            skipped_topics.append(topic)
            continue

        trajectories[topic] = {
            "lats": np.array(lats, dtype=np.float64),
            "lons": np.array(lons, dtype=np.float64),
        }
        logger.info("compare: tópico '%s' — %d ponto(s) válido(s).", topic, len(lats))

    if skipped_topics:
        logger.warning("compare: %d tópico(s) ignorado(s): %s", len(skipped_topics), skipped_topics)

    if not trajectories:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Nenhum dos tópicos {topic_list} retornou pontos GPS válidos. "
                f"Campos usados: lat_field='{lat_field}', lon_field='{lon_field}'. "
                "Verifique se os tópicos estão subscritos e publicando."
            ),
        )

    # ------------------------------------------------------------------
    # Renderização
    # ------------------------------------------------------------------
    png_bytes = _render_compare_plot(
        plt, np,
        trajectories=trajectories,
        skipped=skipped_topics,
        lat_field=lat_field,
        lon_field=lon_field,
        show_markers=show_markers,
    )

    logger.info(
        "get_gps_compare: %d trajetória(s) → PNG %d bytes.",
        len(trajectories), len(png_bytes),
    )

    return Response(content=png_bytes, media_type="image/png")


# ---------------------------------------------------------------------------
# Rota GPS única — DEVE ser registrada antes de /plot/{topic:path}
# ---------------------------------------------------------------------------

@router.get(
    "/plot/gps/{topic:path}",
    response_class=Response,
    responses={
        200: {"content": {"image/png": {}}, "description": "Gráfico de trajetória GPS PNG"},
        400: {"description": "Campos lat/lon não encontrados ou sem pontos válidos"},
        404: {"description": "Tópico não subscrito ou buffer vazio"},
        503: {"description": "matplotlib/numpy não instalados"},
    },
    summary="Gera gráfico de trajetória GPS (lat × lon) como PNG",
    tags=["plot"],
)
def get_gps_plot(
    topic: str,
    lat_field: Annotated[
        str,
        Query(description="Campo da latitude. Suporta notação de ponto (ex: latitude, pose.lat)"),
    ] = "latitude",
    lon_field: Annotated[
        str,
        Query(description="Campo da longitude. Suporta notação de ponto (ex: longitude, pose.lon)"),
    ] = "longitude",
    limit: Annotated[
        int,
        Query(ge=1, le=_GPS_MAX_POINTS, description=f"Máximo de pontos (padrão {_GPS_DEFAULT_LIMIT})"),
    ] = _GPS_DEFAULT_LIMIT,
):
    """
    Plota a trajetória GPS de um tópico ROS como gráfico longitude × latitude.

    Compatível com mensagens ``sensor_msgs/NavSatFix`` (campos ``latitude`` e
    ``longitude`` no nível raiz) e mensagens customizadas via ``lat_field`` /
    ``lon_field`` com notação de ponto.

    O gráfico inclui:
    - Linha contínua da trajetória (azul)
    - Marcador do ponto inicial (triângulo verde)
    - Marcador do ponto final (quadrado vermelho)
    - Contagem de pontos válidos no título
    - Coordenadas de início e fim na legenda

    Pontos inválidos (None, NaN, ±Inf, lat fora de [-90,90],
    lon fora de [-180,180]) são ignorados silenciosamente.
    """
    if not topic.startswith("/"):
        topic = f"/{topic}"

    limit = min(limit, _GPS_MAX_POINTS)

    # Importação lazy — falha explícita se libs não estiverem instaladas
    try:
        import matplotlib  # noqa: PLC0415
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError as exc:
        logger.error("matplotlib/numpy não instalados: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=f"matplotlib e numpy são necessários. Instale: pip install matplotlib numpy  ({exc})",
        ) from exc

    # ------------------------------------------------------------------
    # 1. Obtém histórico
    # ------------------------------------------------------------------
    try:
        history = topic_manager.get_history(topic)
    except Exception as exc:
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

    history = history[-limit:]

    # ------------------------------------------------------------------
    # 2. Extrai lat / lon de cada mensagem
    # ------------------------------------------------------------------
    lats: List[float] = []
    lons: List[float] = []
    skipped = 0

    for entry in history:
        try:
            msg_dict = convert_ros_message(entry["msg"], include_meta=False)
        except Exception as exc:
            logger.debug("get_gps_plot('%s'): erro ao converter msg: %s", topic, exc)
            skipped += 1
            continue

        raw_lat = _extract_field(msg_dict, lat_field)
        raw_lon = _extract_field(msg_dict, lon_field)

        lat = _to_coord(raw_lat)
        lon = _to_coord(raw_lon)

        if lat is None or lon is None:
            skipped += 1
            continue

        # Valida faixas geográficas
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            logger.debug(
                "get_gps_plot('%s'): ponto fora de faixa ignorado lat=%.6f lon=%.6f",
                topic, lat, lon,
            )
            skipped += 1
            continue

        lats.append(lat)
        lons.append(lon)

    if not lats:
        # Tenta descobrir campos disponíveis para mensagem de erro útil
        available = _sample_fields(history[0]["msg"] if history else None)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Nenhum ponto GPS válido extraído de '{topic}' "
                f"usando lat_field='{lat_field}', lon_field='{lon_field}'. "
                f"Campos disponíveis na mensagem: {available}. "
                "Use lat_field/lon_field para apontar os campos corretos."
            ),
        )

    if skipped:
        logger.info(
            "get_gps_plot('%s'): %d/%d mensagem(ns) ignorada(s) (campos ausentes/inválidos).",
            topic, skipped, len(history),
        )

    lat_arr = np.array(lats, dtype=np.float64)
    lon_arr = np.array(lons, dtype=np.float64)

    # ------------------------------------------------------------------
    # 3. Renderiza trajetória
    # ------------------------------------------------------------------
    png_bytes = _render_gps_plot(plt, np, lat_arr, lon_arr, topic=topic,
                                 lat_field=lat_field, lon_field=lon_field)

    logger.info(
        "get_gps_plot('%s'): %d ponto(s) plotado(s) → PNG %d bytes.",
        topic, len(lats), len(png_bytes),
    )

    return Response(content=png_bytes, media_type="image/png")


# ---------------------------------------------------------------------------
# Rota principal (série temporal)
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


def _sample_fields(msg) -> List[str]:
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


# ---------------------------------------------------------------------------
# Helpers GPS
# ---------------------------------------------------------------------------

def _to_coord(raw) -> Optional[float]:
    """
    Converte um valor extraído de mensagem ROS para coordenada float.

    Aceita int, float, bool e string numérica.
    Rejeita None, NaN, ±Inf e tipos não conversíveis.

    Returns:
        float se válido; None caso contrário.
    """
    import math  # noqa: PLC0415

    if raw is None:
        return None

    if isinstance(raw, bool):
        return None  # bool é subclasse de int — descarta (True/False não são coord)

    if isinstance(raw, (int, float)):
        v = float(raw)
        if math.isnan(v) or math.isinf(v):
            return None
        return v

    if isinstance(raw, str):
        try:
            v = float(raw)
            if math.isnan(v) or math.isinf(v):
                return None
            return v
        except ValueError:
            return None

    return None


def _render_gps_plot(
    plt,
    np,
    lat: "np.ndarray",
    lon: "np.ndarray",
    *,
    topic: str,
    lat_field: str,
    lon_field: str,
) -> bytes:
    """
    Gera o gráfico de trajetória GPS e retorna os bytes PNG.

    Layout:
        - Linha contínua azul da trajetória
        - Triângulo verde no ponto inicial (primeiro ponto registrado)
        - Quadrado vermelho no ponto final (último ponto registrado)
        - Grade, aspect igual (para preservar proporção geográfica real)
        - Coordenadas de início e fim na legenda

    A figura é sempre fechada após o uso para liberar memória.
    """
    n = len(lat)

    fig, ax = plt.subplots(figsize=(8, 7), dpi=100)

    try:
        # ------------------------------------------------------------------
        # Linha da trajetória
        # ------------------------------------------------------------------
        ax.plot(
            lon, lat,
            linewidth=1.2,
            color="#2196F3",
            alpha=0.8,
            zorder=2,
            label=f"Trajetória ({n} pts)",
        )

        # Gradiente de cor ao longo do tempo usando um scatter colorido
        if n > 1:
            scatter = ax.scatter(
                lon, lat,
                c=np.arange(n),
                cmap="Blues",
                s=4,
                alpha=0.5,
                zorder=3,
                linewidths=0,
            )

        # ------------------------------------------------------------------
        # Marcador inicial (triângulo verde ▲)
        # ------------------------------------------------------------------
        ax.plot(
            lon[0], lat[0],
            marker="^",
            markersize=10,
            color="#4CAF50",
            zorder=5,
            label=f"Início  ({lat[0]:.6f}, {lon[0]:.6f})",
            linestyle="None",
        )

        # ------------------------------------------------------------------
        # Marcador final (quadrado vermelho ■)
        # ------------------------------------------------------------------
        ax.plot(
            lon[-1], lat[-1],
            marker="s",
            markersize=9,
            color="#F44336",
            zorder=5,
            label=f"Fim  ({lat[-1]:.6f}, {lon[-1]:.6f})",
            linestyle="None",
        )

        # ------------------------------------------------------------------
        # Rótulos e formatação
        # ------------------------------------------------------------------
        ax.set_xlabel(f"Longitude  [{lon_field}]", fontsize=10)
        ax.set_ylabel(f"Latitude  [{lat_field}]", fontsize=10)

        # Bounding box para o título
        lat_span = float(lat.max() - lat.min())
        lon_span = float(lon.max() - lon.min())
        ax.set_title(
            f"{topic}  —  Trajetória GPS\n"
            f"{n} ponto(s) | "
            f"Δlat={lat_span:.5f}°  Δlon={lon_span:.5f}°",
            fontsize=10,
            fontweight="bold",
            pad=10,
        )

        ax.legend(fontsize=8, loc="best", framealpha=0.8)
        ax.grid(True, linestyle="--", alpha=0.35)
        ax.tick_params(axis="both", labelsize=8)

        # Proporção igual: 1 grau lat ≈ 1 grau lon visualmente
        ax.set_aspect("equal", adjustable="datalim")

        # Margem visual ao redor da trajetória
        margin_lat = lat_span * 0.08 if lat_span > 0 else 0.0001
        margin_lon = lon_span * 0.08 if lon_span > 0 else 0.0001
        ax.set_xlim(float(lon.min()) - margin_lon, float(lon.max()) + margin_lon)
        ax.set_ylim(float(lat.min()) - margin_lat, float(lat.max()) + margin_lat)

        # Formata ticks com 5 casas decimais (precisão ~1 m)
        ax.xaxis.set_major_formatter(
            __import__("matplotlib.ticker", fromlist=["FuncFormatter"])
            .FuncFormatter(lambda v, _: f"{v:.5f}")
        )
        ax.yaxis.set_major_formatter(
            __import__("matplotlib.ticker", fromlist=["FuncFormatter"])
            .FuncFormatter(lambda v, _: f"{v:.5f}")
        )

        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
        buf.seek(0)
        return buf.getvalue()

    finally:
        plt.close(fig)


# ---------------------------------------------------------------------------
# Helper de renderização — comparação de múltiplas trajetórias
# ---------------------------------------------------------------------------

def _render_compare_plot(
    plt,
    np,
    *,
    trajectories: dict,
    skipped: List[str],
    lat_field: str,
    lon_field: str,
    show_markers: bool,
) -> bytes:
    """
    Renderiza múltiplas trajetórias GPS sobrepostas numa única figura.

    Cada tópico recebe uma cor da paleta ``tab10`` (10 cores distintas);
    para mais de 10 tópicos a paleta faz ciclo automaticamente.

    Marcadores opcionais por trajetória (quando ``show_markers=True``):
        ▲  início — cor da trajetória, mais escura
        ■  fim    — cor da trajetória, mais escura

    O bounding-box é calculado sobre todos os pontos de todos os tópicos
    para garantir que nenhuma trajetória fique fora da área visível.

    A figura é sempre fechada via ``finally`` para liberar memória.
    """
    import matplotlib.ticker as ticker  # noqa: PLC0415

    n_topics = len(trajectories)
    # tab10 tem 10 cores; para >10 tópicos usa tab20 (20 cores)
    cmap_name = "tab10" if n_topics <= 10 else "tab20"
    cmap = plt.get_cmap(cmap_name)
    colors = [cmap(i % cmap.N) for i in range(n_topics)]

    fig, ax = plt.subplots(figsize=(9, 8), dpi=100)

    try:
        all_lats: List[float] = []
        all_lons: List[float] = []

        for idx, (topic, data) in enumerate(trajectories.items()):
            lat = data["lats"]
            lon = data["lons"]
            color = colors[idx]
            n = len(lat)

            all_lats.extend(lat.tolist())
            all_lons.extend(lon.tolist())

            # ---- Linha da trajetória ----
            short_name = topic.lstrip("/")
            ax.plot(
                lon, lat,
                linewidth=1.4,
                color=color,
                alpha=0.85,
                zorder=2 + idx * 0.1,
                label=f"{short_name}  ({n} pts)",
            )

            # ---- Pontos ao longo da linha (apenas datasets pequenos) ----
            if n <= 300:
                ax.scatter(
                    lon, lat,
                    s=5,
                    color=color,
                    alpha=0.5,
                    zorder=3 + idx * 0.1,
                    linewidths=0,
                )

            if show_markers:
                # ---- Marcador inicial ▲ ----
                ax.plot(
                    lon[0], lat[0],
                    marker="^",
                    markersize=9,
                    color=color,
                    markeredgecolor="white",
                    markeredgewidth=0.6,
                    zorder=6,
                    linestyle="None",
                    label=f"  ▲ início  ({lat[0]:.5f}, {lon[0]:.5f})",
                )
                # ---- Marcador final ■ ----
                ax.plot(
                    lon[-1], lat[-1],
                    marker="s",
                    markersize=8,
                    color=color,
                    markeredgecolor="white",
                    markeredgewidth=0.6,
                    zorder=6,
                    linestyle="None",
                    label=f"  ■ fim      ({lat[-1]:.5f}, {lon[-1]:.5f})",
                )

        # ------------------------------------------------------------------
        # Bounding box global + margens
        # ------------------------------------------------------------------
        lat_arr = np.array(all_lats)
        lon_arr = np.array(all_lons)
        lat_span = float(lat_arr.max() - lat_arr.min())
        lon_span = float(lon_arr.max() - lon_arr.min())
        margin_lat = lat_span * 0.08 if lat_span > 0 else 0.0001
        margin_lon = lon_span * 0.08 if lon_span > 0 else 0.0001
        ax.set_xlim(float(lon_arr.min()) - margin_lon, float(lon_arr.max()) + margin_lon)
        ax.set_ylim(float(lat_arr.min()) - margin_lat, float(lat_arr.max()) + margin_lat)

        # ------------------------------------------------------------------
        # Aviso de tópicos ignorados (faixa de texto no gráfico)
        # ------------------------------------------------------------------
        if skipped:
            warn_text = f"Ignorado(s): {', '.join(s.lstrip('/') for s in skipped)}"
            ax.text(
                0.01, 0.01, warn_text,
                transform=ax.transAxes,
                fontsize=7,
                color="#B71C1C",
                verticalalignment="bottom",
                bbox=dict(boxstyle="round,pad=0.2", facecolor="white", alpha=0.7),
            )

        # ------------------------------------------------------------------
        # Rótulos, título, grade
        # ------------------------------------------------------------------
        ax.set_xlabel(f"Longitude  [{lon_field}]", fontsize=10)
        ax.set_ylabel(f"Latitude  [{lat_field}]", fontsize=10)
        ax.set_title(
            f"Comparação de Trajetórias GPS  —  {n_topics} tópico(s)\n"
            f"Δlat={lat_span:.5f}°  Δlon={lon_span:.5f}°",
            fontsize=10,
            fontweight="bold",
            pad=10,
        )

        ax.legend(
            fontsize=7,
            loc="best",
            framealpha=0.85,
            # Evita legenda enorme com marcadores: apenas entradas de trajetória
            # quando show_markers=False; com show_markers mostra tudo
        )
        ax.grid(True, linestyle="--", alpha=0.35)
        ax.tick_params(axis="both", labelsize=8)
        ax.set_aspect("equal", adjustable="datalim")

        # Ticks com 5 casas decimais (~1 m de precisão)
        ax.xaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.5f}"))
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.5f}"))

        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
        buf.seek(0)
        return buf.getvalue()

    finally:
        plt.close(fig)
