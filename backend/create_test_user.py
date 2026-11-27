"""
Creates or resets a local test admin user tied to organization_id=1.

Run from the backend/ directory:
    python create_test_user.py
"""
import sys
from dotenv import load_dotenv

load_dotenv()

from database.database import SessionLocal
from database.models   import User, Organization
from app.auth.service  import hash_password

EMAIL    = "testadmin@temveris.local"
PASSWORD = "TestAdmin@123"
NAME     = "Test Admin"
ORG_ID   = 1


def main():
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.id == ORG_ID).first()
        if not org:
            print(f"[ERROR] Organization id={ORG_ID} not found. Run migrations first.")
            sys.exit(1)

        user = db.query(User).filter(User.email == EMAIL.lower()).first()

        if user:
            user.password        = hash_password(PASSWORD)
            user.role            = "admin"
            user.organization_id = ORG_ID
            user.is_active       = True
            user.is_password_set = True
            user.invite_token    = None
            db.commit()
            print(f"[OK] Reset existing user: {EMAIL}")
        else:
            new_user = User(
                name            = NAME,
                email           = EMAIL.lower(),
                password        = hash_password(PASSWORD),
                role            = "admin",
                organization_id = ORG_ID,
                is_active       = True,
                is_invited      = False,
                is_password_set = True,
            )
            db.add(new_user)
            db.commit()
            db.refresh(new_user)
            print(f"[OK] Test user created: id={new_user.id}")

        print(f"   Email           : {EMAIL}")
        print(f"   Password        : {PASSWORD}")
        print(f"   Role            : admin")
        print(f"   Organization ID : {ORG_ID}  ({org.name})")
        print("   Login at        : http://localhost:5173")

    except Exception as e:
        db.rollback()
        print(f"[ERROR] {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
