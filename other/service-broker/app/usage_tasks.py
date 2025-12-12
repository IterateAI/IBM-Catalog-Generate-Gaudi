import os
import requests
import datetime
import calendar
import time
import logging
from typing import List, Dict, Any
# from app.usage_celery import usage_celery
from app.database import SessionLocal
from app.models import ServiceInstance

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration from environment
RESOURCE_ID = os.getenv("RESOURCE_ID")
IBM_BASE_URL = "https://billing.cloud.ibm.com"

MEASURE_NAME = "total_query_count"
RETRY_COUNT = 3
RETRY_DELAY = 5  # seconds
USAGE_ENDPOINT_PATH = "generate/api/v1/usage-stats/all"
USAGE_API_KEY = os.getenv("USAGE_API_KEY")

IBMCLOUD_API_KEY = os.getenv("IBMCLOUD_API_KEY")



from celery import Celery
from celery.schedules import crontab

# Separate Redis DB for usage service to avoid conflicts with main celery
REDIS_URL = "redis://redis:6379/1"

# Create completely separate Celery instance for usage reporting
usage_celery = Celery(
    "usage_service",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

usage_celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,

    # Usage service specific settings
    worker_concurrency=1,
    worker_prefetch_multiplier=1,

    # Time limits for usage tasks
    task_time_limit=3600,       # 1 hour limit
    task_soft_time_limit=3300,  # 55 minutes soft limit

    # Reliability
    task_acks_late=True,
    worker_disable_rate_limits=True,
    task_reject_on_worker_lost=True,

    # Result backend
    result_expires=3600,
    task_ignore_result=False,

    # Beat schedule - runs on 2nd day of month at 2:00 AM UTC
    beat_schedule={
        'monthly-usage-report': {
            'task': 'app.usage_tasks.collect_and_report_usage',
            'schedule': crontab(day_of_month=2, hour=2, minute=0),
            'options': {
                'expires': 3600,  # Task expires after 1 hour if not picked up
            }
        },
    },

    # Logging
    worker_log_format='[%(asctime)s: %(levelname)s/%(processName)s] %(message)s',
    worker_task_log_format='[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s',
)

# Autodiscover usage tasks only
# usage_celery.autodiscover_tasks(["app.usage_tasks"])



def get_ibm_iam_token(iam_url: str = "https://iam.cloud.ibm.com/identity/token"):
    data = {
        "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
        "apikey": IBMCLOUD_API_KEY
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    }

    response = requests.post(iam_url, data=data, headers=headers, timeout=30)
    response.raise_for_status()

    token_response = response.json()
    access_token = token_response.get("access_token")
    expires_in = token_response.get("expires_in")

    print(f"IAM token expires in: {expires_in} seconds")

    return access_token


def previous_month_dates():
    """Return previous month start/end as YYYY-MM-DD and epoch milliseconds"""
    now = datetime.datetime.utcnow()
    year = now.year
    month = now.month - 1 or 12
    if month == 12:
        year -= 1
    first_day = datetime.datetime(year, month, 1)
    last_day = datetime.datetime(year, month, calendar.monthrange(year, month)[1], 23, 59, 59)
    start_str = first_day.strftime("%Y-%m-%d")
    end_str = last_day.strftime("%Y-%m-%d")
    start_ms = int(first_day.timestamp() * 1000)
    end_ms = int(last_day.timestamp() * 1000)
    return start_str, end_str, start_ms, end_ms

def get_active_instances() -> List[ServiceInstance]:
    """Get all active instances from database (succeeded state)"""
    db = SessionLocal()
    try:
        instances = db.query(ServiceInstance).filter(
            ServiceInstance.operation=='provision', ServiceInstance.state == "succeeded"
        ).all()
        logger.info(f"Found {len(instances)} active instances")
        return instances
    finally:
        db.close()

def get_instance_usage(cluster_url: str, start_date: str, end_date: str) -> Dict[str, Any]:
    """Call instance usage endpoint with retry logic"""
    if not cluster_url:
        raise ValueError("Cluster URL is required")
    
    # Use HTTP to avoid certificate issues
    if cluster_url.startswith("https://"):
        cluster_url = cluster_url.replace("https://", "http://")
    elif not cluster_url.startswith("http://"):
        cluster_url = f"http://{cluster_url}"
    else:
        cluster_url = f"http://{cluster_url}"
    
    usage_url = f"{cluster_url.rstrip('/')}{USAGE_ENDPOINT_PATH}"
    headers = {"Content-Type": "application/json", "apikey": USAGE_API_KEY}
    payload = {"start": start_date, "end": end_date}
    
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            logger.info(f"[Attempt {attempt}] Calling usage endpoint: {usage_url}")
            resp = requests.post(usage_url, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"[Attempt {attempt}] Usage API returned {resp.status_code}: {resp.text}")
        except Exception as e:
            logger.warning(f"[Attempt {attempt}] Usage API exception: {e}")
        
        if attempt < RETRY_COUNT:
            time.sleep(RETRY_DELAY)
    
    raise Exception(f"Failed to get usage from {usage_url} after {RETRY_COUNT} attempts")

