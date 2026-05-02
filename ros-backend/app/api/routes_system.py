"""
Rotas para execução de comandos do sistema dentro do workspace ROS.

Todas as execuções ocorrem com ``cwd=SYSTEM_WORKDIR`` (padrão: ~/catkin_ws).
O primeiro argumento do comando é validado contra uma allowlist configurável
para impedir execução arbitrária de binários.

Rotas disponíveis:
    POST /api/v1/run                — executa comando e aguarda conclusão
    POST /api/v1/run/background     — executa em background, retorna PID
    POST /api/v1/kill               — encerra processo por PID

Segurança:
- Nunca usa ``shell=True`` — argumentos são passados como lista.
- Valida o executável contra ``settings.SYSTEM_ALLOWED_COMMANDS``.
- Limita stdout/stderr capturados a ``settings.SYSTEM_MAX_OUTPUT_BYTES``.
- Timeout configurável por requisição (máximo global em ``settings``).
- Processos background são rastreados internamente para permitir /kill seguro.
"""

import os
import signal
import subprocess
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()

# Registro de processos background ativos { pid: Popen }
_background_processes: dict[int, subprocess.Popen] = {}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    command: list[str] = Field(
        ...,
        min_length=1,
        examples=[["rosrun", "turtlesim", "turtlesim_node"]],
        description="Lista de argumentos do comando. NÃO use strings com espaços.",
    )
    timeout: Optional[int] = Field(
        default=None,
        ge=1,
        le=300,
        description=(
            "Timeout em segundos (1–300). "
            f"Padrão: {settings.SYSTEM_DEFAULT_TIMEOUT}s."
        ),
    )

    @field_validator("command")
    @classmethod
    def command_not_empty(cls, v: list[str]) -> list[str]:
        if not v or not v[0].strip():
            raise ValueError("O comando não pode ser vazio.")
        return v


class RunResponse(BaseModel):
    command: list[str]
    workdir: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


class BackgroundRunRequest(BaseModel):
    command: list[str] = Field(
        ...,
        min_length=1,
        examples=[["roslaunch", "turtlesim", "multisim.launch"]],
        description="Lista de argumentos do comando a executar em background.",
    )

    @field_validator("command")
    @classmethod
    def command_not_empty(cls, v: list[str]) -> list[str]:
        if not v or not v[0].strip():
            raise ValueError("O comando não pode ser vazio.")
        return v


class BackgroundRunResponse(BaseModel):
    pid: int
    command: list[str]
    workdir: str
    status: str


class KillRequest(BaseModel):
    pid: int = Field(..., ge=1, description="PID do processo a encerrar.")


class KillResponse(BaseModel):
    pid: int
    status: str
    detail: str


# ---------------------------------------------------------------------------
# Guard: allowlist de executáveis
# ---------------------------------------------------------------------------

def _assert_command_allowed(command: list[str]) -> None:
    """
    Verifica se o executável (command[0]) está na allowlist configurada.

    A verificação usa apenas o nome base do executável, ignorando o caminho
    completo (ex: ``/usr/bin/python3`` → ``python3``).

    Se ``settings.SYSTEM_ALLOWED_COMMANDS`` estiver vazio, a verificação é
    desativada (permite qualquer executável — não recomendado em produção).

    Raises:
        HTTPException 403: executável não está na allowlist.
    """
    allowed = settings.SYSTEM_ALLOWED_COMMANDS
    if not allowed:
        logger.warning(
            "SYSTEM_ALLOWED_COMMANDS está vazio — allowlist desativada. "
            "Qualquer comando será aceito."
        )
        return

    executable = os.path.basename(command[0])
    if executable not in allowed:
        logger.warning(
            "Comando bloqueado pela allowlist: '%s'. Permitidos: %s",
            executable,
            allowed,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"Executável '{executable}' não está na lista de comandos permitidos. "
                f"Permitidos: {allowed}"
            ),
        )


