#!/usr/bin/env bash
# ===========================================================================
# scripts/start.sh
#
# Prepara o ambiente ROS Noetic e inicia o servidor FastAPI.
#
# Uso:
#   bash scripts/start.sh              # modo produção
#   bash scripts/start.sh --reload     # modo desenvolvimento (hot-reload)
#
# Variáveis de ambiente reconhecidas (todas com fallback seguro):
#   ROS_MASTER_URI   URI do roscore        (padrão: http://localhost:11311)
#   ROS_IP           IP local do nó ROS    (padrão: detectado via hostname -I)
#   CATKIN_WS        Caminho do workspace  (padrão: ~/catkin_ws)
#   HOST             Endereço de bind      (padrão: 0.0.0.0)
#   PORT             Porta do servidor     (padrão: 8000)
#   LOG_LEVEL        Nível de log uvicorn  (padrão: info)
#
# Pré-requisitos:
#   1. Ubuntu 20.04 + ROS Noetic instalado
#      http://wiki.ros.org/noetic/Installation/Ubuntu
#   2. roscore rodando em outro terminal:
#      source /opt/ros/noetic/setup.bash && roscore
#   3. Virtualenv Python ativado com dependências:
#      python -m venv .venv && source .venv/bin/activate
#      pip install -r requirements.txt
# ===========================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers de log
# ---------------------------------------------------------------------------
_info()    { echo "[start.sh] INFO  $*"; }
_warn()    { echo "[start.sh] WARN  $*" >&2; }
_error()   { echo "[start.sh] ERROR $*" >&2; }
_section() { echo ""; echo "[start.sh] ─────────────────────────────────────"; echo "[start.sh] $*"; }

# ---------------------------------------------------------------------------
# 0. Diretório raiz do projeto
# ---------------------------------------------------------------------------
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
_info "Diretório do projeto: $PROJECT_ROOT"

# ---------------------------------------------------------------------------
# 1. Variáveis de ambiente com fallback seguro
# ---------------------------------------------------------------------------
_section "Configurando variáveis de ambiente"

# ROS_MASTER_URI — onde o roscore está escutando
export ROS_MASTER_URI="${ROS_MASTER_URI:-http://localhost:11311}"
_info "ROS_MASTER_URI=$ROS_MASTER_URI"

# ROS_IP — IP local que os outros nós usarão para alcançar este nó.
# Detecta automaticamente o primeiro IP não-loopback, ou cai em 127.0.0.1.
if [ -z "${ROS_IP:-}" ]; then
    DETECTED_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    if [ -n "$DETECTED_IP" ]; then
        export ROS_IP="$DETECTED_IP"
        _info "ROS_IP detectado automaticamente: $ROS_IP"
    else
        export ROS_IP="127.0.0.1"
        _warn "Não foi possível detectar o IP local. Usando fallback: ROS_IP=$ROS_IP"
    fi
else
    _info "ROS_IP definido externamente: $ROS_IP"
fi

# Workspace catkin
CATKIN_WS="${CATKIN_WS:-$HOME/catkin_ws}"
_info "Workspace catkin: $CATKIN_WS"

# Servidor
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
LOG_LEVEL="${LOG_LEVEL:-info}"

# ---------------------------------------------------------------------------
# 2. Carregar ambiente ROS global (/opt/ros/noetic)
# ---------------------------------------------------------------------------
_section "Carregando ambiente ROS Noetic"

ROS_GLOBAL_SETUP="/opt/ros/noetic/setup.bash"

if [ -f "$ROS_GLOBAL_SETUP" ]; then
    # shellcheck source=/dev/null
    source "$ROS_GLOBAL_SETUP"
    _info "Ambiente ROS global carregado: $ROS_GLOBAL_SETUP"
else
    _warn "Arquivo não encontrado: $ROS_GLOBAL_SETUP"
    _warn "ROS Noetic parece não estar instalado nesta máquina."
    _warn "O servidor vai iniciar, mas rotas ROS retornarão HTTP 503."
    _warn "Guia de instalação: http://wiki.ros.org/noetic/Installation/Ubuntu"
