"""
Seed the ISOLATED local E2E database with two complete, deliberately different
tenants. Never touches production: DATABASE_URL is overridden by run_e2e.sh
before anything is imported.

    ORG A  Asia/Kolkata      Mon-Fri  09:00  15m grace  absent 12  Annual/Unpaid/Sick
    ORG B  America/New_York  Mon-Sat  08:00  30m grace  absent 14  PTO/LWP/Medical
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from database.database import SessionLocal            # noqa: E402
from database import models                           # noqa: E402
from app.auth.service import hash_password            # noqa: E402
from app.schedule_policy import keys as K             # noqa: E402
from app.payroll import policy as pp                  # noqa: E402
from app.leave import policy as lp                    # noqa: E402

PW = "E2ePass!234"

ORGS = {
    "A": dict(
        oid=9001, name="Acme India (E2E-A)", tz="Asia/Kolkata",
        days=["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"],
        start="09:00", end="18:00", expected="8:00", grace="15", absent="12",
        week_start="MONDAY", hr="hr-a@acme-e2e.test",
        leave={
            "Annual Leave": {"limit": 15, "notice_days": 7, "pool_master": True},
            "Unpaid Leave": {"limit": 0, "unlimited": True, "is_unpaid": True},
            "Sick Leave":   {"limit": 12, "notice_days": 0, "is_sick": True,
                             "doc_required": True, "evidence_gate": True},
        },
        disabled=[],
        payroll={pp.SETTING_LWP_DIVISOR_DAYS: "30",
                 pp.SETTING_BIWEEKLY_ANCHOR: "2024-01-01",
                 pp.SETTING_TIME_ADJ_MAX_PAST_DAYS: "60",
                 lp.SETTING_YEAR_START_MONTH: "1"},
        tracker={"screenshot_interval": "1800", "idle_threshold_minutes": "15",
                 "screenshot_blur": "false", "screenshot_enabled": "true"},
    ),
    "B": dict(
        oid=9002, name="Globex US (E2E-B)", tz="America/New_York",
        days=["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"],
        start="08:00", end="17:00", expected="7:30", grace="30", absent="14",
        week_start="SUNDAY", hr="hr-b@globex-e2e.test",
        leave={
            "PTO":           {"limit": 25, "notice_days": 3, "pool_master": True},
            "LWP":           {"limit": 0, "unlimited": True, "is_unpaid": True},
            "Medical Leave": {"limit": 8, "notice_days": 0, "is_sick": True,
                              "doc_required": True, "evidence_gate": False},
        },
        # Every shipped type OFF, so a name-based rule resolves to nothing.
        disabled=["Annual Leave", "Unpaid Leave", "Sick Leave", "Casual Leave",
                  "Compensatory Leave", "Company Leave", "Half Day Leave",
                  "Emergency Leave", "Maternity Leave", "Paternity Leave",
                  "Adoption Leave", "Bereavement Leave", "Child Care Leave",
                  "Parent Care Leave", "Medical Escort Leave"],
        payroll={pp.SETTING_LWP_DIVISOR_DAYS: "22",
                 pp.SETTING_BIWEEKLY_ANCHOR: "2024-01-08",
                 pp.SETTING_TIME_ADJ_MAX_PAST_DAYS: "120",
                 lp.SETTING_YEAR_START_MONTH: "4"},
        tracker={"screenshot_interval": "300", "idle_threshold_minutes": "5",
                 "screenshot_blur": "true", "screenshot_enabled": "true"},
    ),
}


def main():
    db = SessionLocal()
    out = {}
    try:
        for tag, cfg in ORGS.items():
            oid = cfg["oid"]
            db.add(models.Organization(id=oid, name=cfg["name"]))
            db.add(models.OrgSettings(organization_id=oid,
                                      default_timezone=cfg["tz"],
                                      hr_email=cfg["hr"],
                                      primary_country="IN" if tag == "A" else "US"))
            db.commit()

            rows = {
                K.SETTING_WORKING_DAYS: json.dumps(cfg["days"]),
                K.SETTING_SHIFT_START_TIME: cfg["start"],
                K.SETTING_SHIFT_END_TIME: cfg["end"],
                K.SETTING_EXPECTED_WORK_HOURS: cfg["expected"],
                K.SETTING_GRACE_MINUTES: cfg["grace"],
                K.SETTING_ABSENT_AFTER_HOUR: cfg["absent"],
                K.SETTING_WEEK_START_DAY: cfg["week_start"],
                f"leave_policies:{oid}": json.dumps(cfg["leave"]),
                f"disabled_leave_types:{oid}": json.dumps(cfg["disabled"]),
            }
            rows.update(cfg["payroll"])
            rows.update(cfg["tracker"])
            for k, v in rows.items():
                db.add(models.AppSettings(organization_id=oid, key=k, value=v))

            admin = models.User(
                name=f"Admin {tag}", email=f"admin.{tag.lower()}@e2e.test",
                password=hash_password(PW), role="admin", is_active=True,
                is_password_set=True, organization_id=oid,
                department=f"HQ-{tag}", employee_id=f"ADM-{tag}")
            emp = models.User(
                name=f"Employee {tag}", email=f"emp.{tag.lower()}@e2e.test",
                password=hash_password(PW), role="employee", is_active=True,
                is_password_set=True, organization_id=oid, team=f"Team-{tag}",
                department=f"Ops-{tag}", designation=f"Analyst-{tag}",
                employee_id=f"EMP-{tag}", employment_type="full_time")
            db.add_all([admin, emp]); db.commit()
            db.refresh(admin); db.refresh(emp)

            db.add(models.PayrollConfig(
                user_id=emp.id, organization_id=oid, pay_type="monthly",
                basic_salary=60000.0 if tag == "A" else 5000.0,
                currency="INR" if tag == "A" else "USD"))
            db.add(models.Holiday(organization_id=oid,
                                  name=f"E2E Holiday {tag}",
                                  date=__import__("datetime").date(2026, 12, 25),
                                  holiday_type="Public"))
            db.commit()

            out[tag] = {"org_id": oid, "admin_id": admin.id, "emp_id": emp.id,
                        "admin_email": admin.email, "emp_email": emp.email,
                        "tz": cfg["tz"]}
        print(json.dumps(out, indent=1))
    finally:
        db.close()


if __name__ == "__main__":
    main()