def _get_workdir() -> str:
    """Retorna o diretório de trabalho como string absoluta expandida."""
    return str(settings.SYSTEM_WORKDIR.expanduser().resolve())


def _truncate(output: bytes, label: str) -> str:
    """
    Decodifica a saída e trunca se exceder o limite configurado.

    Substitui bytes inválidos por '?' para não quebrar a decodificação
    em caso de saída binária parcial.
    """
    limit = settings.SYSTEM_MAX_OUTPUT_BYTES
    if len(output) > limit:
        logger.warning(
            "%s truncado de %d para %d bytes.", label, len(output), limit
        )
        output = output[:limit]
    return output.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# POST /run
# ---------------------------------------------------------------------------

@router.post(
    "/run",
    response_model=RunResponse,
    summary="Executa comando e aguarda conclusão",
)
def run_command(body: RunRequest):
    """
    Executa um comando dentro do ``SYSTEM_WORKDIR`` e aguarda até o timeout.

    O processo roda com ``shell=False`` — a lista de argumentos é passada
    diretamente para o SO, sem interpretação de shell. Isso evita injeção
    de comandos via metacaracteres (``; && | > $``).

    Comportamento em timeout:
    - O processo é encerrado com ``SIGTERM`` (e ``SIGKILL`` se necessário).
    - ``timed_out: true`` é retornado no corpo da resposta (HTTP 200).
    - ``returncode`` será ``-15`` (SIGTERM) ou ``-9`` (SIGKILL).

    Raises:
        HTTP 403: Executável fora da allowlist.
        HTTP 400: Comando vazio.
        HTTP 500: Falha inesperada ao criar o processo.
    """
    _assert_command_allowed(body.command)

    workdir = _get_workdir()
    timeout = body.timeout or settings.SYSTEM_DEFAULT_TIMEOUT

    logger.info(
        "POST /run — comando=%s, workdir='%s', timeout=%ds.",
        body.command,
        workdir,
        timeout,
    )

    timed_out = False
    try:
        result = subprocess.run(
            body.command,
            cwd=workdir,
            capture_output=True,
            timeout=timeout,
            shell=False,
        )
        stdout = _truncate(result.stdout, "stdout")
        stderr = _truncate(result.stderr, "stderr")
        returncode = result.returncode

        logger.info(
            "POST /run — concluído: returncode=%d, stdout=%d bytes, stderr=%d bytes.",
            returncode,
            len(result.stdout),
            len(result.stderr),
        )

    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = -1
        stdout = _truncate(exc.stdout or b"", "stdout") if exc.stdout else ""
        stderr = _truncate(exc.stderr or b"", "stderr") if exc.stderr else ""

        logger.warning(
            "POST /run — timeout (%ds) ao executar %s.", timeout, body.command
        )

    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Executável '{body.command[0]}' não encontrado. "
                "Verifique se está no PATH e se o ambiente ROS está carregado."
            ),
        ) from exc

    except Exception as exc:
        logger.error("POST /run — erro inesperado: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao executar o comando: {exc}",
        ) from exc

    return RunResponse(
        command=body.command,
        workdir=workdir,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
    )


# ---------------------------------------------------------------------------
# POST /run/background
# ---------------------------------------------------------------------------

