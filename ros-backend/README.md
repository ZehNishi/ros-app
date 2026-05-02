# ros-fastapi-backend

Backend Python estruturado com **FastAPI** para integração com **ROS Noetic** via `rospy`.

---

## Estrutura do Projeto

```
ros-backend/
├── app/
│   ├── main.py               # Ponto de entrada FastAPI (lifespan, roteadores)
│   ├── core/
│   │   ├── config.py         # Configurações via variáveis de ambiente (.env)
│   │   └── logging.py        # Configuração centralizada de logging
│   ├── ros/
│   │   ├── node.py           # Inicialização e ciclo de vida do nó rospy
│   │   └── topics.py         # Publishers e subscribers por tópico
│   └── api/
│       ├── router.py         # Agrega todos os sub-roteadores
│       └── endpoints/
│           ├── health.py     # GET /health — status da API e do ROS
│           └── ros.py        # Endpoints para interagir com tópicos ROS
├── scripts/
│   └── start.sh              # Script de inicialização do servidor
├── requirements.txt
└── README.md
```

---

## Pré-requisitos

| Requisito | Versão | Notas |
|-----------|--------|-------|
| Ubuntu | 20.04 | Recomendado para ROS Noetic |
| ROS Noetic | – | [Guia de instalação](http://wiki.ros.org/noetic/Installation/Ubuntu) |
| Python | 3.8+ | Incluído no ROS Noetic |

---

## Instalação

```bash
# 1. Clone o repositório
git clone <url-do-repo>
cd ros-backend

# 2. Crie e ative o ambiente virtual
python -m venv .venv
source .venv/bin/activate

# 3. Instale as dependências Python
pip install -r requirements.txt
```

> **Importante:** `rospy` **não** é instalável via pip.  
> Ele é fornecido pelo ROS Noetic. Certifique-se de que o ROS está instalado antes de rodar.

---

## Configuração

Crie um arquivo `.env` na raiz do projeto para sobrescrever os valores padrão:

```env
APP_NAME=meu-ros-backend
HOST=0.0.0.0
PORT=8000
ROS_MASTER_URI=http://localhost:11311
ROS_NODE_NAME=fastapi_ros_node
LOG_LEVEL=INFO
```

---

## Como Rodar

### 1. Inicie o `roscore` (em um terminal separado)

```bash
source /opt/ros/noetic/setup.bash
roscore
```

### 2. Inicie o servidor FastAPI

```bash
# Modo produção
bash scripts/start.sh

# Modo desenvolvimento (hot-reload)
bash scripts/start.sh --reload
```

O servidor estará disponível em:

- **API:** `http://localhost:8000`
- **Documentação interativa (Swagger):** `http://localhost:8000/docs`
- **Documentação alternativa (ReDoc):** `http://localhost:8000/redoc`

---

## Endpoints Disponíveis

| Método | Rota | Descrição |
|--------|------|-----------|
| `GET` | `/api/v1/health` | Status geral da aplicação |
| `GET` | `/api/v1/health/ros` | Status do nó ROS |
| `POST` | `/api/v1/ros/publish/chatter` | Publica mensagem no tópico `/chatter` |

---

## Próximos Passos

1. **Ativar o nó ROS:** descomente as chamadas `rospy` em `app/ros/node.py`
2. **Adicionar tópicos:** implemente publishers/subscribers em `app/ros/topics.py`
3. **Adicionar endpoints:** crie novos arquivos em `app/api/endpoints/`
4. **Testes:** adicione testes em `tests/` com `pytest`
5. **Docker:** crie um `Dockerfile` baseado em `ros:noetic` para containerização

---

## Desenvolvimento

```bash
# Verificar tipos (opcional, requer mypy)
pip install mypy
mypy app/

# Formatar código (opcional, requer black)
pip install black
black app/
```
