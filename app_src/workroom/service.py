from collections import defaultdict
import json
import logging
import re
from deepgram import (
    DeepgramClient,
    PrerecordedOptions
)
from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID
from app_src.db.models import (TaskStatus, UserKPIMetricHistory, UserKPISummary, Workroom, Leaderboard, 
                       Task, WorkroomKPIMetricHistory, WorkroomKPISummary, 
                       WorkroomLiveSession, WorkroomOverallKPI, User)
import boto3
from botocore.config import Config as BotoConfig
from app_src.config import Config
from botocore.exceptions import ClientError
from typing import List, Optional
from groq import Groq
from datetime import datetime, timezone, timedelta
from .schema import ImageAnalysisResult, UserDailyKPIReport


GROQ_API_KEY = Config.GROQ_API_KEY
if not GROQ_API_KEY:
    logging.error("GROQ_API_KEY is not set in the environment variables.")
groq_client = Groq(api_key=GROQ_API_KEY)

# AWS S3 Configuration
S3_PRESIGNED_URL_EXPIRY_SECONDS = 3600
AWS_ACCESS_KEY_ID = Config.AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY = Config.AWS_SECRET_ACCESS_KEY
AWS_STORAGE_BUCKET_NAME = Config.AWS_STORAGE_BUCKET_NAME
AWS_REGION = Config.AWS_REGION

s3_client_signed = boto3.client(
    's3',
    region_name=AWS_REGION,
    config=BotoConfig(signature_version='s3v4')
)

s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION,
)

def generate_presigned_url(bucket_name, object_key, expiry_seconds=S3_PRESIGNED_URL_EXPIRY_SECONDS):
    """Generate a presigned URL to access an S3 object."""
    try:
        response = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': object_key},
            ExpiresIn=expiry_seconds
        )
    except Exception as e:
        logging.error(f"Error generating presigned URL: {e}")
        return None
    return response

async def get_all_analysis_results(user_id: UUID, session_id: UUID) -> List[str]:
    """
    Retrieves all plain text analysis result files stored in S3 for a given user session.

    Returns:
        A list of strings, each representing the text content of one analysis result file.
    """
    prefix = f"user_{user_id}/session_{session_id}/"
    analysis_data = []

    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=AWS_STORAGE_BUCKET_NAME, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.startswith(prefix + "analysis_") and key.endswith(".txt"):
                    try:
                        response = s3_client.get_object(Bucket=AWS_STORAGE_BUCKET_NAME, Key=key)
                        content = response['Body'].read().decode('utf-8').strip()
                        if content:
                            analysis_data.append(content)
                        else:
                            logging.warning(f"File {key} is empty. Skipping.")
                    except Exception as read_error:
                        logging.error(f"Failed to read file {key}: {read_error}")
        return analysis_data

    except ClientError as e:
        logging.error(f"Error listing analysis results from S3: {e}")
        return []