def send_usage_to_ibm(instance_id: str, plan_id: str, region: str, start_ms: int, end_ms: int, quantity: int, ibm_token:str) -> Dict[str, Any]:
    """Send usage data to IBM metering API with retry logic"""
    if not RESOURCE_ID:
        raise ValueError("RESOURCE_ID environment variable is required")
    
    body = [{
        "resource_instance_id": instance_id,
        "plan_id": plan_id,
        "region": region,
        "start": start_ms,
        "end": end_ms,
        "measured_usage": [{"measure": str(MEASURE_NAME).upper(), "quantity": quantity}]
    }]
    
    headers = {
        "Authorization": f"Bearer {ibm_token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    url = f"{IBM_BASE_URL}/v4/metering/resources/{RESOURCE_ID}/usage"
    
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            logger.info(f"[Attempt {attempt}] Sending usage to IBM for instance {instance_id}")
            resp = requests.put(url, headers=headers, json=body, timeout=30)
            if resp.status_code == 202:
                logger.info(f"IBM metering accepted for instance {instance_id}")
                return resp.json() if resp.text else {}
            logger.warning(f"[Attempt {attempt}] IBM API returned {resp.status_code}: {resp.text}")
        except Exception as e:
            logger.warning(f"[Attempt {attempt}] IBM API exception: {e}")
        
        if attempt < RETRY_COUNT:
            time.sleep(RETRY_DELAY)
    
    raise Exception(f"Failed to send usage to IBM for instance {instance_id} after {RETRY_COUNT} attempts")

@usage_celery.task(bind=True, time_limit=3600, soft_time_limit=3300)
def collect_and_report_usage(self):
    """Monthly task to collect usage from all active instances and report to IBM"""
    logger.info("Starting monthly usage collection and reporting")
    
    try:
        # Get previous month date range
        start_str, end_str, start_ms, end_ms = previous_month_dates()
        logger.info(f"Processing usage for period: {start_str} to {end_str}")
        
        # Get all active instances
        active_instances = get_active_instances()
        if not active_instances:
            logger.info("No active instances found")
            return {"status": "completed", "message": "No active instances to process"}
        
        results = []
        successful_reports = 0
        failed_reports = 0

        ibm_token = get_ibm_iam_token()
        
        for instance in active_instances:
            instance_result = {
                "instance_id": instance.instance_id,
                "cluster_url": instance.cluster_url,
                "status": "pending"
            }
            
            try:
                # Skip instances without cluster URL
                if not instance.cluster_url:
                    instance_result["status"] = "skipped"
                    instance_result["reason"] = "No cluster URL configured"
                    logger.warning(f"Skipping instance {instance.instance_id}: No cluster URL")
                    results.append(instance_result)
                    continue
                
                # Get usage data from instance
                logger.info(f"Collecting usage for instance {instance.instance_id}")
                usage_data = get_instance_usage(instance.cluster_url, start_str, end_str)
                
    
                units_used = usage_data.get(MEASURE_NAME, 0)
                instance_result["usage_collected"] = units_used
                
                # Send to IBM
                logger.info(f"Reporting {units_used} healthcare units for instance {instance.instance_id}")
                ibm_response = send_usage_to_ibm(
                    instance.instance_id, 
                    instance.plan_id,
                    instance.ibm_region,
                    start_ms, 
                    end_ms, 
                    units_used,
                    ibm_token
                )
                
                instance_result["status"] = "success"
                instance_result["ibm_response"] = ibm_response
                successful_reports += 1
                
            except Exception as e:
                logger.error(f"Failed to process instance {instance.instance_id}: {str(e)}")
                instance_result["status"] = "failed"
                instance_result["error"] = str(e)
                failed_reports += 1
            
            results.append(instance_result)
        
        summary = {
            "status": "completed",
            "period": f"{start_str} to {end_str}",
            "total_instances": len(active_instances),
            "successful_reports": successful_reports,
            "failed_reports": failed_reports,
            "results": results
        }
        
        logger.info(f"Usage reporting completed: {successful_reports} successful, {failed_reports} failed")
        return summary
        
    except Exception as e:
        error_msg = f"Usage collection task failed: {str(e)}"
        logger.error(error_msg)
        return {"status": "failed", "error": error_msg}
