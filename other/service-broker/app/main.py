from fastapi import FastAPI, Depends, HTTPException, Query, status, Request
from fastapi.responses import JSONResponse
from app.auth import api_version_check, basic_auth
from app.schemas import ProvisionRequest, UpdateRequest
from app.database import SessionLocal, Base, engine
from app.models import ServiceInstance
from app.tasks import provision_instance_task, deprovision_instance_task
from app.usage_tasks import collect_and_report_usage
import logging

logger = logging.getLogger(__name__)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Generate Enterprise Service Broker")


@app.get("/health")
def health():
    return {"status": "ok"}


# -------------------------------------------------------------------
# GET /v2/catalog
# -------------------------------------------------------------------
@app.get("/v2/catalog")
def get_catalog(
    auth: bool = Depends(basic_auth), version: bool = Depends(api_version_check)
):
    """Return service catalog - CF Service Broker API v2.12 compliant"""
    return {
        "services": [
            {
                "id": "0bc9d744-6f8c-4821-9999-2278bf6925cc",
                "name": "ibm-cloud-generate-enterprise-saas",
                "description": "Generate Enterprise service provisioned via Terraform",
                "bindable": False,
                "plan_updateable": False,
                "plans": [
                    {
                        "name": "base",
                        "id": "a08e6f90-e19a-4baa-9966-73ef24222b3d",
                        "description": "This pricing plan uses a custom metric “HEALTHCARE_UNIT” defined specifically for your deployment. Number of monthly Healthcare Units will be decided based on the discussion with client. Please reach out to sales@iterate.ai to discuss the requirement.",
                        "free": False,
                    }
                ],
            }
        ]
    }


# -------------------------------------------------------------------
# PUT /v2/service_instances/:id (Async provisioning)
# -------------------------------------------------------------------


# @app.put("/v2/service_instances/{instance_id:path}")
# async def provision(instance_id: str, request: Request):
#     raw_body = await request.body()
#     print("instance_id:", instance_id)
#     print("RAW BODY:", raw_body.decode())  # prints raw JSON
#     raise HTTPException(status_code=400, detail="Intentional bad request for testing")