async def update_workroom_leaderboard(workroom_id: UUID, session: AsyncSession):
    """
    Recalculates and updates the leaderboard for a given workroom.
    NOTE: This function does NOT commit - that's handled by the caller
    """
    DEFAULT_SCORE = 0.0
    DEFAULT_USERNAME = "Unnamed User"
    MIN_ACTIVE_MINUTES = 30
    LIVE_SESSION_POINTS = 2
    STREAK_MULTIPLIER = 0.5
    MISSED_TASK_PENALTY = 2 
    
    # Fetch the workroom and its members
    try:
        result = await session.execute(
            select(Workroom)
            .options(selectinload(Workroom.members).selectinload(User.streak))
            .where(Workroom.id == workroom_id)
        )
        workroom = result.scalar_one_or_none()
        if not workroom:
            raise ValueError(f"Workroom {workroom_id} not found")
            return
        
        if not workroom.members:
            logging.info(f"Workroom {workroom_id} has no members")
            return 

    except Exception as e:
        logging.error(f"Error fetching workroom {workroom_id}: {str(e)}")
        return

    leaderboard_data = []

    for member in workroom.members:
        try:
            # Safely get member details with defaults
            user_id = member.id if member else None
            if not user_id:
                continue

            username = member.first_name or DEFAULT_USERNAME
            
            # 1. KPI Alignment Score with defaults
            kpi_score = DEFAULT_SCORE
            try:
                kpi_result = await session.execute(
                    select(UserKPISummary)
                    .where(
                        UserKPISummary.user_id == user_id,
                        UserKPISummary.workroom_id == workroom_id
                    )
                    .order_by(UserKPISummary.date.desc())
                    .limit(1)
                )
                user_kpi_summary = kpi_result.scalar_one_or_none()
                kpi_score = float(user_kpi_summary.overall_alignment_percentage) if user_kpi_summary else DEFAULT_SCORE
            except Exception as e:
                logging.warning(f"Error getting KPI score for user {user_id}: {str(e)}")
                kpi_score = DEFAULT_SCORE

            # 2. Task Score
            task_score = DEFAULT_SCORE
            completed_task_points = DEFAULT_SCORE
            total_assigned_tasks = 0
            
            try:
                # Completed tasks
                completed_tasks_result = await session.execute(
                    select(Task).where(
                        Task.workroom_id == workroom_id,
                        Task.assigned_users.contains(member),
                        Task.status == TaskStatus.COMPLETED,
                        Task.kpi_link.isnot(None)
                    )
                )
                completed_tasks = completed_tasks_result.scalars().all()
                completed_task_points = sum(
                    float(task.task_point) if task.task_point else DEFAULT_SCORE 
                    for task in completed_tasks
                )

                # Total assigned tasks
                total_tasks_result = await session.execute(
                    select(func.count(Task.id)).where(
                        Task.workroom_id == workroom_id,
                        Task.assigned_users.contains(member))
                    )
                total_assigned_tasks = int(total_tasks_result.scalar() or 0)

                task_score = (
                    completed_task_points / total_assigned_tasks 
                    if total_assigned_tasks > 0 
                    else DEFAULT_SCORE
                )
            except Exception as e:
                logging.warning(f"Error calculating task score for user {user_id}: {str(e)}")
                task_score = DEFAULT_SCORE

            # 3. Engagement Score
            engagement_score = DEFAULT_SCORE
            try:
                # Live sessions
                live_sessions_result = await session.execute(
                    select(func.count(WorkroomLiveSession.id)).where(
                        WorkroomLiveSession.workroom_id == workroom_id,
                        WorkroomLiveSession.screen_sharer_id == user_id)
                    )
                live_session_count = int(live_sessions_result.scalar() or 0)
                live_session_points = live_session_count * LIVE_SESSION_POINTS

                # Active minutes (safe handling of None)
                active_minutes = float(member.daily_active_minutes) if member.daily_active_minutes else 0.0
                active_minutes_score = active_minutes / MIN_ACTIVE_MINUTES

                # Streak bonus (safe handling of missing streak)
                streak_bonus = (
                    float(member.streak.current_streak) * STREAK_MULTIPLIER 
                    if member.streak and member.streak.current_streak 
                    else DEFAULT_SCORE
                )

                engagement_score = live_session_points + active_minutes_score + streak_bonus
            except Exception as e:
                logging.warning(f"Error calculating engagement score for user {user_id}: {str(e)}")
                engagement_score = DEFAULT_SCORE

            # 4. Penalty Score
            penalty_score = DEFAULT_SCORE
            try:
                missed_tasks_result = await session.execute(
                    select(Task).where(
                        Task.workroom_id == workroom_id,
                        Task.assigned_users.contains(member),
                        Task.status != TaskStatus.COMPLETED,
                        Task.due_by < datetime.utcnow())
                    )
                missed_tasks = missed_tasks_result.scalars().all()
                penalty_score = len(missed_tasks) * MISSED_TASK_PENALTY
            except Exception as e:
                logging.warning(f"Error calculating penalty score for user {user_id}: {str(e)}")
                penalty_score = DEFAULT_SCORE

            # Final Score Calculation with bounds checking
            total_score = max(
                DEFAULT_SCORE,
                kpi_score + task_score + engagement_score - penalty_score
            )

            leaderboard_data.append({
                "user_id": user_id,
                "username": username,
                "score": total_score,
                "kpi_score": kpi_score,
                "task_score": task_score,
                "engagement_score": engagement_score,
            })

        except Exception as e:
            logging.error(f"Unexpected error processing user {getattr(member, 'id', 'unknown')}: {str(e)}")
            leaderboard_data.append({
                "user_id": getattr(member, 'id', None),
                "username": getattr(member, 'first_name', DEFAULT_USERNAME),
                "score": DEFAULT_SCORE,
                "kpi_score": DEFAULT_SCORE,
                "task_score": DEFAULT_SCORE,
                "engagement_score": DEFAULT_SCORE,
            })

    # Sort leaderboard by score descending, then by username
    leaderboard_data.sort(key=lambda x: (-x["score"], x["username"].lower()))

    # Save leaderboard - Use merge or upsert pattern
    for rank, entry in enumerate(leaderboard_data, start=1):
        try:
            if not entry["user_id"]:
                continue
            existing_result = await session.execute(
                select(Leaderboard).where(
                    Leaderboard.workroom_id == workroom_id,
                    Leaderboard.user_id == entry["user_id"]
                )
            )
            leaderboard_entry = existing_result.scalar_one_or_none()
            
            update_values = {
                "score": entry["score"],
                "rank": rank,
                "kpi_score": entry["kpi_score"],
                "task_score": entry["task_score"],
                "engagement_score": entry["engagement_score"],
                "updated_at": datetime.now(timezone.utc).replace(tzinfo=None)
            }

            if leaderboard_entry:
                # Update existing entry
                for key, value in update_values.items():
                    setattr(leaderboard_entry, key, value)
            else:
                # Create new entry
                new_leaderboard_entry = Leaderboard(
                    workroom_id=workroom_id,
                    user_id=entry["user_id"],
                    **update_values
                )
                session.add(new_leaderboard_entry)

        except Exception as e:
            logging.error(f"Error updating leaderboard for user {entry.get('user_id', 'unknown')}: {str(e)}")
            continue

    logging.info(f"Updated leaderboard for workroom {workroom_id} with {len(leaderboard_data)} entries")


