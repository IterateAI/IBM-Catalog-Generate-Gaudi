from pydantic import BaseModel
from typing import Optional, Dict, Any


class ProvisionRequest(BaseModel):
    # Required fields
    service_id: str
    plan_id: str
    organization_guid: Optional[str] = None 
    space_guid: Optional[str] = None   
    context: Optional[Dict[str, Any]] = None
    parameters: Optional[Dict[str, Any]] = None

class PreviousValues(BaseModel):
    plan_id: Optional[str] = None
    service_id: Optional[str] = None
    organization_id: Optional[str] = None
    space_id: Optional[str] = None


class UpdateRequest(BaseModel):
    # Required fields
    service_id: str

    # Optional fields
    plan_id: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    context: Optional[Dict[str, Any]] = None
    previous_values: Optional[PreviousValues] = None
