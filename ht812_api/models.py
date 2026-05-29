from pydantic import BaseModel
from typing import Optional


class PatchConfigRequest(BaseModel):
    params: dict[str, str]


class SipPortStatus(BaseModel):
    port: int
    registered: bool
    user: Optional[str] = None
    server: Optional[str] = None
    raw: Optional[str] = None


class SipStatusResponse(BaseModel):
    port1: SipPortStatus
    port2: SipPortStatus


class ActionResponse(BaseModel):
    success: bool
    message: str