# --------------------------------------------------------------------------------
#  Image Analysis Functions
# --------------------------------------------------------------------------------

def validate_image_url(image_url: str) -> bool:
    """
    Validates if the image URL is accessible and in a supported format.
    """
    try:
        import urllib.request
        from urllib.parse import urlparse
        
        # Check if URL is properly formatted
        parsed = urlparse(image_url)
        if not parsed.scheme or not parsed.netloc:
            logging.warning(f"Invalid URL format: {image_url}")
            return False
            
        # Check if URL is accessible (basic check)
        req = urllib.request.Request(image_url, method='HEAD')
        with urllib.request.urlopen(req, timeout=10) as response:
            content_type = response.getheader('content-type', '').lower()
            
            # Check if content type indicates an image
            supported_types = ['image/jpeg', 'image/jpg', 'image/png', 'image/webp', 'image/gif']
            if not any(img_type in content_type for img_type in supported_types):
                logging.warning(f"Unsupported image type: {content_type} for URL: {image_url}")
                return False
                
        return True
        
    except Exception as e:
        logging.warning(f"Image URL validation failed: {e}")
        return False

async def analyze_image(image_url: str, kpi_names: set) -> str:
    """
    Analyzes the image using Groq API and returns a plain text description.
    Focuses on identifying activities related to specific performance metrics.
    """
    try:
        if not kpi_names:
            kpi_names = {"productivity", "focus", "collaboration"}
            
        kpi_list = ", ".join(kpi_names)
        
        # Validate image URL before sending to Groq
        if not validate_image_url(image_url):
            logging.error(f"Invalid or inaccessible image URL: {image_url}")
            return "[Image analysis skipped: Invalid or inaccessible image URL]"
        
        # Create message content with image
        message_content = [
            {
                "type": "text", 
                "text": (
                    "Analyze this screenshot of a user's work session. Focus on identifying activities that relate "
                    f"to these specific performance metrics: {kpi_list}. "
                    "Describe what applications/tools are visible and how they're being used. "
                    "Note any signs of productive work, collaboration, or distractions. "
                    "For example, if you see coding tools, document editors, communication apps, "
                    "or entertainment sites, mention how they relate to the KPIs. "
                    "Keep your response concise (50-100 words) and directly relevant to work performance. "
                    "Format as plain text with no special formatting or bullet points."
                )
            },
            {
                "type": "image_url", 
                "image_url": {"url": image_url}
            }
        ]

        # Use Groq client directly
        completion = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {
                    "role": "system",
                    "content": "You are analyzing a work session screenshot for performance metrics to detect the activity of the user and tools used."
                },
                {
                    "role": "user",
                    "content": message_content
                }
            ],
            temperature=0.4,
            max_completion_tokens=250,
            top_p=1,
            stream=False,
            stop=None
        )

        return completion.choices[0].message.content.strip() if completion.choices[0].message.content else "No analysis returned."

    except Exception as e:
        # Enhanced error logging with more context
        error_msg = str(e)
        if "invalid image data" in error_msg.lower():
            logging.error(f"Groq API rejected image data from URL: {image_url}. Error: {e}")
            return "[Image analysis failed: Invalid image format or corrupted image data]"
        elif "400" in error_msg:
            logging.error(f"Bad request to Groq API with image URL: {image_url}. Error: {e}")
            return "[Image analysis failed: Bad request to vision API]"
        else:
            logging.error(f"Unexpected error analyzing image {image_url}: {e}")
            return f"[Image analysis failed: {str(e)}]"

