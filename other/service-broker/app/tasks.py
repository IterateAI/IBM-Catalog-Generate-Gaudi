from celery import Celery
import os
import subprocess
import json
import logging
from datetime import datetime
from app.database import SessionLocal
from app.models import ServiceInstance

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL")
TERRAFORM_DIR = os.getenv("TERRAFORM_DIR", "/terraform")

celery = Celery(
    "broker_tasks",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

# Configure Celery to run only 1 task at a time
celery.conf.worker_concurrency = 1
celery.conf.task_routes = {
    'app.tasks.provision_instance_task': {'queue': 'provision'},
    'app.tasks.deprovision_instance_task': {'queue': 'deprovision'},
}

def update_instance_status(instance_id: str, state: str, description: str, logs: str = None):
    """Update service instance status in database"""
    db = SessionLocal()
    try:
        instance = db.query(ServiceInstance).filter_by(instance_id=instance_id).first()
        if instance:
            instance.state = state
            instance.description = description
            instance.updated_at = datetime.utcnow()
            if logs:
                instance.deployment_logs = logs
            db.commit()
            logger.info(f"Updated instance {instance_id}: {state} - {description}")
    except Exception as e:
        logger.error(f"Failed to update instance {instance_id}: {str(e)}")
        db.rollback()
    finally:
        db.close()

def run_terraform_command(command: list, cwd: str, timeout: int = 7200) -> tuple:
    """Run terraform command with timeout (2 hours default)"""
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logger.error(f"Terraform command timed out after {timeout} seconds")
        return -1, "", "Command timed out after 2 hours"
    except Exception as e:
        logger.error(f"Terraform command failed: {str(e)}")
        return -1, "", str(e)

@celery.task(bind=True, time_limit=7200, soft_time_limit=7000)  # 2 hour limit
def provision_instance_task(self, instance_id: str):
    """Provision service instance using Terraform"""
    db = SessionLocal()
    
    try:
        # Get instance details from database
        instance = db.query(ServiceInstance).filter_by(instance_id=instance_id).first()
        if not instance:
            logger.error(f"Instance {instance_id} not found in database")
            return {"status": "failed", "description": "Instance not found"}
        
        # Update task ID
        instance.celery_task_id = self.request.id
        db.commit()
        
        # Extract custom parameters
        email = instance.email or instance.parameters.get('email', '')
        name = instance.name or instance.parameters.get('name', '')
        
        logger.info(f"Starting provisioning for {instance_id} - Email: {email}, Name: {name}")
        
        # Use shared terraform directory (only 1 task runs at a time)
        shared_tf_dir = os.path.join(TERRAFORM_DIR, "workspace")
        os.makedirs(shared_tf_dir, exist_ok=True)
        
        # Clean previous state (since we're reusing directory)
        for file in ["terraform.tfvars", "terraform.tfstate", "terraform.tfstate.backup", "tfplan"]:
            file_path = os.path.join(shared_tf_dir, file)
            if os.path.exists(file_path):
                os.remove(file_path)
        
        # Copy your pre-built terraform files from mounted base directory
        base_tf_dir = os.path.join(TERRAFORM_DIR, "base")
        if os.path.exists(base_tf_dir):
            subprocess.run(["cp", "-r", f"{base_tf_dir}/*", shared_tf_dir], shell=True)
        else:
            error_msg = "Terraform base directory not found. Ensure host terraform directory is mounted."
            update_instance_status(instance_id, "failed", error_msg)
            return {"status": "failed", "description": error_msg}
        
        # Create terraform.tfvars with custom parameters
        tfvars_content = f"""
instance_id = "{instance_id}"
email = "{email}"
name = "{name}"
service_id = "{instance.service_id}"
plan_id = "{instance.plan_id}"
organization_guid = "{instance.organization_guid or ''}"
space_guid = "{instance.space_guid or ''}"
"""
        
        # Add any additional parameters from the request
        for key, value in (instance.parameters or {}).items():
            if key not in ['email', 'name']:  # Skip already handled params
                tfvars_content += f'{key} = "{value}"\n'
        
        tfvars_path = os.path.join(shared_tf_dir, "terraform.tfvars")
        with open(tfvars_path, 'w') as f:
            f.write(tfvars_content)
        
        update_instance_status(instance_id, "in progress", "Initializing Terraform")
        
        # Initialize Terraform
        returncode, stdout, stderr = run_terraform_command(
            ["terraform", "init"], shared_tf_dir
        )
        
        if returncode != 0:
            error_msg = f"Terraform init failed: {stderr}"
            update_instance_status(instance_id, "failed", error_msg, f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}")
            return {"status": "failed", "description": error_msg}
        
        update_instance_status(instance_id, "in progress", "Running Terraform plan")
        
        # Plan
        returncode, stdout, stderr = run_terraform_command(
            ["terraform", "plan", "-out=tfplan"], shared_tf_dir
        )
        
        if returncode != 0:
            error_msg = f"Terraform plan failed: {stderr}"
            update_instance_status(instance_id, "failed", error_msg, f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}")
            return {"status": "failed", "description": error_msg}
        
        update_instance_status(instance_id, "in progress", "Applying Terraform configuration")
        
        # Apply
        returncode, stdout, stderr = run_terraform_command(
            ["terraform", "apply", "-auto-approve", "tfplan"], shared_tf_dir
        )
        
        if returncode != 0:
            error_msg = f"Terraform apply failed: {stderr}"
            update_instance_status(instance_id, "failed", error_msg, f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}")
            return {"status": "failed", "description": error_msg}
        
        # Store terraform state path
        instance.terraform_state_path = os.path.join(shared_tf_dir, "terraform.tfstate")
        db.commit()
        
        update_instance_status(
            instance_id, 
            "succeeded", 
            "Provisioning completed successfully",
            f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        )
        
        logger.info(f"Successfully provisioned instance {instance_id}")
        return {"status": "succeeded", "description": "Provisioning completed successfully"}
        
    except Exception as e:
        error_msg = f"Provisioning failed with exception: {str(e)}"
        logger.error(error_msg)
        update_instance_status(instance_id, "failed", error_msg)
        return {"status": "failed", "description": error_msg}
    
    finally:
        db.close()

@celery.task(bind=True, time_limit=7200, soft_time_limit=7000)  # 2 hour limit
def deprovision_instance_task(self, instance_id: str):
    """Deprovision service instance using Terraform destroy"""
    db = SessionLocal()
    
    try:
        # Get instance details from database
        instance = db.query(ServiceInstance).filter_by(instance_id=instance_id).first()
        if not instance:
            logger.warning(f"Instance {instance_id} not found in database, considering it already deprovisioned")
            return {"status": "succeeded", "description": "Instance not found, already deprovisioned"}
        
        # Update task ID and operation
        instance.celery_task_id = self.request.id
        instance.operation = "deprovision"
        instance.state = "in progress"
        instance.description = "Starting deprovisioning"
        db.commit()
        
        logger.info(f"Starting deprovisioning for {instance_id}")
        
        shared_tf_dir = os.path.join(TERRAFORM_DIR, "workspace")
        
        if not os.path.exists(shared_tf_dir):
            logger.warning(f"Terraform workspace not found, considering {instance_id} already destroyed")
            # Remove from database
            db.delete(instance)
            db.commit()
            return {"status": "succeeded", "description": "No terraform workspace found, instance removed"}
        
        update_instance_status(instance_id, "in progress", "Running Terraform destroy")
        
        # Destroy
        returncode, stdout, stderr = run_terraform_command(
            ["terraform", "destroy", "-auto-approve"], shared_tf_dir
        )
        
        if returncode != 0:
            error_msg = f"Terraform destroy failed: {stderr}"
            update_instance_status(instance_id, "failed", error_msg, f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}")
            return {"status": "failed", "description": error_msg}
        
        # Clean up terraform workspace files
        try:
            for file in ["terraform.tfvars", "terraform.tfstate", "terraform.tfstate.backup", "tfplan"]:
                file_path = os.path.join(shared_tf_dir, file)
                if os.path.exists(file_path):
                    os.remove(file_path)
        except Exception as e:
            logger.warning(f"Failed to clean up terraform workspace: {str(e)}")
        
        # Remove instance from database
        db.delete(instance)
        db.commit()
        
        logger.info(f"Successfully deprovisioned instance {instance_id}")
        return {"status": "succeeded", "description": "Deprovisioning completed successfully"}
        
    except Exception as e:
        error_msg = f"Deprovisioning failed with exception: {str(e)}"
        logger.error(error_msg)
        update_instance_status(instance_id, "failed", error_msg)
        return {"status": "failed", "description": error_msg}
    
    finally:
        db.close()
