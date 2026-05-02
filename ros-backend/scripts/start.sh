#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/start.sh
#
# Inicializa o servidor FastAPI + ROS Noetic.
#
# Pré-requisitos:
#   1. ROS Noetic instalado: http://wiki.ros.org/noetic/Installation
#   2. roscore em execução em outro terminal: $ roscore
#   3. Ambiente virtual Python ativado com dependências instaladas:
#        python -m venv .venv && source .venv/bin/activate
#        pip install -r requirements.txt
#
# Uso:
#   bash scripts/start.sh
#   bash scripts/start.sh --reload   # modo desenvolvimento com hot-reload
# ---------------------------------------------------------------------------

set -euo pipefail

# Obtém o diretório raiz do projeto (um nível acima de scripts/)
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# ---------------------------------------------------------------------------
# Carrega o ambiente ROS Noetic
# Ajuste o caminho se o ROS estiver instalado em local diferente.
# ---------------------------------------------------------------------------
ROS_SETUP="/opt/ros/noetic/setup.bash"

if [ -f "$ROS_SETUP" ]; then
    # shellcheck source=/dev/null
    source "$ROS_SETUP"
    echo "[start.sh] Ambiente ROS carregado: $ROS_SETUP"
else
    echo "[start.sh] AVISO: $ROS_SETUP não encontrado."
    echo "           O nó ROS não será inicializado. API rodará sem ROS."
fi

# ---------------------------------------------------------------------------
# Configurações do servidor (sobrescrevíveis via variáveis de ambiente)
# ---------------------------------------------------------------------------
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
LOG_LEVEL="${LOG_LEVEL:-info}"

# Adiciona --reload se passado como argumento
EXTRA_ARGS="$*"

# ---------------------------------------------------------------------------
# Inicia o servidor
# ---------------------------------------------------------------------------
echo "[start.sh] Iniciando servidor em http://${HOST}:${PORT}"
echo "[start.sh] Documentação disponível em http://${HOST}:${PORT}/docs"

exec uvicorn app.main:app \
    --host "$HOST" \
    --port "$PORT" \
    --log-level "$LOG_LEVEL" \
    $EXTRA_ARGS