async def process_image_and_store_task(
    user_id: UUID,
    session_id: UUID,
    image_url: str,
    image_filename: str,
    timestamp_str: str,
    session: AsyncSession,
):
    """
    Analyzes a screenshot and stores the structured analysis result in S3.
    """
    timestamp = datetime.fromisoformat(timestamp_str)

    # Retrieve the live session with workroom relationship loaded
    workroom_live_session = await session.execute(
        select(WorkroomLiveSession)
        .options(selectinload(WorkroomLiveSession.workroom)
        .selectinload(Workroom.performance_metrics))
        .where(WorkroomLiveSession.id == session_id)
    )
    workroom_live_session = workroom_live_session.scalar_one_or_none()
    
    if not workroom_live_session:
        logging.warning(f"Live session not found: {session_id}")
        return

    workroom = workroom_live_session.workroom
    if not workroom:
        logging.warning(f"Workroom not found for session: {session_id}")
        return

    # Get all KPI names from performance metrics
    kpi_names = [metric.kpi_name for metric in workroom.performance_metrics]
    
    # Format KPIs for the prompt
    workroom_kpis = ", ".join(kpi_names) if kpi_names else "productivity"

    # Perform image analysis
    analysis_result = await analyze_image(image_url, workroom_kpis)
    if not analysis_result:
        logging.warning(f"Analysis result is None, skipping storage")
        return

    # Store the analysis result to S3
    await store_analysis_result(analysis_result, image_filename)

async def store_analysis_result(analysis_text: str, original_image_key: str) -> bool:
    """
    Stores the plain text analysis result in AWS S3.
    """
    base_name = original_image_key.split('/')[-1]
    file_name_without_ext = base_name.rsplit('.', 1)[0]
    path_parts = original_image_key.split('/')[:-1]
    text_key = '/'.join(path_parts) + f'/analysis_{file_name_without_ext}.txt'

    try:
        s3_client.put_object(
            Bucket=AWS_STORAGE_BUCKET_NAME,
            Key=text_key,
            Body=analysis_text.encode("utf-8"),
            ContentType="text/plain"
        )
        return True
    except ClientError as e:
        logging.error(f"Error storing analysis result: {e}")
        return False

