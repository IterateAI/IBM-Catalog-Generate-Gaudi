from celery import Celery

REDIS_URL = "redis://redis:6379/0"

celery = Celery(
    "broker_tasks",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,

    # Worker executes only ONE task at a time
    worker_concurrency=1,
    worker_prefetch_multiplier=1,

    # Time limits
    task_time_limit=7200,       # Hard limit
    task_soft_time_limit=7000,  # Soft limit

    # Reliability
    task_acks_late=True,
    worker_disable_rate_limits=True,
    task_reject_on_worker_lost=True,

    # Result backend
    result_expires=3600,
    task_ignore_result=False,

    # Logging
    worker_log_format='[%(asctime)s: %(levelname)s/%(processName)s] %(message)s',
    worker_task_log_format='[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s',
)

celery.autodiscover_tasks(["app.tasks"])