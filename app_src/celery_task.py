import logging
from typing import List
from uuid import UUID
from celery import Celery, shared_task
from celery.utils.log import get_task_logger
import asyncio
from celery.signals import setup_logging
from celery.schedules import crontab
from datetime import datetime, timezone
from sqlalchemy import select
from app_src.mail import mail, create_message
from app_src.config import Config
from app_src.db.db_connect import async_session
from app_src.db.models import (User, Workroom, WorkroomLiveSession, 
                       WorkroomMemberLink, WorkroomPerformanceMetric)
from app_src.workroom.service import (analyze_image, calculate_workroom_kpi_overview, 
    generate_user_session_summary, store_analysis_result, delete_s3_object, process_audio,
    store_audio_analysis_report, analyze_text_from_audio, update_workroom_leaderboard
)
from asgiref.sync import async_to_sync

logger = get_task_logger(__name__)
CELERY_BROKER_URL = Config.CELERY_BROKER_URL
CELERY_RESULT_BACKEND = Config.CELERY_RESULT_BACKEND
DOMAIN = Config.DOMAIN
HUDDDLE_LINK = Config.HUDDDLE_LINK

celery_app = Celery('tasks',
                    broker=CELERY_BROKER_URL,
                    backend=CELERY_RESULT_BACKEND)

celery_app.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    timezone='UTC',
    enable_utc=True,
)

celery_app.conf.beat_schedule = {
    'send-daily-manager-reports': {
        'task': 'tasks.email_daily_performance_to_managers',
        'schedule': crontab(hour=20, minute=0),
    },
}

@setup_logging.connect
def configure_logging(**kwargs):
    logging.basicConfig(level=logging.INFO)

@celery_app.task(queue="first")
async def send_email_task(recipients, subject, body):
    logger.info(f"Task send_email_task started for recipients: {recipients}")
    message = create_message(recipients, subject, body)
    try:
        logger.info(f"Attempting to send message to {message.recipients}")
        await mail.send_message(message)
        logger.info(f"Email sent successfully to {message.recipients}")
    except Exception as e:
        logger.error(f"Error sending email to {message.recipients}: {e}", exc_info=True)
        raise

@celery_app.task(queue="images")
def process_image_and_store_task(user_id: str, session_id: str, image_url: str, image_filename: str, timestamp_str: str):
    try:
        async def process():
            async with async_session() as session:
                timestamp = datetime.fromisoformat(timestamp_str)

                workroom_live_session = await session.get(WorkroomLiveSession, session_id)
                if not workroom_live_session:
                    logger.warning(f"Live session not found: {session_id}")
                    return

                workroom = await session.get(Workroom, workroom_live_session.workroom_id)
                if not workroom:
                    logger.warning(f"Workroom not found: {workroom_live_session.workroom_id}")
                    return

                kpis = workroom.kpis or {}
                logger.info(f"Analyzing image {image_url} for KPIs: {kpis}")
                analysis_result = await analyze_image(image_url, kpis)

                if not analysis_result:
                    logger.warning(f"Image analysis failed for {image_url}.")
                    return

                logger.info(f"Image analysis result: {analysis_result}")
                await store_analysis_result(analysis_result, image_filename)
                await session.commit()
                await delete_s3_object(image_filename)

        async_to_sync(process)()

    except Exception as e:
        logger.error(f"Error processing image: {e}", exc_info=True)

