import logging
from app_src.db.db_connect import async_session
from app_src.mail import mail_service
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

logger = logging.getLogger('arq.worker')

# Define startup and shutdown functions at module level
async def startup(ctx):
    logger.info("🚀 Starting worker initialization...")
    
    try:
        ctx['session_maker'] = async_session
        ctx['mail'] = mail_service
        
        # Test connections
        if not await ctx['mail'].test_connection():
            raise ConnectionError("Failed to connect to mail server")
        
        logger.info("✅ Mail server connection verified")
        logger.info("🏁 Worker startup complete")
    except Exception as e:
        logging.error(f"❌ Worker startup failed: {e}")
        raise

async def shutdown(ctx):
    logging.info("🛑 Worker shutting down")
    pass

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
    
    # Reference the module-level functions
    on_startup = startup
    on_shutdown = shutdown





logger.info("🚀 Starting worker initialization...")