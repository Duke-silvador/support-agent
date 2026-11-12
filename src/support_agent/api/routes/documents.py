"""Document lifecycle: upload, read and delete with owner-scoped access.

* ``POST /documents``            — any authenticated caller; private by default.
                                   Publishing to the shared corpus (``public``)
                                   requires an operator.
* ``GET /documents/{id}``        — owner, operator, or anyone for a public doc.
* ``DELETE /documents/{id}``     — owner of a private doc, or an operator. A
                                   public doc may only be removed by an operator
                                   because it is shared context for every user.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.api.deps import enforce_rate_limit, is_operator
from support_agent.api.schemas import DocumentResponse
from support_agent.core.config import Settings, get_settings
from support_agent.core.identity import Identity, get_identity
from support_agent.core.ratelimit import RateLimitResult
from support_agent.db.models import Chunk, Document
from support_agent.db.session import get_session
from support_agent.providers.factory import get_embedding_provider
from support_agent.rag.ingest import DocumentRejected, EmbeddingUnavailable, ingest_document

logger = logging.getLogger(__name__)
router = APIRouter(tags=["documents"])

Visibility = Literal["private", "public"]


@router.post(
    "/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "Identical document already visible to the caller"},
        403: {"description": "Only an operator may publish a shared document"},
        413: {"description": "File too large"},
        415: {"description": "Unsupported file type"},
        422: {"description": "Invalid document"},
        502: {"description": "Embedding provider unavailable"},
    },
)
async def upload_document(
    response: Response,
    file: Annotated[UploadFile, File(description="UTF-8 .md or .txt document")],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    identity: Annotated[Identity, Depends(get_identity)],
    _rate: Annotated[RateLimitResult, Depends(enforce_rate_limit)],
    visibility: Annotated[Visibility, Form()] = "private",
) -> DocumentResponse:
    if visibility == "public" and not is_operator(identity.user_id, settings, identity.roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only an operator may publish a document to the shared corpus",
        )

    payload = await file.read(settings.max_upload_bytes + 1)
    if len(payload) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file too large (max {settings.max_upload_bytes} bytes)",
        )

    try:
        result = await ingest_document(
            session,
            filename=file.filename or "upload.md",
            payload=payload,
            settings=settings,
            embedder=get_embedding_provider(),
            owner_id=identity.user_id,
            visibility=visibility,
        )
    except DocumentRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except EmbeddingUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="embedding provider unavailable; try again later",
        ) from exc

    if result.duplicate:
        response.status_code = status.HTTP_200_OK

    logger.info(
        "document upload handled",
        extra={
            "user_id": identity.user_id,
            "document_id": str(result.document_id),
            "duplicate": result.duplicate,
            "chunks": result.chunk_count,
            "injection_flags": result.injection_flags,
            "visibility": result.visibility,
        },
    )
    return DocumentResponse(
        document_id=result.document_id,
        filename=result.filename,
        title=result.title,
        chunk_count=result.chunk_count,
        duplicate=result.duplicate,
        injection_flags=result.injection_flags,
        created_at=result.created_at,
        owner_id=result.owner_id,
        visibility=result.visibility,
    )


@router.get(
    "/documents/{document_id}",
    response_model=DocumentResponse,
    responses={404: {"description": "Unknown document or not visible to the caller"}},
)
async def get_document(
    document_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    identity: Annotated[Identity, Depends(get_identity)],
    _rate: Annotated[RateLimitResult, Depends(enforce_rate_limit)],
) -> DocumentResponse:
    document = await _visible_document(session, document_id, identity, settings)
    count = await session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == document.id)
    )
    return _to_response(document, chunk_count=int(count or 0), duplicate=False)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        403: {"description": "Shared documents may only be deleted by an operator"},
        404: {"description": "Unknown document or not visible to the caller"},
    },
)
async def delete_document(
    document_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    identity: Annotated[Identity, Depends(get_identity)],
    _rate: Annotated[RateLimitResult, Depends(enforce_rate_limit)],
) -> Response:
    document = await _visible_document(session, document_id, identity, settings)
    operator = is_operator(identity.user_id, settings, identity.roles)
    if document.visibility == "public" and not operator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only an operator may delete a document from the shared corpus",
        )
    if document.owner_id != identity.user_id and not operator:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")

    await session.delete(document)
    await session.commit()
    logger.info(
        "document deleted",
        extra={
            "user_id": identity.user_id,
            "document_id": str(document.id),
            "visibility": document.visibility,
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _visible_document(
    session: AsyncSession,
    document_id: str,
    identity: Identity,
    settings: Settings,
) -> Document:
    # Unknown/invisible ids deliberately collapse into one 404 so a caller cannot
    # probe for the existence of another user's private documents.
    document = await session.scalar(select(Document).where(Document.id == _as_uuid(document_id)))
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    visible = (
        document.visibility == "public"
        or document.owner_id == identity.user_id
        or is_operator(identity.user_id, settings, identity.roles)
    )
    if not visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    return document


def _as_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="document not found"
        ) from exc


def _to_response(document: Document, *, chunk_count: int, duplicate: bool) -> DocumentResponse:
    return DocumentResponse(
        document_id=document.id,
        filename=document.filename,
        title=document.title,
        chunk_count=chunk_count,
        duplicate=duplicate,
        injection_flags=[str(f) for f in (document.injection_flags or [])],
        created_at=document.created_at,
        owner_id=document.owner_id,
        visibility="public" if document.visibility == "public" else "private",
    )
