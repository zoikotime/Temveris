from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class ScreenshotOut(BaseModel):
    id: int
    user_id: Optional[int]
    employee_name: Optional[str]
    timestamp: Optional[datetime]
    app_name: Optional[str]
    window_title: Optional[str]
    file_url: str
    # Thumbnail is now a fetchable URL (Supabase signed URL in prod,
    # or a /uploads/... path locally). Legacy rows may still return a
    # base64 JPEG blob until they age out — the frontend handles both.
    thumbnail: Optional[str] = None
    mouse_clicks: int
    key_presses: int
    idle_seconds: Optional[int] = 0
    productivity_score: Optional[float] = 0.0
    is_active: bool

    class Config:
        from_attributes = True