@app.put("/v2/service_instances/{instance_id:path}")
def provision(
    instance_id: str,
    body: ProvisionRequest,
    accepts_incomplete: bool = Query(False),
    auth: bool = Depends(basic_auth),
    version: bool = Depends(api_version_check),
):
    """Provision service instance - CF Service Broker API v2.12 compliant"""
    db = SessionLocal()

    try:
        # Get service_id and plan_id from request body (CF spec compliant)
        service_id = body.service_id
        plan_id = body.plan_id

        # Validate required parameters
        if not service_id or not plan_id:
            return JSONResponse(
                status_code=400,
                content={"description": "service_id and plan_id are required"},
            )

        # Check if async is required but not requested
        if not accepts_incomplete:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "AsyncRequired",
                    "description": "This service plan requires client support for asynchronous service operations.",
                },
            )

        # Check if instance already exists (idempotency)
        existing = db.query(ServiceInstance).filter_by(instance_id=instance_id).first()
        if existing:
            if existing.state == "succeeded":
                # Return 200 for already provisioned instance
                return JSONResponse(status_code=200, content={"operation": "provision"})
            elif existing.state == "in progress":
                # Return 202 for ongoing provisioning
                return JSONResponse(status_code=202, content={"operation": "provision"})
            elif existing.state == "failed":
                # Allow retry for failed instances
                existing.state = "in progress"
                existing.description = "Retrying provisioning"
                db.commit()

        # Extract custom parameters
        email = body.parameters.get("Email", "").strip() if body.parameters else ""
        name = body.parameters.get("Name", "").strip() if body.parameters else ""
        org = body.parameters.get("Organization", "").strip() if body.parameters else ""
        healthcare_units = (
            body.parameters.get("Number-of-Healthcare-Units", "").strip()
            if body.parameters
            else ""
        )
        ibm_region = (
            body.parameters.get("IBM-Cloud-Region", "").strip()
            if body.parameters
            else ""
        )
        instance_zone = (
            body.parameters.get("Instance-Zone", "").strip() if body.parameters else ""
        )
        cluster_url = (
            body.parameters.get("Cluster-URL", "").strip() if body.parameters else ""
        )
        user_cert = (
            body.parameters.get("Full-Chain-Domain-Cert", "").strip()
            if body.parameters
            else ""
        )
        user_key = (
            body.parameters.get("Private-Key-Domain-Cert", "").strip()
            if body.parameters
            else ""
        )

        if (
            not email
            or not name
            or not org
            or not healthcare_units
            or not ibm_region
            or not instance_zone
            or not cluster_url
            or not user_cert
            or not user_key
        ):
            return JSONResponse(
                status_code=400,
                content={"description": "All custom parameters are required"},
            )

        healthcare_units = int(healthcare_units)

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
                org=org,
                healthcare_units=str(healthcare_units),
                ibm_region=ibm_region,
                instance_zone=instance_zone,
                cluster_url=cluster_url,
                operation="provision",
                state="in progress",
                description="Provisioning started",
                dashboard_url=f"http://dashboard.ibm/{instance_id}",
            )
            db.add(instance)
            db.commit()

        # Start async provisioning task
        task = provision_instance_task.delay(
            instance_id,
            healthcare_units,
            ibm_region,
            instance_zone,
            cluster_url,
            user_cert,
            user_key,
        )

        # Update task ID in database
        if existing:
            existing.celery_task_id = task.id
        else:
            instance.celery_task_id = task.id
        db.commit()

        logger.info(
            f"Started provisioning for instance {instance_id} with email: {email}, name: {name}"
        )

        # Return 202 Accepted for async operation
        return JSONResponse(status_code=202, content={"operation": "provision"})

    except Exception as e:
        logger.error(f"Provisioning error for {instance_id}: {str(e)}")
        return JSONResponse(
            status_code=500, content={"description": f"Internal server error: {str(e)}"}
        )
    finally:
        db.close()


# -------------------------------------------------------------------
# GET /v2/service_instances/:id/last_operation
# -------------------------------------------------------------------
@app.get("/v2/service_instances/{instance_id:path}/last_operation")
def last_operation(
    instance_id: str,
    service_id: str = Query(None, description="Service ID from catalog"),
    plan_id: str = Query(None, description="Plan ID from catalog"),
    operation: str = Query(None, description="Operation identifier"),
    auth: bool = Depends(basic_auth),
    version: bool = Depends(api_version_check),
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
                    "description": "Operation completed successfully",
                },
            )

        # Return current state from database
        response_data = {
            "state": instance.state,
            "description": instance.description or "Operation in progress",
        }

        # Add operation field if available
        if instance.operation:
            response_data["operation"] = instance.operation

        logger.info(
            f"Last operation for {instance_id}: {instance.state} - {instance.description}"
        )

        return JSONResponse(status_code=200, content=response_data)

    except Exception as e:
        logger.error(f"Last operation error for {instance_id}: {str(e)}")
        return JSONResponse(
            status_code=500, content={"description": f"Internal server error: {str(e)}"}
        )
    finally:
        db.close()


# -------------------------------------------------------------------
# PATCH /v2/service_instances/:id (Update - Not Supported)
# -------------------------------------------------------------------
@app.patch("/v2/service_instances/{instance_id:path}")
def update_service_instance(
    instance_id: str,
    body: UpdateRequest,
    accepts_incomplete: bool = Query(False),
    auth: bool = Depends(basic_auth),
    version: bool = Depends(api_version_check),
):
    """Update service instance - Not supported as plan_updateable is false"""
    # If no changes requested, return 200 OK
    return JSONResponse(
        status_code=422,
        content={
            "error": "ParameterChangeNotSupported",
            "description": "This service does not support parameter changes.",
        },
    )