@celery_app.task()
def process_audio_and_store_report_task(user_id: str, session_id: str, audio_url: str, audio_s3_key: str, timestamp_str: str):
    async def inner():
        try:
            transcript = await process_audio(audio_url)
            timestamp = datetime.fromisoformat(timestamp_str)

            async with async_session() as session:
                session_obj = await session.get(WorkroomLiveSession, session_id)
                if not session_obj:
                    logger.warning(f"Session not found: {session_id}")
                    return

                workroom = await session.get(Workroom, session_obj.workroom_id)
                if not workroom:
                    logger.warning(f"Workroom not found: {session_obj.workroom_id}")
                    return

                kpis = workroom.kpis or {}
                analysis_result = await analyze_text_from_audio(transcript, kpis)

                if not analysis_result:
                    logger.warning(f"Analysis failed for {audio_url}")
                    return

                report_filename = (
                    f"user_{user_id}/session_{session_id}/audio_analysis_{timestamp.strftime('%Y%m%d_%H%M%S')}.json"
                )

                if not await store_audio_analysis_report(analysis_result.json(), report_filename):
                    logger.error("Failed to store audio analysis report")
                    return

                for activity in analysis_result.activities:
                    if not activity.kpi_name:
                        logger.warning(f"Missing KPI name in activity: {activity}")
                        continue

                    result = await session.execute(
                        select(WorkroomPerformanceMetric).where(
                            WorkroomPerformanceMetric.workroom_id == workroom.id,
                            WorkroomPerformanceMetric.user_id == user_id,
                            WorkroomPerformanceMetric.kpi_name == activity.kpi_name,
                        )
                    )
                    metric = result.scalar_one_or_none()
                    if metric:
                        metric.metric_value += 1
                    else:
                        session.add(WorkroomPerformanceMetric(
                            workroom_id=workroom.id,
                            user_id=user_id,
                            kpi_name=activity.kpi_name,
                            metric_value=1
                        ))

                await session.commit()
            await delete_s3_object(audio_s3_key)
        except Exception as e:
            logger.error(f"Error processing audio: {e}")
            raise

    loop = asyncio.get_event_loop()
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    loop.run_until_complete(inner())

@celery_app.task(queue="sessions")
def process_workroom_end_session(workroom_id: str, session_id: str, user_id: str):
    try:
        async def process():
            async with async_session() as session:
                logger.info(f"📦 Processing closeout for session {session_id} in workroom {workroom_id}")

                await generate_user_session_summary(UUID(workroom_id), UUID(session_id), UUID(user_id), session)
                await update_workroom_leaderboard(UUID(workroom_id), session)
                await calculate_workroom_kpi_overview(UUID(workroom_id), session)

                live_session_result = await session.execute(
                    select(WorkroomLiveSession).where(WorkroomLiveSession.id == UUID(session_id))
                )
                live_session = live_session_result.scalar_one_or_none()

                if not live_session:
                    logger.error(f"Session {session_id} not found during cleanup.")
                    return

                live_session.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
                live_session.is_active = False
                await session.commit()
                await session.refresh(live_session)

                logger.info(f"✅ Session {session_id} marked ended and deleted.")

        async_to_sync(process)()

    except Exception as e:
        logger.error(f"❌ Error processing session {session_id}: {e}", exc_info=True)
        raise

