"""
Rotas para gerenciamento de arquivos dentro do workspace ROS.

Todas as operações são restritas ao diretório base configurado em
``settings.FILES_BASE_PATH`` (padrão: ``~/catkin_ws``).

Qualquer tentativa de acessar um caminho fora desse diretório — inclusive
via path traversal (``../``, symlinks, caminhos absolutos) — é rejeitada
com HTTP 403.

Rotas disponíveis:
    GET  /api/v1/files?path=<rel>   — lista arquivos e diretórios
    GET  /api/v1/file?path=<rel>    — lê conteúdo de um arquivo (UTF-8)
    POST /api/v1/file               — cria ou substitui um arquivo

Limites configuráveis via settings (app/core/config.py ou .env):
    FILES_BASE_PATH      — diretório raiz (padrão ~/catkin_ws)
    FILES_MAX_READ_BYTES — tamanho máximo de leitura (padrão 1 MB)
    FILES_MAX_WRITE_BYTES — tamanho máximo de escrita (padrão 1 MB)
"""

from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class FileEntry(BaseModel):
    name: str
    path: str
    kind: Literal["file", "directory"]
    size_bytes: Optional[int] = None


class ListFilesResponse(BaseModel):
    base_path: str
    path: str
    entries: list[FileEntry]


class ReadFileResponse(BaseModel):
    path: str
    size_bytes: int
    content: str


class WriteFileRequest(BaseModel):
    path: str = Field(
        ...,
        examples=["src/my_node/my_script.py"],
        description="Caminho relativo ao BASE_PATH.",
    )
    content: str = Field(
        ...,
        description="Conteúdo textual (UTF-8) a ser gravado no arquivo.",
    )


class WriteFileResponse(BaseModel):
    status: str
    path: str
    size_bytes: int
    created_dirs: bool


# ---------------------------------------------------------------------------
# Guard de segurança
# ---------------------------------------------------------------------------

def _resolve_safe(relative_path: str) -> Path:
    """
    Resolve ``relative_path`` em relação ao ``FILES_BASE_PATH`` e verifica
    que o resultado está contido no diretório base.

    Args:
        relative_path: Caminho relativo fornecido pelo cliente.
                       Barras iniciais e ``..`` são normalizados pelo resolve().

    Returns:
        Path absoluto e seguro.

    Raises:
        HTTPException 403: Se o caminho resolvido estiver fora do BASE_PATH.
        HTTPException 400: Se o caminho estiver vazio após a normalização.
    """
    base: Path = settings.FILES_BASE_PATH.expanduser().resolve()

    # Remove barra inicial para forçar relativo ao base
    clean = relative_path.lstrip("/").strip()
    if not clean:
        raise HTTPException(
            status_code=400,
            detail="O parâmetro 'path' não pode ser vazio.",
        )

    resolved: Path = (base / clean).resolve()

    # Garante que o caminho final está dentro do base (proteção path traversal)
    try:
        resolved.relative_to(base)
    except ValueError:
        logger.warning(
            "Acesso negado — caminho '%s' resolveu para '%s', fora de '%s'.",
            relative_path,
            resolved,
            base,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"Acesso negado: o caminho '{relative_path}' está fora do "
                f"diretório base permitido."
            ),
        )

    return resolved


# ---------------------------------------------------------------------------
# GET /files
# ---------------------------------------------------------------------------

@router.get(
    "/files",
    response_model=ListFilesResponse,
    summary="Lista arquivos e diretórios",
)
def list_files(
    path: str = Query(
        default="",
        description="Caminho relativo ao BASE_PATH. Vazio lista o diretório raiz.",
        examples=["src/my_package"],
    )
):
    """
    Lista o conteúdo de um diretório dentro do workspace ROS.

    - ``path`` vazio ou ``"."`` lista o próprio ``BASE_PATH``.
    - Retorna cada entrada com ``kind`` (``file`` ou ``directory``) e
      ``size_bytes`` (apenas para arquivos).

    Raises:
        HTTP 400: Caminho vazio após normalização.
        HTTP 403: Caminho fora do BASE_PATH.
        HTTP 404: Diretório não encontrado.
        HTTP 400: Caminho aponta para um arquivo, não diretório.
    """
    base: Path = settings.FILES_BASE_PATH.expanduser().resolve()

    # path vazio ou "." → lista o próprio base
    if not path.strip() or path.strip() in (".", "./"):
        target = base
    else:
        target = _resolve_safe(path)

    logger.info("GET /files — listando '%s'.", target)

    if not target.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Diretório não encontrado: '{path}'.",
        )

    if not target.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"'{path}' é um arquivo, não um diretório. Use GET /file para lê-lo.",
        )

    entries: list[FileEntry] = []
    try:
        for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
            rel = str(item.relative_to(base))
            entries.append(
                FileEntry(
                    name=item.name,
                    path=rel,
                    kind="file" if item.is_file() else "directory",
                    size_bytes=item.stat().st_size if item.is_file() else None,
                )
            )
    except PermissionError as exc:
        logger.error("Permissão negada ao listar '%s': %s", target, exc)
        raise HTTPException(
            status_code=403,
            detail=f"Permissão negada ao acessar '{path}'.",
        ) from exc

    logger.info(
        "GET /files — '%s': %d entrada(s) encontrada(s).", path or ".", len(entries)
    )
    return ListFilesResponse(
        base_path=str(base),
        path=str(target.relative_to(base)) if target != base else ".",
        entries=entries,
    )


