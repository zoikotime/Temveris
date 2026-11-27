from pydantic import BaseModel
from typing import Optional

# ======================
# REGISTER
# ======================
class RegisterRequest(BaseModel):
    name: Optional[str] = None
    email: str
    password: str
    role: str = "employee"


# ======================
# LOGIN
# ======================
class LoginRequest(BaseModel):
    email: str
    password: str


# ======================
# RESPONSE
# ======================
class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    role: str
    is_active: bool

    class Config:
        from_attributes = True