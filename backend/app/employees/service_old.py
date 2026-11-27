from sqlalchemy.orm import Session
from database.models import Employee, User, Department, Team
from app.employees.schemas import EmployeeCreate, EmployeeUpdate
from passlib.context import CryptContext
from app.employees.status import calculate_status

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ==============================
# 🧑‍💼 EMPLOYEE SERVICES
# ==============================

def create_employee(db: Session, data: EmployeeCreate):
    existing = db.query(User).filter(User.email == data.email).first()
    if existing:
        return None, "Email already registered"

    hashed_pw = pwd_context.hash(data.password)
    user = User(
        name=data.name,
        email=data.email,
        password=hashed_pw,
        role=data.role
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    employee = Employee(
        user_id=user.id,
        phone=data.phone,
        address=data.address,
        date_of_birth=data.date_of_birth,
        gender=data.gender,
        employee_code=data.employee_code,
        designation=data.designation,
        date_of_joining=data.date_of_joining,
        employment_type=data.employment_type
    )
    db.add(employee)
    db.commit()
    db.refresh(employee)

    return employee, None


def get_all_employees(db: Session):

    results = db.query(Employee, User).join(
        User, Employee.user_id == User.id
    ).all()

    employees = []
    for emp, user in results:

        work_status = calculate_status({
            "check_in_time": getattr(emp, "check_in_time", None),
            "check_out_time": getattr(emp, "check_out_time", None),
            "last_active": getattr(emp, "last_active", None),
            "on_leave": getattr(emp, "on_leave", False),
        })

        employees.append({
            "id": emp.id,
            "user_id": emp.user_id,

            "name": user.name,
            "email": user.email,
            "role": user.role,

            "phone": emp.phone,
            "address": emp.address,
            "gender": emp.gender,
            "employee_code": emp.employee_code,
            "designation": emp.designation,
            "date_of_joining": emp.date_of_joining,
            "employment_type": emp.employment_type,

            "check_in_time": getattr(emp, "check_in_time", None),
            "check_out_time": getattr(emp, "check_out_time", None),
            "last_active": getattr(emp, "last_active", None),
            "on_leave": getattr(emp, "on_leave", False),

            "work_status": work_status,

            "status": "active" if emp.is_active else "inactive",
            "is_active": emp.is_active,

            "clockedIn": False,
            "clockInTime": None,
            "hoursToday": 0,

            "created_at": emp.created_at
        })
    return employees


def get_employee_by_id(db: Session, employee_id: int):
    result = db.query(Employee, User).join(
        User, Employee.user_id == User.id
    ).filter(Employee.id == employee_id).first()

    if not result:
        return None

    emp, user = result

    work_status = calculate_status({
        "check_in_time": getattr(emp, "check_in_time", None),
        "check_out_time": getattr(emp, "check_out_time", None),
        "last_active": getattr(emp, "last_active", None),
        "on_leave": getattr(emp, "on_leave", False),
    })

    return {
        "id": emp.id,
        "user_id": emp.user_id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "phone": emp.phone,
        "address": emp.address,
        "gender": emp.gender,
        "employee_code": emp.employee_code,
        "designation": emp.designation,

        "date_of_joining": emp.date_of_joining,
        "employment_type": emp.employment_type,

        "check_in_time": getattr(emp, "check_in_time", None),
        "check_out_time": getattr(emp, "check_out_time", None),
        "last_active": getattr(emp, "last_active", None),
        "on_leave": getattr(emp, "on_leave", False),

        "work_status": work_status,

        "status": "active" if emp.is_active else "inactive",
        "is_active": emp.is_active,
        "clockedIn": False,
        "clockInTime": None,
        "hoursToday": 0,
        "created_at": emp.created_at
    }


def get_employee_by_user_id(db: Session, user_id: int):
    result = db.query(Employee, User).join(
        User, Employee.user_id == User.id
    ).filter(Employee.user_id == user_id).first()

    if not result:
        return None

    emp, user = result

    work_status = calculate_status({
        "check_in_time": getattr(emp, "check_in_time", None),
        "check_out_time": getattr(emp, "check_out_time", None),
        "last_active": getattr(emp, "last_active", None),
        "on_leave": getattr(emp, "on_leave", False),
    })

    return {
        "user_id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,

        "id": emp.id,
        "phone": emp.phone,
        "designation": emp.designation,

        "employment_type": emp.employment_type,
        "is_active": emp.is_active,

        "check_in_time": getattr(emp, "check_in_time", None),
        "check_out_time": getattr(emp, "check_out_time", None),
        "last_active": getattr(emp, "last_active", None),
        "on_leave": getattr(emp, "on_leave", False),

        "work_status": work_status,

        "created_at": emp.created_at
    }


def update_employee(db: Session, employee_id: int, data: EmployeeUpdate):
    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        return None, "Employee not found"

    for field, value in data.dict(exclude_unset=True).items():
        if field == "name":
            user = db.query(User).filter(User.id == emp.user_id).first()
            if user:
                user.name = value
        elif hasattr(emp, field):
            setattr(emp, field, value)

    db.commit()
    db.refresh(emp)
    return emp, None


def delete_employee(db: Session, employee_id: int):
    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        return False, "Employee not found"

    emp.is_active = False
    user = db.query(User).filter(User.id == emp.user_id).first()
    if user:
        user.is_active = False

    db.commit()
    return True, "Employee deactivated successfully"

