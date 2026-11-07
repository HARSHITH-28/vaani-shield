"""Authenticated upload and retrieval routes for unified assistant audio."""

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from backend.api_models import AudioUploadResponse
from backend.auth import get_authenticated_principal
from services.principal import CustomerPrincipal
from services.speech_storage import (
    MAX_AUDIO_SIZE,
    AudioTooLargeError,
    AudioUploadError,
    UnsupportedAudioFormatError,
    delete_speech_artifact,
    get_speech_artifact,
    store_audio_upload,
)


router = APIRouter(prefix="/api/v1/voice", tags=["Assistant Voice"])


@router.post("/uploads", response_model=AudioUploadResponse, status_code=201)
async def upload_assistant_audio(
    file: UploadFile = File(...),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        content = await file.read(MAX_AUDIO_SIZE + 1)
        return store_audio_upload(
            principal, file.filename, file.content_type, content
        )
    except AudioTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from None
    except UnsupportedAudioFormatError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from None
    except AudioUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    finally:
        await file.close()


@router.get("/responses/{audio_id}")
def retrieve_assistant_audio(
    audio_id: UUID,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    artifact = get_speech_artifact(principal, audio_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Audio response not found.")
    path = Path(artifact["path"])
    return FileResponse(
        path=path,
        media_type=artifact["media_type"],
        filename="vaanishield_response.wav",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
        background=BackgroundTask(delete_speech_artifact, audio_id),
    )