# Using states (machines) to manage tasks 
@celery_app.task(queue="emails", bind=True)
async def send_workroom_invite_email_task(self, workroom_id: str, creator_name: str, friend_emails: List[str]):
    try:
        logging.info(f"Task send_workroom_invite_email_task started for workroom {workroom_id}")
        workroom_uuid = UUID(workroom_id)
        async with async_session() as session:
            async with session.begin():
                logging.info("DB session opened for invite task")
                
                invite_url = Config.HUDDDLE_LINK

                workroom = (await session.execute(
                    select(Workroom).where(Workroom.id == workroom_uuid)
                )).scalar_one_or_none()
                
                if not workroom:
                    logging.error(f"Workroom with ID {workroom_uuid} not found")
                    return
                logging.info(f"Workroom {workroom.name} found for invite task.")
                
                members_to_add = []

                for friend_email in friend_emails:
                    try:
                        logging.info(f"Attempting to send email to {friend_email}")
                        email_body = f"""
                        <!DOCTYPE html>
                        <html lang="en">
                        <head>
                            <meta charset="UTF-8">
                            <meta name="viewport" content="width=device-width, initial-scale=1.0">
                            <title>You are Invited...</title>
                            <style>
                                body {{
                                    font-family: Arial, sans-serif;
                                    margin: 0;
                                    padding: 0;
                                    background-color: #f4f4f4;
                                    color: #333;
                                    line-height: 1.6;
                                }}
                                .email-wrapper {{
                                    max-width: 600px;
                                    margin: 20px auto;
                                    background-color: #ffffff;
                                    border-radius: 8px;
                                    overflow: hidden;
                                    box-shadow: 0 0 10px rgba(0, 0, 0, 0.1);
                                }}
                                .email-header {{
                                    background-color: #9b87f5;
                                    text-align: center;
                                    padding: 30px;
                                }}
                                .logo {{
                                    color: white;
                                    font-size: 24px;
                                    font-weight: bold;
                                    letter-spacing: 1px;
                                }}
                                .container {{
                                    padding: 30px 40px;
                                    text-align: center;
                                    background-color: #f7f7f7;
                                }}
                                h1 {{
                                    color: #1A1F2C;
                                    font-size: 24px;
                                    margin-bottom: 20px;
                                }}
                                p {{
                                    color: #444;
                                    font-size: 16px;
                                    margin: 15px 0;
                                }}
                                strong {{
                                    color: #7E69AB;
                                    font-size: 24px;
                                    letter-spacing: 2px;
                                }}
                                .footer {{
                                    padding: 20px;
                                    text-align: center;
                                    font-size: 14px;
                                    color: #666;
                                    border-top: 1px solid #eee;
                                    background-color: #f7f7f7;
                                }}
                                .footer p {{
                                    margin: 8px 0;
                                    font-size: 14px;
                                    color: #666;
                                }}
                                .footer a {{
                                    color: #9b87f5;
                                    text-decoration: none;
                                }}
                                .footer a:hover {{
                                    text-decoration: underline;
                                }}
                                .social-icon {{
                                    width: 16px;
                                    height: 16px;
                                    vertical-align: middle;
                                    margin-right: 5px;
                                }}
                                .unsubscribe {{
                                    color: #999;
                                    font-size: 12px;
                                    margin-top: 20px;
                                }}
                                @media only screen and (max-width: 600px) {{
                                    .email-wrapper {{
                                        width: 100%;
                                        margin: 0;
                                        border-radius: 0;
                                    }}
                                    .container {{
                                        padding: 20px;
                                    }}
                                }}
                            </style>
                        </head>
                        <body>
                            <div class="email-wrapper">
                                <div class="email-header">
                                    <h1 class="logo">Hudddle :)</h1>
                                </div>
                                <div class="container">
                                    <h1>Join the {workroom.name} on Hudddle</h1>
                                    <p>Hi there,</p>
                                    <p>{creator_name} has invited you to join the workroom '{workroom.name}' on Hudddle.</p>
                                    <p>Click the link below to join:</p>
                                    <p><a href="{invite_url}">Join Workroom</a></p>
                                    <p>If you don't have a Hudddle account, you'll be able to create one and then join the workroom.</p>
                                    <p>See you there!</p>
                                </div>
                                <div class="footer">
                                    <p>You can unsubscribe from this service anytime you want.</p>
                                    <p>Follow us on <a href="https://x.com/hudddler">Twitter</a></p>
                                </div>
                            </div>
                        </body>
                        </html>
                        """

                        subject = f"Invitation to join {workroom.name}"
                        email_result = await send_email_task.apply_async(
                            kwargs={
                                'recipients': [friend_email],
                                'subject': subject,
                                'body': email_body
                            }).get()
                        logging.info(f"Email sent to {friend_email}")
                    except Exception as e:
                        logging.error(f"Error sending email to {friend_email}: {e}")
                        continue

                    # Find user
                    friend_user = (await session.execute(
                        select(User).filter_by(email=friend_email)
                    )).scalar_one_or_none()
                    
                    if friend_user:
                        existing_member = (await session.execute(
                            select(WorkroomMemberLink).where(
                                WorkroomMemberLink.workroom_id == workroom.id,
                                WorkroomMemberLink.user_id == friend_user.id
                            )
                        )).scalar_one_or_none()
                        
                        if not existing_member:
                            members_to_add.append(
                                WorkroomMemberLink(
                                    workroom_id=workroom.id,
                                    user_id=friend_user.id
                                )
                            )

                if members_to_add:
                    session.add_all(members_to_add)
                    await session.commit()
                    
                logging.info(f"Members added and committed for workroom {workroom_id}")

    except Exception as e:
        logging.error(f"Error in send_workroom_invite_email_task: {e}")
        raise
   
# @celery_app.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
# async def send_workroom_invite_email_task(self, workroom_id: str, creator_name: str, friend_emails: List[str]):
#     try:
#         logging.info(f"Task send_workroom_invite_email_task started for workroom {workroom_id}")
#         workroom_uuid = UUID(workroom_id)
#         async with async_session() as session:
#             await session.begin()
#             logging.info("DB session opened for invite task")
            
#             invite_url = Config.HUDDDLE_LINK

#             stmt = select(Workroom).where(Workroom.id == workroom_uuid)
#             result = await session.execute(stmt)
#             workroom = result.scalar_one_or_none()
#             if not workroom:
#                 logging.error(f"Workroom with ID {workroom_uuid} not found")
#                 return
#             logging.info(f"Workroom {workroom.name} found for invite task.")
            
#             members_to_add = []

