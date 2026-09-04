"""Response models, one careful and one that grew."""

from pydantic import BaseModel


class UserOut(BaseModel):
    """What a client should see."""

    id: int
    email: str


class UserAdminOut(BaseModel):
    """Grew a field at a time. Every one of them had a reason."""

    id: int
    email: str
    password_hash: str
    reset_token: str
    is_superuser: bool