async def generate_user_session_summary(workroom_id: UUID, session_id: UUID, user_id: UUID, db: AsyncSession):
    # Get session and user details
    session_obj = await db.get(WorkroomLiveSession, session_id)
    if not session_obj:
        raise HTTPException(status_code=404, detail="Session not found")

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Eagerly load workroom with performance metrics
    result = await db.execute(
        select(Workroom)
        .options(
            selectinload(Workroom.performance_metrics),
            selectinload(Workroom.created_by_user)
        )
        .where(Workroom.id == workroom_id)
    )
    workroom = result.scalar_one_or_none()
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")

    # Get user activity results
    all_activities = await get_all_analysis_results(user_id, session_id)
    if not all_activities:
        raise HTTPException(status_code=404, detail="No analysis results found")

    # Get recently completed tasks (last 6 hours) assigned to this user
    six_hours_ago = datetime.utcnow() - timedelta(hours=6)
    recent_tasks_result = await db.execute(
        select(Task).where(
            Task.workroom_id == workroom_id,
            Task.assigned_users.contains(user),
            Task.status == TaskStatus.COMPLETED,
            Task.completed_at >= six_hours_ago,
            Task.completed_at.isnot(None)
        ).order_by(Task.completed_at.desc())
    )
    recent_completed_tasks = recent_tasks_result.scalars().all()
    
    # Prepare task information for the prompt
    recent_tasks_info = []
    for task in recent_completed_tasks:
        task_info = {
            "title": task.title,
            "kpi_link": task.kpi_link,
            "task_tools": task.task_tools or [],
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "task_points": task.task_point
        }
        recent_tasks_info.append(task_info)

    # Prepare KPI metrics with weights
    kpi_metrics = [
        {
            "name": pm.kpi_name,
            "weight": pm.weight,
            "description": f"Importance: {pm.weight}/10"
        } 
        for pm in workroom.performance_metrics
    ]
    
    # Create fallback response
    fallback_response = UserDailyKPIReport(
        summary_text="No analysis found",
        kpi_breakdown=[
            {"kpi_name": pm.kpi_name, "percentage": 0.0}
            for pm in workroom.performance_metrics
        ]
    )
    
    try:
        # Create the prompt content
        kpi_metrics_json = json.dumps(kpi_metrics, indent=2)
        all_activities_json = json.dumps(all_activities, indent=2)
        recent_tasks_json = json.dumps(recent_tasks_info, indent=2)
        
        user_content = f"""
        Team Member: {user.first_name} {user.last_name}
        Workroom: {workroom.name}
        Session Date: {session_obj.start_time.date() if session_obj.start_time else 'Today'}

        Below are the performance metrics for this workroom with their importance weights:
        {kpi_metrics_json}

        Here are the detected activities from {user.first_name}'s session:
        {all_activities_json}

        Here are the tasks {user.first_name} completed in the last 6 hours within this workroom:
        {recent_tasks_json}

        Your analysis task:
        1. Evaluate {user.first_name}'s performance by analyzing:
        - How well their detected activities align with the specified KPIs
        - Whether they used the tools specified in their completed tasks (task_tools)
        - The correlation between their activities and the tasks they completed
        - Quality of work based on KPI alignment and task completion patterns
        
        2. Write a structured summary with the following format for frontend display:
        - Start with "**Insights**" as a header
        - Use bullet points (•) for key insights
        - Include a "**Recommendations**" section with actionable suggestions
        - Keep insights concise and specific to observed activities and KPI alignment
        - Format for easy reading with proper spacing and structure
        
        3. For each KPI, provide an alignment percentage (0-100) considering:
        - Time spent on KPI-related activities
        - Usage of tools specified in completed tasks
        - Quality of engagement with task-related work
        - Weight/importance of each KPI
        - Evidence from both activities and completed task patterns

        Return a JSON object with this exact structure:
        {{
            "summary_text": "**Insights**\n\n• [Specific insight about KPI alignment and activity patterns]\n• [Evidence of tool usage and task completion effectiveness]\n• [Quality assessment based on observed behaviors]\n• [Performance consistency or notable patterns]\n\n**Recommendations**\n\n• [Specific actionable recommendation based on analysis]\n• [Suggestion for improving KPI alignment or workflow]\n• [Tool usage or collaboration improvements if applicable]",
            "kpi_breakdown": [
                {{
                    "kpi_name": "KPI Name",
                    "percentage": 85.0
                }},
                ...
            ]
        }}

        Important:
        - Format summary_text for direct frontend rendering with proper markdown
        - Use bullet points (•) not dashes (-)
        - Include clear section headers with **bold** formatting
        - Keep insights data-driven and specific to observed activities
        - Base analysis on both screen activities AND completed tasks with their tools
        - Reward alignment between task tools and detected activities
        - Consider task completion timing and KPI relevance
        - Only return valid JSON with properly escaped formatting
        - Include all KPIs in the breakdown
        - Percentages should be floats
        """
        
        # Use Groq client directly
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": "You are an AI performance analyst evaluating a team member's work session. Respond like you are accesssing and advising the team member"
                },
                {
                    "role": "user",
                    "content": user_content
                }
            ],
            temperature=0.3,
            max_completion_tokens=800,
            top_p=1,
            stream=False,
            stop=None
        )
        
        # Parse JSON response
        response_text = completion.choices[0].message.content.strip()
        
        # Try to extract JSON from the response
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
            try:
                parsed_data = json.loads(json_str)
                
                # Create UserDailyKPIReport from parsed data
                if "summary_text" in parsed_data and "kpi_breakdown" in parsed_data:
                    kpi_breakdown = [
                        {"kpi_name": item["kpi_name"], "percentage": float(item["percentage"])}
                        for item in parsed_data["kpi_breakdown"]
                    ]
                    summary_data = UserDailyKPIReport(
                        summary_text=parsed_data["summary_text"],
                        kpi_breakdown=kpi_breakdown
                    )
                else:
                    raise ValueError("Missing required fields in LLM response")
            except (json.JSONDecodeError, ValueError, KeyError) as parse_error:
                logging.warning(f"Failed to parse LLM JSON response: {parse_error}. Raw response: {response_text}")
                summary_data = fallback_response
        else:
            logging.warning(f"No JSON found in LLM response: {response_text}")
            summary_data = fallback_response
            
    except Exception as e:
        logging.warning(f"LLM call failed: {str(e)}")
        summary_data = fallback_response
            
    # Calculate overall alignment percentage (weighted average)
    total_weight = sum(pm.weight for pm in workroom.performance_metrics)
    weighted_sum = 0.0
    
    for kpi in summary_data.kpi_breakdown:
        # Find matching performance metric to get weight
        pm = next((pm for pm in workroom.performance_metrics 
                if pm.kpi_name == kpi.kpi_name), None)
        if pm:
            weighted_sum += (kpi.percentage * pm.weight)
    
    overall_alignment = weighted_sum / total_weight if total_weight > 0 else 0

    # Upsert UserKPISummary
    existing_summary = await db.execute(
        select(UserKPISummary)
        .where(
            UserKPISummary.user_id == user_id,
            UserKPISummary.workroom_id == workroom.id,
            UserKPISummary.session_id == session_id
        )
    )
    existing_summary = existing_summary.scalar_one_or_none()

    if existing_summary:
        existing_summary.overall_alignment_percentage = overall_alignment
        existing_summary.kpi_breakdown = {k.kpi_name: k.percentage for k in summary_data.kpi_breakdown}
        existing_summary.summary_text = summary_data.summary_text
    else:
        new_summary = UserKPISummary(
            user_id=user_id,
            session_id=session_id,
            workroom_id=workroom.id,
            overall_alignment_percentage=overall_alignment,
            kpi_breakdown={k.kpi_name: k.percentage for k in summary_data.kpi_breakdown},
            summary_text=summary_data.summary_text,
            date=session_obj.ended_at.date() if session_obj.ended_at else datetime.utcnow().date()
        )
        db.add(new_summary)

    # Save individual KPI metrics to history
    today = datetime.utcnow().date()

    # Save overall alignment to history
    overall_kpi_name = f"{today} - Overall Alignment"
    existing_overall_history = await db.execute(
        select(UserKPIMetricHistory).where(
            UserKPIMetricHistory.user_id == user_id,
            UserKPIMetricHistory.workroom_id == workroom.id,
            UserKPIMetricHistory.kpi_name == overall_kpi_name,
            UserKPIMetricHistory.date == today
        )
    )
    existing_overall_history = existing_overall_history.scalar_one_or_none()

    if existing_overall_history:
        existing_overall_history.alignment_percentage = overall_alignment
    else:
        db.add(UserKPIMetricHistory(
            user_id=user_id,
            workroom_id=workroom.id,
            kpi_name=overall_kpi_name,
            alignment_percentage=overall_alignment,
            date=today
        ))

    await db.commit()
    return summary_data

