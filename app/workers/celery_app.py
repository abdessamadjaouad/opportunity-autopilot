from celery import Celery

from app.config import Settings
from app.db import Database

settings = Settings()
celery_app = Celery("opportunity_autopilot", broker=settings.redis_url)
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    timezone="Africa/Casablanca", enable_utc=True, task_ignore_result=True,
    worker_prefetch_multiplier=1, task_acks_late=True, task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True, broker_transport_options={"visibility_timeout": 900},
    task_soft_time_limit=540, task_time_limit=600,
    beat_schedule={"durable-tick": {"task": "oa.tick", "schedule": 30.0},
                   "dispatch": {"task": "oa.dispatch", "schedule": 15.0}},
)


def database():
    settings.validate_runtime()
    db = Database(settings.db_url)
    db.initialize()
    return db


@celery_app.task(name="oa.tick")
def tick():
    from app.workers.scheduler import run_tick
    db = database()
    try:
        return run_tick(db, settings, max_jobs=10)
    finally:
        db.engine.dispose()


@celery_app.task(name="oa.dispatch", acks_late=True)
def dispatch():
    from app.applications.dispatch import dispatch_next
    db = database()
    try:
        from app.applications.notifications import dispatch_notification
        result = dispatch_next(db, settings)
        dispatch_notification(db, settings)
        from app.applications.followups import dispatch_followup
        dispatch_followup(db, settings)
        return result
    finally:
        db.engine.dispose()
