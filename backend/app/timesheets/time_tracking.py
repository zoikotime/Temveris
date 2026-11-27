from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from database.database import get_db
from database.models import ClockSession, BreakSession, User
from app.auth.service import get_current_user

router = APIRouter(prefix="/time", tags=["Time Tracking"])


def _resolve_target(user_id: int, current_user: User, db: Session) -> User:
    """
    Resolve the target user for a time-tracking operation.
    Employees may only operate on themselves.
    Admins/managers may operate on any user within their own org.
    Returns 404 for cross-org or non-existent users.
    """
    if user_id == current_user.id:
        return current_user
    if (current_user.role or "").lower() not in ("admin", "manager", "hr", "owner"):
        raise HTTPException(403, "You can only manage your own time")
    target = db.query(User).filter(
        User.id == user_id,
        User.organization_id == current_user.organization_id,
    ).first()
    if not target:
        raise HTTPException(404, "User not found")
    return target


# ==============================
# CLOCK IN
# ==============================
@router.post("/clock-in/{user_id}")
def clock_in(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target = _resolve_target(user_id, current_user, db)

    active_session = db.query(ClockSession).filter(
        ClockSession.user_id == target.id,
        ClockSession.is_active == True,
    ).first()

    if active_session:
        raise HTTPException(status_code=400, detail="Already clocked in")

    session = ClockSession(
        user_id=target.id,
        organization_id=target.organization_id,
        clock_in_time=datetime.now(timezone.utc),
        is_active=True,
    )

    db.add(session)
    db.commit()
    db.refresh(session)

    return {
        "message": "Clock-in successful",
        "session_id": session.id,
    }


# ==============================
# START BREAK
# ==============================
@router.post("/break-start/{user_id}")
def start_break(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target = _resolve_target(user_id, current_user, db)

    session = db.query(ClockSession).filter(
        ClockSession.user_id == target.id,
        ClockSession.is_active == True,
    ).first()

    if not session:
        raise HTTPException(status_code=400, detail="No active session")

    active_break = db.query(BreakSession).filter(
        BreakSession.user_id == target.id,
        BreakSession.is_active == True,
    ).first()

    if active_break:
        raise HTTPException(status_code=400, detail="Already on break")

    new_break = BreakSession(
        user_id=target.id,
        session_id=session.id,
        organization_id=target.organization_id,
        break_start=datetime.now(timezone.utc),
        is_active=True,
    )

    db.add(new_break)
    db.commit()
    db.refresh(new_break)

    return {"message": "Break started"}


# ==============================
# END BREAK
# ==============================
@router.post("/break-end/{user_id}")
def end_break(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target = _resolve_target(user_id, current_user, db)

    active_break = db.query(BreakSession).filter(
        BreakSession.user_id == target.id,
        BreakSession.is_active == True,
    ).first()

    if not active_break:
        raise HTTPException(status_code=400, detail="No active break")

    active_break.break_end = datetime.now(timezone.utc)
    duration = (active_break.break_end - active_break.break_start).total_seconds()
    active_break.break_seconds = int(duration)
    active_break.is_active = False

    session = db.query(ClockSession).filter(
        ClockSession.id == active_break.session_id,
        ClockSession.organization_id == target.organization_id,
    ).first()

    if session:
        session.break_seconds = (session.break_seconds or 0) + active_break.break_seconds

    db.commit()

    return {"message": "Break ended"}


# ==============================
# CLOCK OUT
# ==============================
@router.post("/clock-out/{user_id}")
def clock_out(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target = _resolve_target(user_id, current_user, db)

    session = db.query(ClockSession).filter(
        ClockSession.user_id == target.id,
        ClockSession.organization_id == target.organization_id,
        ClockSession.is_active == True,
    ).first()

    if not session:
        raise HTTPException(status_code=400, detail="No active session")

    session.clock_out_time = datetime.now(timezone.utc)
    total_time = (session.clock_out_time - session.clock_in_time).total_seconds()
    session.total_seconds = int(total_time - (session.break_seconds or 0))
    session.is_active = False

    db.commit()

    return {
        "message": "Clock-out successful",
        "worked_seconds": session.total_seconds,
    }


# ==============================
# STATUS
# ==============================
@router.get("/status/{user_id}")
def get_status(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target = _resolve_target(user_id, current_user, db)

    session = db.query(ClockSession).filter(
        ClockSession.user_id == target.id,
        ClockSession.organization_id == target.organization_id,
        ClockSession.is_active == True,
    ).first()

    if not session:
        return {"status": "not_clocked_in"}

    active_break = db.query(BreakSession).filter(
        BreakSession.user_id == target.id,
        BreakSession.is_active == True,
    ).first()

    if active_break:
        return {"status": "on_break"}

    return {"status": "working"}