fi

# ---------------------------------------------------------------------------
# 3. Carregar workspace local (~/catkin_ws/devel/setup.bash)
# ---------------------------------------------------------------------------
_section "Carregando workspace local"

CATKIN_DEVEL_SETUP="$CATKIN_WS/devel/setup.bash"

if [ -f "$CATKIN_DEVEL_SETUP" ]; then
    # shellcheck source=/dev/null
    source "$CATKIN_DEVEL_SETUP"
    _info "Workspace local carregado: $CATKIN_DEVEL_SETUP"
else
    _warn "Workspace local não encontrado: $CATKIN_DEVEL_SETUP"
    _warn "Se você tem pacotes customizados, execute primeiro:"
    _warn "  cd $CATKIN_WS && catkin_make"
fi

# ---------------------------------------------------------------------------
# 4. Exportar PYTHONPATH (garante que rospy e pacotes ROS são encontrados)
# ---------------------------------------------------------------------------
_section "Configurando PYTHONPATH"

# O source do setup.bash já deve exportar o PYTHONPATH correto.
# Esta etapa adiciona o site-packages do ROS explicitamente como fallback
# para ambientes onde o source pode não ter propagado corretamente.
ROS_PYTHON_LIB="/opt/ros/noetic/lib/python3/dist-packages"

if [ -d "$ROS_PYTHON_LIB" ]; then
    if [[ ":${PYTHONPATH:-}:" != *":$ROS_PYTHON_LIB:"* ]]; then
        export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$ROS_PYTHON_LIB"
        _info "PYTHONPATH atualizado com: $ROS_PYTHON_LIB"
    else
        _info "PYTHONPATH já contém: $ROS_PYTHON_LIB"
    fi
else
    _warn "Diretório não encontrado: $ROS_PYTHON_LIB (ROS não instalado?)"
fi

_info "PYTHONPATH=$PYTHONPATH"

# ---------------------------------------------------------------------------
# 5. Verificar se o uvicorn está disponível
# ---------------------------------------------------------------------------
_section "Verificando dependências Python"

if ! command -v uvicorn &>/dev/null; then
    _error "uvicorn não encontrado no PATH."
    _error "Ative o virtualenv e instale as dependências:"
    _error "  source .venv/bin/activate"
    _error "  pip install -r requirements.txt"
    exit 1
fi

UVICORN_VERSION="$(uvicorn --version 2>&1 | head -n1)"
_info "uvicorn encontrado: $UVICORN_VERSION"

# Verifica se o FastAPI pode ser importado (detecta erros cedo)
if ! python -c "import fastapi" &>/dev/null; then
    _error "FastAPI não encontrado. Instale as dependências:"
    _error "  pip install -r requirements.txt"
    exit 1
fi
_info "FastAPI importado com sucesso."

# ---------------------------------------------------------------------------
# 6. Iniciar servidor FastAPI
# ---------------------------------------------------------------------------
_section "Iniciando servidor"

# Monta argumentos extras passados na linha de comando (ex: --reload)
EXTRA_ARGS=("$@")

_info "Host:      $HOST"
_info "Porta:     $PORT"
_info "Log level: $LOG_LEVEL"
_info "Extras:    ${EXTRA_ARGS[*]:-nenhum}"
echo ""
_info "Servidor disponível em:       http://${HOST}:${PORT}"
_info "Documentação (Swagger):       http://localhost:${PORT}/docs"
_info "Documentação (ReDoc):         http://localhost:${PORT}/redoc"
echo ""

# exec substitui o processo do shell pelo uvicorn (sem processo pai ocioso).
# Sinais (SIGTERM, SIGINT) chegam diretamente ao uvicorn para shutdown limpo.
exec uvicorn app.main:app \
    --host "$HOST" \
    --port "$PORT" \
    --log-level "$LOG_LEVEL" \
    "${EXTRA_ARGS[@]}"