#   --------------------------------------------------------------------------------
#   KPI Functions
#   --------------------------------------------------------------------------------

async def calculate_workroom_kpi_overview(workroom_id: UUID, user_id: UUID, session: AsyncSession):
    # Get workroom and today's date
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")

    today = datetime.utcnow().date()

    # Check if summary already exists for today
    existing_summary = await session.execute(
        select(WorkroomKPISummary).where(
            WorkroomKPISummary.workroom_id == workroom_id,
            WorkroomKPISummary.date == today
        )
    )
    existing_summary = existing_summary.scalar_one_or_none()

    # Get current user's summary for today (with ordering to handle duplicates)
    user_summary_result = await session.execute(
        select(UserKPISummary).where(
            UserKPISummary.workroom_id == workroom_id,
            UserKPISummary.user_id == user_id,
            UserKPISummary.date == today
        ).order_by(UserKPISummary.id.desc()).limit(1)
    )
    user_summary = user_summary_result.scalar_one_or_none()

    if not user_summary:
        raise HTTPException(status_code=404, detail="No KPI summary found for current user today")
    
    # Get all user summaries for today (only for alignment calculation)
    summaries_result = await session.execute(
        select(UserKPISummary).where(
            UserKPISummary.workroom_id == workroom_id,
            UserKPISummary.date == today
        )
    )
    summaries = summaries_result.scalars().all()

    if not summaries:
        raise HTTPException(status_code=404, detail="No KPI summaries found for today")

    # Calculate averages
    total_alignment = sum(s.overall_alignment_percentage for s in summaries)
    average_alignment = total_alignment / len(summaries)

    # Calculate average KPI breakdown
    combined_kpi_breakdown = defaultdict(list)
    for summary in summaries:
        if summary.kpi_breakdown:
            for kpi_name, percentage in summary.kpi_breakdown.items():
                combined_kpi_breakdown[kpi_name].append(percentage)

    averaged_kpi_breakdown = {
        kpi_name: sum(values) / len(values)
        for kpi_name, values in combined_kpi_breakdown.items()
    }

    # Prepare fallback summary text
    fallback_summary = (
        "Team performance analysis unavailable. "
        f"Average alignment: {round(average_alignment, 2)}%. "
        "Please check individual member reports for details."
    )

    try:
        # Prepare texts for LLM
        texts_for_llm = [user_summary.summary_text]
        if existing_summary and existing_summary.summary_text:
            texts_for_llm.append(existing_summary.summary_text)
        
        # Create the prompt content
        kpi_breakdown_json = json.dumps(averaged_kpi_breakdown, indent=2)
        
        user_content = f"""
        Below are the relevant summaries from today:

        Current User Summary:
        {texts_for_llm[0]}

        {"Existing Team Summary:" + texts_for_llm[1] if len(texts_for_llm) > 1 else ""}

        Key Metrics:
        - Overall Alignment: {round(average_alignment, 2)}%
        - KPI Breakdown: {kpi_breakdown_json}

        Generate a structured executive summary formatted for frontend display with the following structure:

        **Team Performance Summary**

        • [Overall team performance insight with specific alignment percentage]
        • [Key productivity patterns observed across team members]
        • [Collaboration effectiveness and tool usage patterns]

        **Key Strengths**

        • [Specific strength with supporting data]
        • [Notable individual contributions - mention names for exceptional performance]
        • [Effective processes or high-performing areas]

        **Areas for Improvement**

        • [Specific improvement area with context]
        • [Performance gaps or misalignment issues]
        • [Resource or support needs identified]

        **Recommendations for Tomorrow**

        • [Actionable recommendation for team productivity]
        • [Specific suggestions for underperforming areas]
        • [Strategic focus areas based on today's insights]

        Format requirements:
        - Use bullet points (•) for all list items
        - Include **bold** section headers
        - Write in professional but approachable tone for managers
        - Focus on team-level insights with specific data points
        - Mention individual names only for particularly notable performance (good or concerning)
        - Keep each bullet point concise but informative
        - Ensure proper markdown formatting for frontend rendering
        """
        
        # Use Groq client directly
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": "You are analyzing daily performance summaries for a workroom team. Respond like you are writing a daily report to the Team Lead or Manager who wants to know how well the team performed today and how align they are to the team's KPI metrics"
                },
                {
                    "role": "user",
                    "content": user_content
                }
            ],
            temperature=0.5,
            max_completion_tokens=500,
            top_p=1,
            stream=False,
            stop=None
        )
        
        generated_summary = completion.choices[0].message.content.strip()
        
    except Exception as e:
        logging.error(f"LLM summary generation failed: {e}")
        generated_summary = fallback_summary

    # Update or create WorkroomKPISummary
    if existing_summary:
        existing_summary.overall_alignment_percentage = average_alignment
        existing_summary.kpi_breakdown = averaged_kpi_breakdown
        existing_summary.summary_text = generated_summary
    else:
        session.add(WorkroomKPISummary(
            workroom_id=workroom_id,
            date=today,
            overall_alignment_percentage=average_alignment,
            kpi_breakdown=averaged_kpi_breakdown,
            summary_text=generated_summary
        ))

    # Update WorkroomKPIMetricHistory
    # First check for existing entries
    existing_metrics = await session.execute(
        select(WorkroomKPIMetricHistory).where(
            WorkroomKPIMetricHistory.workroom_id == workroom_id,
            WorkroomKPIMetricHistory.date == today
        )
    )
    existing_metrics = existing_metrics.scalars().all()

    # Add overall alignment metric
    overall_metric_name = f"{today} - Overall Alignment"
    overall_metric = next(
        (m for m in existing_metrics if m.kpi_name == overall_metric_name),
        None
    )
    if overall_metric:
        overall_metric.metric_value = round(average_alignment, 2)
    else:
        session.add(WorkroomKPIMetricHistory(
            workroom_id=workroom_id,
            date=today,
            kpi_name=overall_metric_name,
            metric_value=round(average_alignment, 2)
        ))

    # Update WorkroomOverallKPI
    existing_kpi = await session.execute(
        select(WorkroomOverallKPI).where(
            WorkroomOverallKPI.workroom_id == workroom_id
        )
    )
    existing_kpi = existing_kpi.scalar_one_or_none()

    if existing_kpi:
        existing_kpi.overall_alignment_score = average_alignment
    else:
        session.add(WorkroomOverallKPI(
            workroom_id=workroom_id,
            overall_alignment_score=average_alignment
        ))

    await session.commit()

    return {
        "date": today.isoformat(),
        "overall_alignment": round(average_alignment, 2),
        "kpi_breakdown": {k: round(v, 2) for k, v in averaged_kpi_breakdown.items()},
        "summary_text": generated_summary
    }

