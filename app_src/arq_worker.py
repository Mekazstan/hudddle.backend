import logging
from arq.jobs import Job
from app_src.db.db_connect import async_session
from app_src.mail import get_mail
from app_src.config import Config
from arq import cron
from arq.connections import RedisSettings
from app_src.arq_tasks import (
    send_email_task,
    send_workroom_invites,
    process_image_and_store_task,
    process_workroom_end_session,
    email_daily_performance_to_managers
)

class WorkerSettings:
    functions = [
        send_email_task,
        send_workroom_invites,
        process_image_and_store_task,
        process_workroom_end_session,
        email_daily_performance_to_managers
    ]
    redis_settings = RedisSettings.from_dsn(Config.REDIS_URL)
    cron_jobs = [
        cron(
            email_daily_performance_to_managers,
            hour=20,
            minute=0,
            name="send_daily_manager_reports"
        )
    ]
    
    job_timeout = 300
    max_jobs = 10
    queue_name = "arq:queue"
    
    async def startup(ctx):
        ctx['mail'] = get_mail()
        ctx['session_maker'] = async_session
        
        # Test connections
        try:
            await ctx['mail'].get_mail_server()
            logging.info("Mail server connection verified")
        except Exception as e:
            logging.error(f"Mail server connection failed: {e}")
            raise
        
        logging.info("Worker startup complete")

    async def shutdown(ctx):
        pass