# -------------------------------------------------------------------
# DELETE /v2/service_instances/:id (Async deprovision)
# -------------------------------------------------------------------
@app.delete("/v2/service_instances/{instance_id:path}")
def deprovision(
    instance_id: str,
    accepts_incomplete: bool = Query(False),
    service_id: str = Query(..., description="Service ID from catalog"),
    plan_id: str = Query(..., description="Plan ID from catalog"),
    auth: bool = Depends(basic_auth),
    version: bool = Depends(api_version_check),
):
    """Deprovision service instance - CF Service Broker API v2.12 compliant"""
    db = SessionLocal()

    try:
        # For DELETE operations, service_id and plan_id are query parameters per CF spec
        if not service_id or not plan_id:
            return JSONResponse(
                status_code=400,
                content={"description": "service_id and plan_id are required"},
            )

        # Check if async is required but not requested
        if not accepts_incomplete:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "AsyncRequired",
                    "description": "This service plan requires client support for asynchronous service operations.",
                },
            )

        # Check if instance exists
        instance = db.query(ServiceInstance).filter_by(instance_id=instance_id).first()

        if not instance:
            # Return 410 Gone if instance doesn't exist
            return JSONResponse(status_code=410, content={})

        # Check if already being deprovisioned
        if instance.operation == "deprovision" and instance.state == "in progress":
            return JSONResponse(status_code=202, content={"operation": "deprovision"})

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
        return JSONResponse(status_code=202, content={"operation": "deprovision"})

    except Exception as e:
        logger.error(f"Deprovisioning error for {instance_id}: {str(e)}")
        return JSONResponse(
            status_code=500, content={"description": f"Internal server error: {str(e)}"}
        )
    finally:
        db.close()


# -------------------------------------------------------------------
# DEBUG: GET /debug/instances
# -------------------------------------------------------------------


@app.get("/debug/instances")
def list_instances(auth: bool = Depends(basic_auth)):
    """Debug endpoint to list all service instances in database"""
    db = SessionLocal()
    try:
        instances = db.query(ServiceInstance).all()

        result = []
        for instance in instances:
            result.append(
                {
                    "instance_id": instance.instance_id,
                    "service_id": instance.service_id,
                    "plan_id": instance.plan_id,
                    "operation": instance.operation,
                    "state": instance.state,
                    "description": instance.description,
                    "celery_task_id": instance.celery_task_id,
                    "terraform_state_path": instance.terraform_state_path,
                    "email": instance.email,
                    "name": instance.name,
                    "org": instance.org,
                    "healthcare_units": instance.healthcare_units,
                    "ibm_region": instance.ibm_region,
                    "instance_zone": instance.instance_zone,
                    "cluster_url": instance.cluster_url,
                    "created_at": (
                        instance.created_at.isoformat() if instance.created_at else None
                    ),
                    "updated_at": (
                        instance.updated_at.isoformat() if instance.updated_at else None
                    ),
                }
            )

        return {"total_instances": len(result), "instances": result}

    except Exception as e:
        logger.error(f"Error listing instances: {str(e)}")
        return JSONResponse(
            status_code=500, content={"error": f"Failed to list instances: {str(e)}"}
        )
    finally:
        db.close()


@app.post("/debug/trigger-usage-report")
def trigger_usage_report(auth: bool = Depends(basic_auth)):
    """Debug endpoint to manually trigger the monthly usage collection and reporting task"""
    try:
        task = collect_and_report_usage.delay()
        logger.info(f"Manually triggered usage report task with ID: {task.id}")
        
        return {
            "status": "triggered",
            "message": "Usage collection and reporting task has been queued",
            "task_id": task.id
        }
    
    except Exception as e:
        logger.error(f"Error triggering usage report: {str(e)}")
        return JSONResponse(
            status_code=500, 
            content={"error": f"Failed to trigger usage report: {str(e)}"}
        )


# Bind and unbind endpoints removed as per requirements
# This service broker does not support service binding
