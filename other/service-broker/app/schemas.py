from pydantic import BaseModel
from typing import Optional, Dict

class ProvisionRequest(BaseModel):
    service_id: str
    plan_id: str
    organization_guid: Optional[str]
    space_guid: Optional[str]
    context: Optional[dict]
    parameters: Optional[dict] = {}
