# LEGACY FILE — DO NOT MOUNT
#
# This router predates the multitenancy rewrite. Its endpoints create User
# rows without organization_id and have no authentication, making them
# unsafe for any production use. The router is NOT included in main.py.
#
# All functionality has been superseded by app/employees/router.py, which
# enforces authentication, admin/HR role checks, and organization_id scoping.
#
# This file is kept only to avoid breaking any import that may reference
# `app.auth.router`. Do not add routes here and do not include this router
# in main.py.

from fastapi import APIRouter

# Empty router — no routes registered.
router = APIRouter(prefix="/employees", tags=["Employees-Legacy-Unused"])