# --------------------------------------------------------------------------------
#  Audio Procesing Functions
# --------------------------------------------------------------------------------
async def process_audio(audio_url: str) -> str:
    """
    Processes the audio file from a URL (e.g., S3) using Deepgram.
    """
    try:
        DG_API_KEY = Config.DG_API_KEY
        deepgram: DeepgramClient = DeepgramClient(api_key=DG_API_KEY)

        AUDIO_URL = {"url": audio_url}

        options = PrerecordedOptions(
            model="nova-2",
            smart_format=True,
        )

        response = deepgram.listen.rest.v("1").transcribe_url(AUDIO_URL, options)

        transcript = response['results']['channels'][0]['alternatives'][0]['transcript']

        logging.info(f"Audio processed from URL: {audio_url}")
        return transcript

    except Exception as e:
        logging.error(f"Error processing audio with Deepgram from URL: {e}")
        raise e

async def store_audio_analysis_report(report_text: str, s3_key: str) -> bool:
    """
    Stores the audio analysis report in AWS S3.
    """
    try:
        s3_client.put_object(
            Bucket=AWS_STORAGE_BUCKET_NAME,
            Key=s3_key,
            Body=report_text.encode("utf-8"),
            ContentType="application/json"
        )
        logging.info(f"Stored audio analysis report: {s3_key} in S3")
        return True
    except ClientError as e:
        logging.error(f"Error storing audio analysis report: {e}")
        return False

