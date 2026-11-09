"""Server-created identity context passed into internal services."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CustomerPrincipal:
    customer_key: str
    session_id: UUID
    issued_at: datetime
    expires_at: datetime
    auth_method: Literal["demo_cookie"] = "demo_cookie"


@dataclass(frozen=True, slots=True)
class DevicePrincipal:
    """Narrowly scoped Android companion identity; never grants banking access."""

    device_id: str
    customer_key: str
    device_name: str
    status: Literal["ACTIVE"] = "ACTIVE"
    auth_method: Literal["device_bearer"] = "device_bearer"