#             for friend_email in friend_emails:
#                 try:
#                     logging.info(f"Attempting to send email to {friend_email}")
#                     email_body = f"""
#                     <!DOCTYPE html>
#                     <html lang="en">
#                     <head>
#                         <meta charset="UTF-8">
#                         <meta name="viewport" content="width=device-width, initial-scale=1.0">
#                         <title>You are Invited...</title>
#                         <style>
#                             body {{
#                                 font-family: Arial, sans-serif;
#                                 margin: 0;
#                                 padding: 0;
#                                 background-color: #f4f4f4;
#                                 color: #333;
#                                 line-height: 1.6;
#                             }}
#                             .email-wrapper {{
#                                 max-width: 600px;
#                                 margin: 20px auto;
#                                 background-color: #ffffff;
#                                 border-radius: 8px;
#                                 overflow: hidden;
#                                 box-shadow: 0 0 10px rgba(0, 0, 0, 0.1);
#                             }}
#                             .email-header {{
#                                 background-color: #9b87f5;
#                                 text-align: center;
#                                 padding: 30px;
#                             }}
#                             .logo {{
#                                 color: white;
#                                 font-size: 24px;
#                                 font-weight: bold;
#                                 letter-spacing: 1px;
#                             }}
#                             .container {{
#                                 padding: 30px 40px;
#                                 text-align: center;
#                                 background-color: #f7f7f7;
#                             }}
#                             h1 {{
#                                 color: #1A1F2C;
#                                 font-size: 24px;
#                                 margin-bottom: 20px;
#                             }}
#                             p {{
#                                 color: #444;
#                                 font-size: 16px;
#                                 margin: 15px 0;
#                             }}
#                             strong {{
#                                 color: #7E69AB;
#                                 font-size: 24px;
#                                 letter-spacing: 2px;
#                             }}
#                             .footer {{
#                                 padding: 20px;
#                                 text-align: center;
#                                 font-size: 14px;
#                                 color: #666;
#                                 border-top: 1px solid #eee;
#                                 background-color: #f7f7f7;
#                             }}
#                             .footer p {{
#                                 margin: 8px 0;
#                                 font-size: 14px;
#                                 color: #666;
#                             }}
#                             .footer a {{
#                                 color: #9b87f5;
#                                 text-decoration: none;
#                             }}
#                             .footer a:hover {{
#                                 text-decoration: underline;
#                             }}
#                             .social-icon {{
#                                 width: 16px;
#                                 height: 16px;
#                                 vertical-align: middle;
#                                 margin-right: 5px;
#                             }}
#                             .unsubscribe {{
#                                 color: #999;
#                                 font-size: 12px;
#                                 margin-top: 20px;
#                             }}
#                             @media only screen and (max-width: 600px) {{
#                                 .email-wrapper {{
#                                     width: 100%;
#                                     margin: 0;
#                                     border-radius: 0;
#                                 }}
#                                 .container {{
#                                     padding: 20px;
#                                 }}
#                             }}
#                         </style>
#                     </head>
#                     <body>
#                         <div class="email-wrapper">
#                             <div class="email-header">
#                                 <h1 class="logo">Hudddle :)</h1>
#                             </div>
#                             <div class="container">
#                                 <h1>Join the {workroom.name} on Hudddle</h1>
#                                 <p>Hi there,</p>
#                                 <p>{creator_name} has invited you to join the workroom '{workroom.name}' on Hudddle.</p>
#                                 <p>Click the link below to join:</p>
#                                 <p><a href="{invite_url}">Join Workroom</a></p>
#                                 <p>If you don't have a Hudddle account, you'll be able to create one and then join the workroom.</p>
#                                 <p>See you there!</p>
#                             </div>
#                             <div class="footer">
#                                 <p>You can unsubscribe from this service anytime you want.</p>
#                                 <p>Follow us on <a href="https://x.com/hudddler">Twitter</a></p>
#                             </div>
#                         </div>
#                     </body>
#                     </html>
#                     """

#                     subject = f"Invitation to join {workroom.name}"
#                     send_email_task.apply_async(recipients=[friend_email], subject=subject, body=email_body)
#                     logging.info(f"Email sent to {friend_email}")
#                 except Exception as e:
#                     logging.error(f"Error sending email to {friend_email}: {e}")

#                 stmt = select(User).filter_by(email=friend_email)
#                 result = await session.execute(stmt)
#                 friend_user = result.scalar_one_or_none()
                
