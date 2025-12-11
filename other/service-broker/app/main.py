from fastapi import FastAPI, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from app.auth import api_version_check, basic_auth
from app.schemas import ProvisionRequest
from app.database import SessionLocal, Base, engine
from app.models import ServiceInstance
from app.tasks import provision_instance_task, deprovision_instance_task
import logging

logger = logging.getLogger(__name__)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Minimal Service Broker")

# -------------------------------------------------------------------
# GET /v2/catalog
# -------------------------------------------------------------------
@app.get("/v2/catalog")
def get_catalog(
    auth: bool = Depends(basic_auth),
    version: bool = Depends(api_version_check)
):
    """Return service catalog - CF Service Broker API v2.12 compliant"""
    return {
        "services": [
            {
                # REQUIRED FIELDS - USE EXISTING CF MARKETPLACE IDs
                "name": "ibm-cloud-service",                 # REPLACE: Get from `cf marketplace`
                "id": "REPLACE_WITH_EXISTING_SERVICE_ID",    # REPLACE: Get from `cf curl /v2/services`
                "description": "IBM Cloud service provisioned via Terraform",
                "bindable": False,                            # No binding as requested
                
                # OPTIONAL FIELDS
                "plan_updateable": False,                     # No updates as requested
                "tags": ["ibm", "terraform"],                # For marketplace filtering
                "requires": [],                               # No special permissions needed
                
                "plans": [
                    {
                        # REQUIRED PLAN FIELDS - USE EXISTING CF PLAN IDs
                        "name": "standard",                   # REPLACE: Get from existing plan name
                        "id": "REPLACE_WITH_EXISTING_PLAN_ID", # REPLACE: Get from `cf curl /v2/service_plans`
                        "description": "Standard IBM Cloud service deployment",
                        
                        # OPTIONAL PLAN FIELDS  
                        "free": False,
                        "metadata": {
                            "displayName": "Standard Plan",
                            "bullets": [
                                "99.999999999% (11 9's) durability",
                                "Terraform-based provisioning", 
                                "Global accessibility",
                                "Integrated with IBM Cloud IAM"
                            ],
                            "costs": [
                                {
                                    "amount": {"usd": 0.023},
                                    "unit": "GB per month"
                                }
                            ]
                        }
                    }
                    # Add more plans here if they exist in your CF marketplace
                ],
                
                # SERVICE METADATA
                "metadata": {
                    "displayName": "IBM Cloud Object Storage",
                    "imageUrl": "https://cloud.ibm.com/images/catalog/icons/object-storage.svg",
                    "longDescription": "Highly scalable cloud storage service designed for high durability, resiliency and security. Store, manage and access your data via our self-service portal and RESTful APIs.",
                    "providerDisplayName": "IBM Cloud",
                    "documentationUrl": "https://cloud.ibm.com/docs/cloud-object-storage",
                    "supportUrl": "https://cloud.ibm.com/unifiedsupport/supportcenter"
                }
            }
        ]
    }

# -------------------------------------------------------------------
# PUT /v2/service_instances/:id (Async provisioning)
# -------------------------------------------------------------------
@app.put("/v2/service_instances/{instance_id}")
def provision(
    instance_id: str,
    body: ProvisionRequest,
    accepts_incomplete: bool = Query(False),
    service_id: str = Query(..., description="Service ID from catalog"),
    plan_id: str = Query(..., description="Plan ID from catalog"),
    auth: bool = Depends(basic_auth),
    version: bool = Depends(api_version_check)
):
    """Provision service instance - CF Service Broker API v2.12 compliant"""
    db = SessionLocal()
    
    try:
        # Validate required parameters
        if not service_id or not plan_id:
            return JSONResponse(
                status_code=400,
                content={"description": "service_id and plan_id are required"}
            )
        
        # Check if async is required but not requested
        if not accepts_incomplete:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "AsyncRequired",
                    "description": "This service plan requires client support for asynchronous service operations."
                }
            )
        
        # Check if instance already exists (idempotency)
        existing = db.query(ServiceInstance).filter_by(instance_id=instance_id).first()
        if existing:
            if existing.state == "succeeded":
                # Return 200 for already provisioned instance
                return JSONResponse(
                    status_code=200,
                    content={
                        "dashboard_url": existing.dashboard_url or f"http://dashboard.ibm/{instance_id}",
                        "operation": "provision"
                    }
                )
            elif existing.state == "in progress":
                # Return 202 for ongoing provisioning
                return JSONResponse(
                    status_code=202,
                    content={
                        "dashboard_url": existing.dashboard_url or f"http://dashboard.ibm/{instance_id}",
                        "operation": "provision"
                    }
                )
            elif existing.state == "failed":
                # Allow retry for failed instances
                existing.state = "in progress"
                existing.description = "Retrying provisioning"
                db.commit()
        
        # Extract custom parameters
        email = body.parameters.get('email', '') if body.parameters else ''
        name = body.parameters.get('name', '') if body.parameters else ''
        
        if not email or not name:
            return JSONResponse(
                status_code=400,
                content={"description": "Custom parameters 'email' and 'name' are required"}
            )
        
        # Create new instance if it doesn't exist
        if not existing:
            instance = ServiceInstance(
                instance_id=instance_id,
                service_id=body.service_id,
                plan_id=body.plan_id,
                organization_guid=body.organization_guid,
                space_guid=body.space_guid,
                parameters=body.parameters,
                email=email,
                name=name,
                operation="provision",
                state="in progress",
                description="Provisioning started",
                dashboard_url=f"http://dashboard.ibm/{instance_id}"
            )
            db.add(instance)
            db.commit()
        
        # Start async provisioning task
        task = provision_instance_task.delay(instance_id)
        
        # Update task ID in database
        if existing:
            existing.celery_task_id = task.id
        else:
            instance.celery_task_id = task.id
        db.commit()
        
        logger.info(f"Started provisioning for instance {instance_id} with email: {email}, name: {name}")
        
        # Return 202 Accepted for async operation
        return JSONResponse(
            status_code=202,
            content={
                "dashboard_url": f"http://dashboard.ibm/{instance_id}",
                "operation": "provision"
            }
        )
        
    except Exception as e:
        logger.error(f"Provisioning error for {instance_id}: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"description": f"Internal server error: {str(e)}"}
        )
    finally:
        db.close()

