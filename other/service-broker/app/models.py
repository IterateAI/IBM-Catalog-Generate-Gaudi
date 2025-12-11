from sqlalchemy import Column, String, JSON, DateTime, Text
from datetime import datetime
from .database import Base

class ServiceInstance(Base):
    __tablename__ = "service_instances"

    instance_id = Column(String, primary_key=True, index=True)
    service_id = Column(String, nullable=False)
    plan_id = Column(String, nullable=False)
    organization_guid = Column(String, nullable=True)
    space_guid = Column(String, nullable=True)
    dashboard_url = Column(String, nullable=True)
    parameters = Column(JSON)  # Store all custom parameters including email, name
    operation = Column(String, nullable=True)   # "provision" or "deprovision"
    state = Column(String, default="in progress")  # "in progress", "succeeded", "failed"
    description = Column(String, nullable=True)
    
    # Custom parameter fields for easy access
    email = Column(String, nullable=True)
    name = Column(String, nullable=True)
    
    # Terraform deployment tracking
    terraform_state_path = Column(String, nullable=True)
    deployment_logs = Column(Text, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Celery task tracking
    celery_task_id = Column(String, nullable=True)
