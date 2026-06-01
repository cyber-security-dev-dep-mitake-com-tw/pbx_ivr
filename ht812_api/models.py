from datetime import datetime
from typing import Any

from pydantic import BaseModel


class PatchConfigRequest(BaseModel):
    params: dict[str, str]

    model_config = {
        "json_schema_extra": {
            "example": {
                "params": {
                    "P47": "sip.example.com",
                    "P48": "5060",
                }
            }
        }
    }


class GetValuesResponse(BaseModel):
    values: dict[str, Any]
    diagnostics: dict[str, Any] | None = None


class ActionResponse(BaseModel):
    success: bool
    message: str
    diagnostics: dict[str, Any] | None = None


class PortStatusResponse(BaseModel):
    raw: Any


class SystemInfoResponse(BaseModel):
    raw: Any


class BackupFile(BaseModel):
    filename: str
    size_bytes: int
    created_at: datetime
    path: str
    diagnostics: dict[str, Any] | None = None


class BackupListResponse(BaseModel):
    count: int
    backups: list[BackupFile]
    diagnostics: dict[str, Any] | None = None


class ProvisionLine(BaseModel):
    port: int
    extension: str
    sip_server: str
    sip_port: str
    transport: str
    password_manual: bool


class ProvisionTwoLineRequest(BaseModel):
    sip_server: str | None = None
    sip_port: str = "5060"
    transport: str = "tcp"
    line1_extension: str = "1001"
    line2_extension: str = "1002"
    apply: bool = True


class ProvisionTwoLineResponse(BaseModel):
    success: bool
    message: str
    lines: list[ProvisionLine]
    params_written: list[str]
    diagnostics: dict[str, Any] | None = None


class ForceRegisterResponse(BaseModel):
    success: bool
    message: str
    sip_server: str
    sip_port: str
    transport: str
    params_written: dict[str, str]
    readback: dict[str, str]
    diagnostics: dict[str, Any] | None = None
