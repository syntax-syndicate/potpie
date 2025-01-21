from fastapi import Depends, HTTPException, Header
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel

from app.core.database import get_db
from app.modules.auth.api_key_service import APIKeyService
from app.modules.conversations.conversation.conversation_controller import ConversationController
from app.modules.conversations.message.message_schema import MessageRequest
from app.modules.parsing.graph_construction.parsing_controller import ParsingController
from app.modules.parsing.graph_construction.parsing_schema import ParsingRequest
from app.modules.utils.APIRouter import APIRouter
from app.modules.conversations.conversation.conversation_schema import (
    CreateConversationRequest,
    CreateConversationResponse,
    ConversationStatus,
)

router = APIRouter()

class SimpleConversationRequest(BaseModel):
    project_ids: List[str]
    agent_ids: List[str]

async def get_api_key_user(
    x_api_key: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> dict:
    """Dependency to validate API key and get user info."""
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="API key is required",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    
    user = await APIKeyService.validate_api_key(x_api_key, db)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    
    return user

@router.post("/conversations/", response_model=CreateConversationResponse)
async def create_conversation(
    conversation: SimpleConversationRequest,
    db: Session = Depends(get_db),
    user=Depends(get_api_key_user),
):
    user_id = user["user_id"]
    # Create full conversation request with defaults
    full_request = CreateConversationRequest(
        user_id=user_id,
        title=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        status=ConversationStatus.ARCHIVED,
        project_ids=conversation.project_ids,
        agent_ids=conversation.agent_ids
    )
    
    controller = ConversationController(db, user_id, None)
    return await controller.create_conversation(full_request)

@router.post("/parse")
async def parse_directory(
    repo_details: ParsingRequest,
    db: Session = Depends(get_db),
    user=Depends(get_api_key_user),
):
    return await ParsingController.parse_directory(repo_details, db, user)

@router.get("/parsing-status/{project_id}")
async def get_parsing_status(
    project_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_api_key_user),
):
    return await ParsingController.fetch_parsing_status(project_id, db, user)

@router.post("/conversations/{conversation_id}/message/")
async def post_message(
    conversation_id: str,
    message: MessageRequest,
    db: Session = Depends(get_db),
    user=Depends(get_api_key_user),
):
    if message.content == "" or message.content is None or message.content.isspace():
        raise HTTPException(
            status_code=400,
            detail="Message content cannot be empty"
        )

    user_id = user["user_id"]
    # Note: email is no longer available with API key auth
    controller = ConversationController(db, user_id, None)
    message_stream = controller.post_message(conversation_id, message, stream=False)
    return StreamingResponse(message_stream, media_type="text/event-stream")