async def delete_s3_object(s3_key: str) -> bool:
    """
    Deletes an object from AWS S3.
    """
    try:
        s3_client.delete_object(
            Bucket=AWS_STORAGE_BUCKET_NAME,
            Key=s3_key,
        )
        logging.info(f"Deleted object: {s3_key} from S3")
        return True
    except ClientError as e:
        logging.error(f"Error deleting object from S3: {e}")
        return False

async def upload_audio_to_s3(file: UploadFile, user_id: UUID, session_id: UUID, timestamp: str) -> Optional[str]:
    """
    Uploads an audio file to AWS S3 and returns the URL and key.
    """
    s3_key = f"user_{user_id}/session_{session_id}/audio_{timestamp}.{file.filename.split('.')[-1]}"
    try:
        s3_client.upload_fileobj(
            Fileobj=file.file,
            Bucket=AWS_STORAGE_BUCKET_NAME,
            Key=s3_key,
            ExtraArgs={
                'ACL': 'public-read',
                'ContentType': file.content_type
            }
        )
        audio_url = f"https://{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
        return audio_url, s3_key
    except ClientError as e:
        logging.error(f"Error uploading audio to S3: {e}")
        return None, None

async def analyze_text_from_audio(transcript: str, workroom_kpis: List[dict]) -> ImageAnalysisResult:
    """
    Analyzes the audio transcript and categorizes activities based on workroom KPIs.
    This function is similar to analyze_image, but it takes text as input.
    """
    try:
        prompt_content = [
            {
                "type": "text",
                "text": "You are analyzing a transcript of a remote worker's audio. Now you are to analyze weather it aligns  Return your response as a JSON object conforming to the following schema:"
            },
            {
                "type": "text",
                "text": ImageAnalysisResult.schema_json()
            },
            {
                "type": "text",
                "text": f"KPIs: {workroom_kpis}"
            },
            {
                "type": "text",
                "text": f"Transcript: {transcript}"
            }
        ]

        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt_content}],
            temperature=0.5,
            max_completion_tokens=500,
        )
        analysis_json = completion.choices[0].message.content
        if analysis_json:
            try:
                return ImageAnalysisResult.parse_raw(analysis_json)
            except Exception as e:
                logging.error(f"Error parsing LLM output to ImageAnalysisResult: {e}, Raw output: {analysis_json}")
                return ImageAnalysisResult(activities=[], general_observations="Failed to parse LLM output.")
        else:
            return ImageAnalysisResult(activities=[], general_observations="No analysis from LLM.")

    except Exception as e:
        logging.error(f"Groq API Error during audio analysis: {str(e)}")
        return ImageAnalysisResult(activities=[], general_observations=f"Groq API error: {e}")


