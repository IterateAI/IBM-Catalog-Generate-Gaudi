from celery import Celery
import os
import subprocess
import json
import logging
from datetime import datetime
from app.database import SessionLocal
from app.models import ServiceInstance
from app.celery import celery

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL")
TERRAFORM_DIR = os.getenv("TERRAFORM_DIR", "/terraform")
DOCKER_USER = os.getenv("DOCKER_USER")
DOCKER_PASS = os.getenv("DOCKER_PASS")
IBMCLOUD_API_KEY = os.getenv("IBMCLOUD_API_KEY")

def update_instance_status(instance_id: str, state: str, description: str, logs: str = None):
    """Update service instance status in database"""
    db = SessionLocal()
    try:
        instance = db.query(ServiceInstance).filter_by(instance_id=instance_id).first()
        if instance:
            instance.state = state
            instance.description = description
            instance.updated_at = datetime.utcnow()
            # if logs:
            #     instance.deployment_logs = logs
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
def provision_instance_task(self, instance_id: str, healthcare_units: int, ibm_region: str, instance_zone: str, cluster_url: str, user_cert: str, user_key: str):
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
        
        # Copy base terraform files ONCE if workspace is empty of .tf files
        base_tf_dir = os.path.join(TERRAFORM_DIR, "base")
        tf_files_exist = any(f.endswith('.tf') for f in os.listdir(shared_tf_dir) if os.path.isfile(os.path.join(shared_tf_dir, f)))
        
        if not tf_files_exist:
            if os.path.exists(base_tf_dir):
                # Copy all base files once (excluding any existing state files)
                # Make sure shared_tf_dir exists
                subprocess.run(["mkdir", "-p", shared_tf_dir], check=True)

                # Copy everything from base_tf_dir into shared_tf_dir (files + folders)
                subprocess.run(["cp", "-r", f"{base_tf_dir}/.", shared_tf_dir], check=True)
                logger.info("Copied base Terraform files to workspace")
            else:
                error_msg = "Terraform base directory not found. Ensure host terraform directory is mounted."
                update_instance_status(instance_id, "failed", error_msg)
                return {"status": "failed", "description": error_msg}
        
        # Clean only temporary files, preserve all state files
        for file in ["terraform.tfvars", "tfplan"]:
            file_path = os.path.join(shared_tf_dir, file)
            if os.path.exists(file_path):
                os.remove(file_path)
        
        # Instance-specific state file path
        instance_state_file = os.path.join(shared_tf_dir, f"terraform-{instance_id}.tfstate")

        user_cert_literal = user_cert.replace("\n", "\\n")
        user_key_literal = user_key.replace("\n", "\\n")
        
        # Create terraform.tfvars with custom parameters
        tfvars_content = f"""
models = "21"
hugging_face_token = "hf_dummy"
deployment_mode = "single-node"
worker_gaudi_count = 3 
ssh_allowed_cidr = "0.0.0.0/0"
vault_pass_code = "pass" 
instance_profile = "cx3d-32x80"
gaudi_image = "ibm-ubuntu-22-04-5-minimal-amd64-2"
xeon_image = "ibm-ubuntu-22-04-5-minimal-amd64-2"
cpu_or_gpu = "cpu"
image = "ibm-ubuntu-22-04-5-minimal-amd64-2"
ssh_key = "service-broker-key"
ssh_private_key = "/app/keys/id_rsa"
resource_group = "enterprise-inference-rg"

ibmcloud_api_key = "{IBMCLOUD_API_KEY}"
generate_enterprise_docker_user = "{DOCKER_USER}"
generate_enterprise_docker_password = "{DOCKER_USER}"

ibmcloud_region = "{ibm_region}"
instance_zone = "{instance_zone}"
cluster_url = "{cluster_url}"
user_cert = "{user_cert_literal}"
user_key = "{user_key_literal}"
healthcare_units = {healthcare_units}
"""
        
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
        
        # Plan with instance-specific state
        returncode, stdout, stderr = run_terraform_command(
            ["terraform", "plan", f"-state={instance_state_file}", "-out=tfplan"], shared_tf_dir
        )
        
        if returncode != 0:
            error_msg = f"Terraform plan failed: {stderr}"
            update_instance_status(instance_id, "failed", error_msg, f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}")
            return {"status": "failed", "description": error_msg}
        
        update_instance_status(instance_id, "in progress", "Applying Terraform configuration")
        
        # Apply with instance-specific state
        returncode, stdout, stderr = run_terraform_command(
            ["terraform", "apply", f"-state={instance_state_file}", "-auto-approve", "tfplan"], shared_tf_dir
        )
        
        if returncode != 0:
            error_msg = f"Terraform apply failed: {stderr}"
            update_instance_status(instance_id, "failed", error_msg, f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}")
            return {"status": "failed", "description": error_msg}
        
        # Store terraform state path (instance-specific)
        instance.terraform_state_path = instance_state_file
        db.commit()
        
        update_instance_status(
            instance_id, 
            "succeeded", 
            "Successfully provisioned",
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
        
        update_instance_status(instance_id, "in progress", "Preparing Terraform destroy")
        
        # Base .tf files should already be in workspace from provisioning
        # No need to copy again - just recreate tfvars and use existing state
        
        # Recreate terraform.tfvars with original provisioning parameters
        # This is CRITICAL - Terraform needs the same variables to know what to destroy
        tfvars_content = f"""
models = "21"
hugging_face_token = "hf_dummy"
deployment_mode = "single-node"
worker_gaudi_count = 3 
ssh_allowed_cidr = "0.0.0.0/0"
vault_pass_code = "pass" 
instance_profile = "cx3d-32x80"
gaudi_image = "ibm-ubuntu-22-04-5-minimal-amd64-2"
xeon_image = "ibm-ubuntu-22-04-5-minimal-amd64-2"
cpu_or_gpu = "cpu"
image = "ibm-ubuntu-22-04-5-minimal-amd64-2"
ssh_key = "service-broker-key"
ssh_private_key = "/app/keys/id_rsa"
resource_group = "enterprise-inference-rg"

ibmcloud_api_key = "{IBMCLOUD_API_KEY}"
generate_enterprise_docker_user = "{DOCKER_USER}"
generate_enterprise_docker_password = "{DOCKER_PASS}"

ibmcloud_region = "{instance.ibm_region or 'us-south'}"
instance_zone = "{instance.instance_zone or 'us-south-1'}"
cluster_url = "{instance.cluster_url or ''}"
user_cert = "-----BEGIN CERTIFICATE-----\\nDUMMYCERTDATA\\n-----END CERTIFICATE-----"
user_key = "-----BEGIN PRIVATE KEY-----\\nDUMMYKEYDATA\\n-----END PRIVATE KEY-----"
healthcare_units = {instance.healthcare_units}
"""
        
        tfvars_path = os.path.join(shared_tf_dir, "terraform.tfvars")
        with open(tfvars_path, 'w') as f:
            f.write(tfvars_content)
        
        update_instance_status(instance_id, "in progress", "Running Terraform destroy")
        
        # Use the stored state file path for this specific instance
        if instance.terraform_state_path and os.path.exists(instance.terraform_state_path):
            state_file = instance.terraform_state_path
        else:
            # Fallback to expected location if stored path is missing
            state_file = os.path.join(shared_tf_dir, f"terraform-{instance_id}.tfstate")
        
        # Destroy with the instance-specific state file
        returncode, stdout, stderr = run_terraform_command(
            ["terraform", "destroy", f"-state={state_file}", "-auto-approve"], shared_tf_dir
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
        
        # TESTING: Just mark as succeeded and remove from database
        update_instance_status(instance_id, "succeeded", "Deprovisioning successfull")
        
        # Remove instance from database
        db.delete(instance)
        db.commit()
        
        logger.info(f"Successfully deprovisioned instance {instance_id}")
        return {"status": "succeeded", "description": "TEST: Deprovisioning completed successfully"}
        
    except Exception as e:
        error_msg = f"Deprovisioning failed with exception: {str(e)}"
        logger.error(error_msg)
        update_instance_status(instance_id, "failed", error_msg)
        return {"status": "failed", "description": error_msg}
    
    finally:
        db.close()
