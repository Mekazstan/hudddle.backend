from uuid import UUID
import logging
from app_src.config import Config
from datetime import datetime, timezone
from sqlalchemy import select
from app_src.mail import create_message
from app_src.config import Config
from app_src.db.models import User, Workroom, WorkroomLiveSession, WorkroomPerformanceMetric
from app_src.workroom.service import (analyze_image, calculate_workroom_kpi_overview, 
    generate_user_session_summary, store_analysis_result, delete_s3_object, update_workroom_leaderboard
)

DOMAIN = Config.DOMAIN
HUDDDLE_LINK = Config.HUDDDLE_LINK

logger = logging.getLogger(__name__)

async def send_email_task(ctx, email_data: dict, _job_try=0):
    if 'mail' not in ctx:
        raise RuntimeError("ARQ ctx['mail'] not set — did startup() run?")
    mail = ctx['mail']
    logger.info(f"Starting email send to {email_data['recipients']}")
    try:
        message = create_message(
            recipients=email_data['recipients'],
            subject=email_data['subject'],
            body=email_data['body']
        )
        await mail.send_message(message)
        logger.info(f"Email successfully sent to {email_data['recipients']}")
        return {"status": "success", "recipients": email_data['recipients']}
    except Exception as e:
        logger.error(f"Failed to send email: {e}", exc_info=True)
        if _job_try > 3:
            logger.error("Job permanently failed after 3 tries")
            raise
        await ctx['redis'].enqueue_job('send_email_task', email_data, _defer_by=60, _job_try=_job_try + 1)
        raise

async def send_workroom_invites(ctx, workroom_name, creator_name, recipient_emails, _job_try=0):
    if 'mail' not in ctx:
        raise RuntimeError("ARQ ctx['mail'] not set — did startup() run?")
    mail = ctx['mail']
    logger.info(f"Sending invites for workroom: {workroom_name} to {len(recipient_emails)} recipients")
    subject = f"Invitation to join {workroom_name}"
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
                                <h1>Join the {workroom_name} on Hudddle</h1>
                                <p>Hi there,</p>
                                <p>{creator_name} has invited you to join the workroom '{workroom_name}' on Hudddle.</p>
                                <p>Click the link below to join:</p>
                                <p><a href="{HUDDDLE_LINK}">Join Workroom</a></p>
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

    failed_emails = []
    successful_emails = []
    
    for email in recipient_emails:
        try:
            message = create_message(recipients=[email], subject=subject, body=email_body)
            await mail.send_message(message)
            successful_emails.append(email)
            logger.info(f"✅ Email sent to {email}")
        except Exception as e:
            failed_emails.append(email)
            logger.error(f"❌ Failed to send to {email}: {e}")
        
        if failed_emails and _job_try < 3:
            logger.info(f"Retrying {len(failed_emails)} failed emails")
            await ctx['redis'].enqueue_job(
                'send_workroom_invites', 
                workroom_name, 
                creator_name, 
                failed_emails,
                _defer_by=60, 
                _job_try=_job_try + 1
            )
        elif failed_emails:
            logger.error(f"Permanently failed to send to: {failed_emails}")
        
        return {
            "status": "completed",
            "successful": successful_emails,
            "failed": failed_emails,
            "workroom": workroom_name
        }

async def process_image_and_store_task(ctx, user_id, session_id, image_url, image_filename, timestamp_str, _job_try=0):
    if 'session_maker' not in ctx:
        raise RuntimeError("ARQ ctx['session_maker'] not set — did startup() run?")
    session_maker = ctx['session_maker']
    async with session_maker() as session:
        try:
            await session.begin()
            timestamp = datetime.fromisoformat(timestamp_str)

            workroom_live_session = await session.get(WorkroomLiveSession, session_id)
            if not workroom_live_session:
                logger.warning("Live session not found")
                return

            workroom = await session.get(Workroom, workroom_live_session.workroom_id)
            if not workroom:
                logger.warning("Workroom not found")
                return

            performance_metrics = await session.execute(
                select(WorkroomPerformanceMetric)
                .where(WorkroomPerformanceMetric.workroom_id == workroom.id)
            )
            kpi_names = {metric.kpi_name for metric in performance_metrics.scalars()}

            analysis_result = await analyze_image(image_url, kpi_names)
            if not analysis_result:
                logger.warning("Image analysis failed")
                return

            await store_analysis_result(analysis_result, image_filename)
            await session.commit()

            await delete_s3_object(image_filename)
            return analysis_result
        except Exception as e:
            if session.in_transaction():
                await session.rollback()
            logger.error(f"Image processing failed: {e}", exc_info=True)
            if _job_try > 3:
                logger.error("Job permanently failed after 3 tries")
                return
            await ctx['redis'].enqueue_job('process_image_and_store_task', user_id, session_id, image_url, image_filename, timestamp_str, _defer_by=60, _job_try=_job_try + 1)
            raise

