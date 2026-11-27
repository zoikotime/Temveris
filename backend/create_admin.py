"""
Run this script from the backend/ directory to create or reset accounts.

Usage:
  python create_admin.py                  # create/reset a tenant admin
  python create_admin.py --super-admin    # create/reset the platform super admin

The --super-admin flag creates a platform-level account with organization_id=NULL.
Add the email to SUPER_ADMIN_EMAILS in .env — the role is upgraded to super_admin
on first login automatically.
"""
import os
import sys
import argparse
import getpass
from dotenv import load_dotenv

load_dotenv()

from database.database import SessionLocal
from database.models   import User, Organization
from app.auth.service  import hash_password

# ── Tenant admin defaults (no --super-admin flag) ────────────────────────────
TENANT_EMAIL    = "admin3@temveris.example"
# Password: CREATE_ADMIN_TENANT_PASSWORD env var, or prompted for.
TENANT_NAME     = "Admin"

# ── Platform super admin defaults (--super-admin flag) ───────────────────────
# The first address in SUPER_ADMIN_EMAILS, so the account created here is the one
# the backend promotes to super_admin on first login.
SUPER_EMAIL     = (os.getenv("SUPER_ADMIN_EMAILS") or "admin@temveris.example").split(",")[0].strip()
# Password: CREATE_ADMIN_SUPER_PASSWORD env var, or prompted for.
SUPER_NAME      = "Temveris Platform"


def _password(env_name):
    """Read the password from env_name, else prompt. Never hard-coded, never printed."""
    pw = os.getenv(env_name)
    if pw:
        return pw
    pw = getpass.getpass(f"New password ({env_name} not set): ")
    if not pw or pw != getpass.getpass("Repeat password: "):
        sys.exit("[ERROR] Passwords empty or do not match")
    return pw


def create_tenant_admin(db):
    password = _password("CREATE_ADMIN_TENANT_PASSWORD")
    org = db.query(Organization).first()
    org_id = org.id if org else None
    if not org_id:
        print("[WARN] No organization found — admin will have organization_id=NULL")

    user = db.query(User).filter(User.email == TENANT_EMAIL.lower()).first()
    if user:
        user.password        = hash_password(password)
        user.role            = "admin"
        user.is_active       = True
        user.is_password_set = True
        user.invite_token    = None
        if user.organization_id is None and org_id:
            user.organization_id = org_id
        db.commit()
        print(f"[OK] Password reset for existing tenant admin: {TENANT_EMAIL}")
    else:
        db.add(User(
            name            = TENANT_NAME,
            email           = TENANT_EMAIL.lower(),
            password        = hash_password(password),
            role            = "admin",
            is_active       = True,
            is_invited      = False,
            is_password_set = True,
            organization_id = org_id,
        ))
        db.commit()
        print(f"[OK] Tenant admin created: {TENANT_EMAIL}")

    print(f"   Email   : {TENANT_EMAIL}")
    print(f"   Role    : admin (org_id={org_id})")
    print("   Login at localhost:5173")


def create_super_admin(db):
    password = _password("CREATE_ADMIN_SUPER_PASSWORD")
    user = db.query(User).filter(User.email == SUPER_EMAIL.lower()).first()
    if user:
        user.password        = hash_password(password)
        user.role            = "admin"          # promotion to super_admin happens on first login
        user.organization_id = None             # platform account — no tenant affiliation
        user.is_active       = True
        user.is_password_set = True
        user.invite_token    = None
        db.commit()
        print(f"[OK] Platform account reset: {SUPER_EMAIL}")
    else:
        db.add(User(
            name            = SUPER_NAME,
            email           = SUPER_EMAIL.lower(),
            password        = hash_password(password),
            role            = "admin",          # promoted to super_admin on first login
            is_active       = True,
            is_invited      = False,
            is_password_set = True,
            organization_id = None,             # no tenant org — platform layer only
        ))
        db.commit()
        print(f"[OK] Platform account created: {SUPER_EMAIL}")

    print(f"   Email   : {SUPER_EMAIL}")
    print(f"   org_id  : NULL (platform layer)")
    print()
    print("   NEXT STEPS:")
    print(f"   1. Add to backend/.env:  SUPER_ADMIN_EMAILS={SUPER_EMAIL}")
    print(f"   2. Restart the backend server")
    print(f"   3. Log in — role is upgraded to super_admin automatically on first login")
    print(f"   4. You will be redirected to /super-admin")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--super-admin", action="store_true",
                        help="Create/reset the platform super admin account")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.super_admin:
            create_super_admin(db)
        else:
            create_tenant_admin(db)
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