@router.post(
    "/run/background",
    response_model=BackgroundRunResponse,
    summary="Executa comando em background (não bloqueante)",
)
def run_background(body: BackgroundRunRequest):
    """
    Inicia um processo em background e retorna imediatamente com o PID.

    Útil para comandos de longa duração como ``roslaunch``.
    O processo fica registrado internamente e pode ser encerrado via
    ``POST /kill``.

    stdout e stderr são descartados (``subprocess.DEVNULL``). Para capturar
    saída em tempo real, considere implementar um endpoint SSE separado.

    Raises:
        HTTP 403: Executável fora da allowlist.
        HTTP 400: Executável não encontrado no PATH.
        HTTP 500: Falha inesperada ao iniciar o processo.
    """
    _assert_command_allowed(body.command)

    workdir = _get_workdir()

    logger.info(
        "POST /run/background — comando=%s, workdir='%s'.",
        body.command,
        workdir,
    )

    try:
        proc = subprocess.Popen(
            body.command,
            cwd=workdir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            # Cria novo grupo de processos para que SIGTERM/SIGKILL atinjam
            # o processo e seus filhos (ex: nodes ROS lançados pelo roslaunch).
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Executável '{body.command[0]}' não encontrado. "
                "Verifique se está no PATH e se o ambiente ROS está carregado."
            ),
        ) from exc
    except Exception as exc:
        logger.error("POST /run/background — erro ao iniciar: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao iniciar o processo em background: {exc}",
        ) from exc

    _background_processes[proc.pid] = proc

    logger.info(
        "POST /run/background — processo iniciado com PID %d.", proc.pid
    )

    return BackgroundRunResponse(
        pid=proc.pid,
        command=body.command,
        workdir=workdir,
        status="running",
    )


# ---------------------------------------------------------------------------
# POST /kill
# ---------------------------------------------------------------------------

@router.post(
    "/kill",
    response_model=KillResponse,
    summary="Encerra processo por PID",
)
def kill_process(body: KillRequest):
    """
    Encerra um processo iniciado via ``POST /run/background``.

    Estratégia de encerramento:
    1. Envia ``SIGTERM`` ao grupo do processo (encerramento gracioso).
    2. Aguarda 3 segundos.
    3. Se ainda estiver vivo, envia ``SIGKILL`` (encerramento forçado).

    Apenas processos registrados internamente (iniciados via /run/background)
    podem ser encerrados. Isso evita que a API seja usada para matar processos
    arbitrários do sistema.

    Raises:
        HTTP 404: PID não encontrado no registro interno.
        HTTP 500: Erro ao enviar sinal.
    """
    pid = body.pid
    logger.info("POST /kill — solicitado encerramento do PID %d.", pid)

    proc = _background_processes.get(pid)
    if proc is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"PID {pid} não encontrado no registro de processos background. "
                "Apenas processos iniciados via POST /run/background podem ser encerrados."
            ),
        )

    # Verifica se o processo já terminou por conta própria
    poll = proc.poll()
    if poll is not None:
        _background_processes.pop(pid, None)
        logger.info("PID %d já havia terminado com código %d.", pid, poll)
        return KillResponse(
            pid=pid,
            status="already_terminated",
            detail=f"Processo já havia terminado com returncode={poll}.",
        )

    try:
        # Envia SIGTERM ao grupo de processos (inclui filhos do roslaunch)
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        logger.info("SIGTERM enviado ao grupo do PID %d.", pid)

        try:
            proc.wait(timeout=3)
            status = "terminated"
            detail = f"Processo {pid} encerrado com SIGTERM."
            logger.info("PID %d encerrado via SIGTERM.", pid)

        except subprocess.TimeoutExpired:
            # SIGTERM ignorado — força com SIGKILL
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            proc.wait(timeout=2)
            status = "killed"
            detail = f"Processo {pid} não respondeu ao SIGTERM — encerrado com SIGKILL."
            logger.warning("PID %d encerrado via SIGKILL (ignorou SIGTERM).", pid)

    except ProcessLookupError:
        # Processo terminou entre o poll() e o killpg()
        status = "already_terminated"
        detail = f"Processo {pid} terminou antes do sinal ser enviado."
        logger.info("PID %d não encontrado ao enviar sinal (já terminou).", pid)

    except Exception as exc:
        logger.error("Erro ao encerrar PID %d: %s", pid, exc)
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao encerrar processo {pid}: {exc}",
        ) from exc

    finally:
        _background_processes.pop(pid, None)

    return KillResponse(pid=pid, status=status, detail=detail)