#                 if friend_user:
#                     stmt = select(WorkroomMemberLink).where(
#                         WorkroomMemberLink.workroom_id == workroom.id,
#                         WorkroomMemberLink.user_id == friend_user.id
#                     )
#                     result = await session.execute(stmt)
#                     existing_member = result.scalar_one_or_none()
                    
#                     if not existing_member:
#                         members_to_add.append(
#                             WorkroomMemberLink(
#                                 workroom_id=workroom.id,
#                                 user_id=friend_user.id
#                             )
#                         )

#             if members_to_add:
#                 session.add_all(members_to_add)

#             await session.commit()
#             logging.info(f"Members added and committed for workroom {workroom_id}")

#     except Exception as e:
#         logging.error(f"Error in send_workroom_invite_email_task: {e}", exc_info=True)
#         raise self.retry(exc=e)

@celery_app.task()
def email_daily_performance_to_managers():
    try:
        async def process():
            async with async_session() as session:
                result = await session.execute(
                    select(Workroom, User)
                    .join(User, Workroom.created_by == User.id)
                )
                workrooms_with_creators = result.all()
                
                for workroom, creator in workrooms_with_creators:
                    email_html = generate_manager_email(
                        manager_name=creator.first_name or "Manager",
                        workroom_name=workroom.name,
                        date=datetime.now().strftime("%B %d, %Y")
                    )
                    subject = f"🚀 Your Team's Daily Performance: {workroom.name}"
                    message = create_message([creator.email], subject, email_html)
                    async_to_sync(mail.send_message)(message)
                    logging.info(f"Daily performance email sent to {creator.email}")

        async_to_sync(process)()

    except Exception as e:
        logging.error(f"Error sending manager emails: {str(e)}", exc_info=True)
        raise

def generate_manager_email(manager_name: str, workroom_name: str, date: str) -> str:
    """Generate HTML email for managers with performance summary"""
    hudddle_link = HUDDDLE_LINK
    
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Daily Team Performance</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f4f4f4;
            color: #333;
            line-height: 1.6;
        }}
        
        .email-wrapper {{
            max-width: 600px;
            margin: 20px auto;
            background-color: #ffffff;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 0 10px rgba(0, 0, 0, 0.1);
        }}
        
        .email-header {{
            background-color: #9b87f5;
            text-align: center;
            padding: 30px;
        }}
        
        .logo {{
            color: white;
            font-size: 24px;
            font-weight: bold;
            letter-spacing: 1px;
        }}
        
        .container {{
            padding: 30px 40px;
            text-align: center;
            background-color: #f7f7f7;
        }}
        
        h1 {{
            color: #1A1F2C;
            font-size: 24px;
            margin-bottom: 20px;
        }}
        
        p {{
            color: #444;
            font-size: 16px;
            margin: 15px 0;
        }}
        
        .cta-button {{
            display: inline-block;
            background-color: #9b87f5;
            color: white;
            padding: 12px 24px;
            margin: 20px 0;
            border-radius: 4px;
            text-decoration: none;
            font-weight: bold;
        }}
        
        .footer {{
            padding: 20px;
            text-align: center;
            font-size: 14px;
            color: #666;
            border-top: 1px solid #eee;
            background-color: #f7f7f7;
        }}
    </style>
</head>
<body>
    <div class="email-wrapper">
        <div class="email-header">
            <div class="logo">Hudddle</div>
        </div>
        
        <div class="container">
            <h1>Hi {manager_name},</h1>
            <p>Your team in <strong>{workroom_name}</strong> has been hard at work today!</p>
            
            <p>Here's what happened on {date}:</p>
            
            <ul style="text-align: left; margin: 20px 0; padding-left: 20px;">
                <li>✅ Tasks completed by your team</li>
                <li>📈 Performance metrics updated</li>
                <li>🏆 Leaderboard positions changed</li>
                <li>📊 New KPI insights available</li>
            </ul>
            
            <p>Don't miss out on seeing how your team performed today!</p>
            
            <a href="{hudddle_link}" class="cta-button">View Team Performance</a>
            
            <p style="margin-top: 30px; font-style: italic;">
                The Hudddle Team<br>
                Making remote work measurable
            </p>
        </div>
        
        <div class="footer">
            <p>You're receiving this email because you're a manager of {workroom_name}.</p>
            <p>Hudddle.io - The Future of Remote Team Management</p>
        </div>
    </div>
</body>
</html>
"""    

        