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
