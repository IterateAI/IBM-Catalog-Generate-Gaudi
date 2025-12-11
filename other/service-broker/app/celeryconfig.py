"""
Celery configuration for Service Broker
Ensures single task execution and proper timeout handling
"""
import os

# Broker settings
broker_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
result_backend = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

# Task settings
task_serializer = 'json'
result_serializer = 'json'
accept_content = ['json']
timezone = 'UTC'
enable_utc = True

# Worker settings - Only 1 task at a time to avoid conflicts
worker_concurrency = 1
worker_prefetch_multiplier = 1

# Task routing - Use default queue for simplicity
# task_routes = {
#     'app.tasks.provision_instance_task': {'queue': 'provision'},
#     'app.tasks.deprovision_instance_task': {'queue': 'deprovision'},
# }

# Task time limits (2 hours)
task_time_limit = 7200  # Hard limit
task_soft_time_limit = 7000  # Soft limit

# Task retry settings
task_acks_late = True
worker_disable_rate_limits = True

# Result backend settings
result_expires = 3600  # 1 hour

# Task execution settings
task_reject_on_worker_lost = True
task_ignore_result = False

# Logging
worker_log_format = '[%(asctime)s: %(levelname)s/%(processName)s] %(message)s'
worker_task_log_format = '[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s'