async def process_workroom_end_session(ctx, workroom_id, session_id, user_id, _job_try=0):
    if 'session_maker' not in ctx:
        raise RuntimeError("ARQ ctx['session_maker'] not set — did startup() run?")
    session_maker = ctx['session_maker']
    async with session_maker() as session:
        try:
            await session.begin()
            workroom_uuid = UUID(workroom_id)
            session_uuid = UUID(session_id)
            user_uuid = UUID(user_id)

            logger.info(f"📦 Starting closeout for session {session_id}")

            # Must run in strict order: summary -> leaderboard -> KPI overview
            await generate_user_session_summary(workroom_uuid, session_uuid, user_uuid, session)
            await update_workroom_leaderboard(workroom_uuid, session)
            await calculate_workroom_kpi_overview(workroom_uuid, session)

            live_session = await session.get(WorkroomLiveSession, session_uuid)
            if not live_session:
                logger.error("Session not found")
                return False

            live_session.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
            live_session.is_active = False

            await session.commit()
            logger.info("✅ Session closed")
            return True
        except Exception as e:
            if session.in_transaction():
                await session.rollback()
            logger.error(f"❌ Error closing session: {e}", exc_info=True)
            if _job_try > 3:
                logger.error("Job permanently failed after 3 tries")
                return
            await ctx['redis'].enqueue_job('process_workroom_end_session', workroom_id, session_id, user_id, _defer_by=60, _job_try=_job_try + 1)
            raise

async def email_daily_performance_to_managers(ctx, _job_try=0):
    if 'mail' not in ctx:
        raise RuntimeError("ARQ ctx['mail'] not set — did startup() run?")
    mail = ctx['mail']
    try:
        session_maker = ctx['session_maker']
        async with session_maker() as session:
            result = await session.execute(
                select(Workroom, User)
                .join(User, Workroom.created_by == User.id)
            )
            for workroom, creator in result.all():
                email_html = generate_manager_email(
                    manager_name=creator.first_name or "Manager",
                    workroom_name=workroom.name,
                    date=datetime.now().strftime("%B %d, %Y")
                )
                message = create_message([creator.email], f"🚀 Daily Performance: {workroom.name}", email_html)
                await mail.send_message(message)
                logger.info(f"Email sent to {creator.email}")
    except Exception as e:
        logger.error(f"Error sending manager emails: {e}", exc_info=True)
        if _job_try > 3:
            logger.error("Job permanently failed after 3 tries")
            return
        await ctx['redis'].enqueue_job('email_daily_performance_to_managers', _defer_by=60, _job_try=_job_try + 1)
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

def get_password_reset_template(otp: str) -> str:
    return f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Password Reset OTP</title>
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
                    <div class="logo">Hudddle.</div>
                </div>
                
                <div class="container">
                    <h1>Password Reset OTP</h1>
                    <p>Your OTP code is: <strong>{otp}</strong></p>
                    <p>This code expires in 15 minutes.</p>
                    
                    <p style="margin-top: 18px; font-style: italic;">
                        <br>
                        Let's make work fun together. The Team at Hudddle.io
                    </p>
                </div>
                
                <div class="footer">
                    <p>You can unsubscribe from this service anytime you want.</p>
                    <p>Follow us on <a href="https://x.com/hudddler">Twitter</a></p>
                </div>
            </div>
        </body>
        </html>
    """

