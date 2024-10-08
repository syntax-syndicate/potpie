
from typing import Any, Dict

from pydantic import BaseModel


class ToolRequest(BaseModel):
    tool_id: str
    params: Dict[str, Any]

class ToolResponse(BaseModel):
    results: Any

class ToolInfo(BaseModel):
    id: str
    name: str
    description: str