# ---------------------------------------------------------------------------
# GET /file
# ---------------------------------------------------------------------------

@router.get(
    "/file",
    response_model=ReadFileResponse,
    summary="Lê o conteúdo de um arquivo",
)
def read_file(
    path: str = Query(
        ...,
        description="Caminho relativo ao BASE_PATH.",
        examples=["src/my_package/my_node.py"],
    )
):
    """
    Retorna o conteúdo textual (UTF-8) de um arquivo dentro do workspace.

    Limitado a ``FILES_MAX_READ_BYTES`` (padrão 1 MB). Arquivos binários
    ou maiores que o limite são rejeitados.

    Raises:
        HTTP 400: Caminho vazio, é um diretório, excede limite ou não é UTF-8.
        HTTP 403: Caminho fora do BASE_PATH ou permissão negada.
        HTTP 404: Arquivo não encontrado.
    """
    target = _resolve_safe(path)
    logger.info("GET /file — lendo '%s'.", target)

    if not target.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Arquivo não encontrado: '{path}'.",
        )

    if target.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"'{path}' é um diretório. Use GET /files para listá-lo.",
        )

    size = target.stat().st_size
    limit = settings.FILES_MAX_READ_BYTES

    if size > limit:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Arquivo '{path}' tem {size} bytes, excede o limite de "
                f"{limit} bytes ({limit // 1024} KB)."
            ),
        )

    try:
        content = target.read_text(encoding="utf-8")
    except PermissionError as exc:
        logger.error("Permissão negada ao ler '%s': %s", target, exc)
        raise HTTPException(
            status_code=403,
            detail=f"Permissão negada ao ler '{path}'.",
        ) from exc
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Arquivo '{path}' não é um arquivo de texto UTF-8 válido. "
                "Apenas arquivos de texto são suportados."
            ),
        ) from exc

    logger.info("GET /file — '%s' lido (%d bytes).", path, size)
    return ReadFileResponse(path=path, size_bytes=size, content=content)


# ---------------------------------------------------------------------------
# POST /file
# ---------------------------------------------------------------------------

@router.post(
    "/file",
    response_model=WriteFileResponse,
    summary="Cria ou substitui um arquivo",
)
def write_file(body: WriteFileRequest):
    """
    Cria ou substitui um arquivo dentro do workspace ROS.

    - Cria automaticamente os diretórios intermediários se necessário.
    - Sobrescreve o arquivo se já existir.
    - Limitado a ``FILES_MAX_WRITE_BYTES`` (padrão 1 MB).

    Raises:
        HTTP 400: Caminho vazio, conteúdo excede limite ou o destino é um diretório.
        HTTP 403: Caminho fora do BASE_PATH ou permissão negada.
    """
    target = _resolve_safe(body.path)
    logger.info("POST /file — escrevendo '%s'.", target)

    if target.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"'{body.path}' é um diretório existente. Forneça um caminho de arquivo.",
        )

    encoded = body.content.encode("utf-8")
    limit = settings.FILES_MAX_WRITE_BYTES

    if len(encoded) > limit:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Conteúdo tem {len(encoded)} bytes, excede o limite de "
                f"{limit} bytes ({limit // 1024} KB)."
            ),
        )

    # Cria diretórios intermediários automaticamente
    created_dirs = not target.parent.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        logger.error("Permissão negada ao criar diretórios para '%s': %s", target, exc)
        raise HTTPException(
            status_code=403,
            detail=f"Permissão negada ao criar diretórios para '{body.path}'.",
        ) from exc

    if created_dirs:
        logger.info("POST /file — diretório(s) criado(s): '%s'.", target.parent)

    try:
        target.write_text(body.content, encoding="utf-8")
    except PermissionError as exc:
        logger.error("Permissão negada ao escrever '%s': %s", target, exc)
        raise HTTPException(
            status_code=403,
            detail=f"Permissão negada ao escrever '{body.path}'.",
        ) from exc

    final_size = target.stat().st_size
    logger.info(
        "POST /file — '%s' gravado (%d bytes, dirs_criados=%s).",
        body.path,
        final_size,
        created_dirs,
    )

    return WriteFileResponse(
        status="written",
        path=body.path,
        size_bytes=final_size,
        created_dirs=created_dirs,
    )
