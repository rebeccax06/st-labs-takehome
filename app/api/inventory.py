from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.inventory_service import InventoryService
from app.schemas.api import CIDetail, CIView, Page

router = APIRouter()
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=10000)]


def inventory_service(request: Request):
    with request.app.state.sessions() as session:
        yield InventoryService(session, request.app.state.settings.organization_id)


Inventory = Annotated[InventoryService, Depends(inventory_service)]


@router.get("/devices", response_model=Page[CIView], response_model_exclude_unset=True)
def devices(service: Inventory, status: Literal["active", "inactive", "unknown"] | None = None,
            os_family: str | None = None, limit: Limit = 50, offset: Offset = 0):
    return service.list("device", dict(status=status, os_family=os_family), limit, offset)


@router.get("/users", response_model=Page[CIView], response_model_exclude_unset=True)
def users(service: Inventory, status: Literal["active", "inactive", "suspended", "unknown"] | None = None,
          mfa_enabled: bool | None = None, limit: Limit = 50, offset: Offset = 0):
    return service.list("user", dict(status=status, mfa_enabled=mfa_enabled), limit, offset)


@router.get("/apps", response_model=Page[CIView], response_model_exclude_unset=True)
def apps(service: Inventory, app_type: Literal["saas", "on_prem", "unknown"] | None = None,
         limit: Limit = 50, offset: Offset = 0):
    return service.list("application", dict(app_type=app_type), limit, offset)


@router.get("/ci/{ci_id}", response_model=CIDetail, response_model_exclude_unset=True)
def detail(ci_id: UUID, service: Inventory, edge_limit: Limit = 50, edge_offset: Offset = 0):
    return service.detail(str(ci_id), edge_limit, edge_offset)
