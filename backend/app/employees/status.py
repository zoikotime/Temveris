from sqlalchemy.orm import Session
from sqlalchemy import and_
from database import models

def calculate_status(user, db: Session):

    if user.on_leave:
        return "On Leave"

    session = db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.user_id == user.id,
            models.ClockSession.clock_out_time == None
        )
    ).first()

    if not session:
        return "Yet to Start"

    # NOTE: We MUST filter is_active == True in addition to break_end == None.
    # A "zombie" BreakSession (break_end IS NULL but is_active = False) can be
    # left behind when a Resume request races with a deploy / network blip —
    # the resume succeeds in flipping is_active off but the same transaction
    # fails to write break_end. Without the is_active filter, the admin
    # dashboard reads that zombie row as "On Break" while the worker's own
    # dashboard correctly shows "Working" (its query already filters is_active).
    # This kept Vivek-style false positives appearing only on the admin side.
    active_break = db.query(models.BreakSession).filter(
        and_(
            models.BreakSession.user_id   == user.id,
            models.BreakSession.break_end == None,
            models.BreakSession.is_active == True,
        )
    ).first()

    if active_break:
        return "On Break"

    return "Working"