# -------------------------------------------------------------------
# GET /v2/service_instances/:id/last_operation
# -------------------------------------------------------------------
@app.get("/v2/service_instances/{instance_id}/last_operation")
def last_operation(
    instance_id: str,
    service_id: str = Query(None, description="Service ID from catalog"),
    plan_id: str = Query(None, description="Plan ID from catalog"),
    operation: str = Query(None, description="Operation identifier"),
    auth: bool = Depends(basic_auth),
    version: bool = Depends(api_version_check)
):
    """Get last operation status - CF Service Broker API v2.12 compliant"""
    db = SessionLocal()
    
    try:
        instance = db.query(ServiceInstance).filter_by(instance_id=instance_id).first()
        
        if not instance:
            # If instance not found, assume it was successfully deleted
            return JSONResponse(
                status_code=200,
                content={
                    "state": "succeeded",
                    "description": "Operation completed successfully"
                }
            )
        
        # Return current state from database
        response_data = {
            "state": instance.state,
            "description": instance.description or "Operation in progress"
        }
        
        # Add operation field if available
        if instance.operation:
            response_data["operation"] = instance.operation
        
        logger.info(f"Last operation for {instance_id}: {instance.state} - {instance.description}")
        
        return JSONResponse(
            status_code=200,
            content=response_data
        )
        
    except Exception as e:
        logger.error(f"Last operation error for {instance_id}: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"description": f"Internal server error: {str(e)}"}
        )
    finally:
        db.close()

# -------------------------------------------------------------------
# DELETE /v2/service_instances/:id (Async deprovision)
# -------------------------------------------------------------------
@app.delete("/v2/service_instances/{instance_id}")
def deprovision(
    instance_id: str,
    accepts_incomplete: bool = Query(False),
    service_id: str = Query(..., description="Service ID from catalog"),
    plan_id: str = Query(..., description="Plan ID from catalog"),
    auth: bool = Depends(basic_auth),
    version: bool = Depends(api_version_check)
):
    """Deprovision service instance - CF Service Broker API v2.12 compliant"""
    db = SessionLocal()
    
    try:
        # Validate required parameters
        if not service_id or not plan_id:
            return JSONResponse(
                status_code=400,
                content={"description": "service_id and plan_id are required"}
            )
        
        # Check if async is required but not requested
        if not accepts_incomplete:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "AsyncRequired",
                    "description": "This service plan requires client support for asynchronous service operations."
                }
            )
        
        # Check if instance exists
        instance = db.query(ServiceInstance).filter_by(instance_id=instance_id).first()
        
        if not instance:
            # Return 410 Gone if instance doesn't exist
            return JSONResponse(
                status_code=410,
                content={}
            )
        
        # Check if already being deprovisioned
        if instance.operation == "deprovision" and instance.state == "in progress":
            return JSONResponse(
                status_code=202,
                content={"operation": "deprovision"}
            )
        
        # Start async deprovisioning task
        task = deprovision_instance_task.delay(instance_id)
        
        # Update instance status
        instance.operation = "deprovision"
        instance.state = "in progress"
        instance.description = "Deprovisioning started"
        instance.celery_task_id = task.id
        db.commit()
        
        logger.info(f"Started deprovisioning for instance {instance_id}")
        
        # Return 202 Accepted for async operation
        return JSONResponse(
            status_code=202,
            content={"operation": "deprovision"}
        )
        
    except Exception as e:
        logger.error(f"Deprovisioning error for {instance_id}: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"description": f"Internal server error: {str(e)}"}
        )
    finally:
        db.close()

# Bind and unbind endpoints removed as per requirements
# This service broker does not support service binding
