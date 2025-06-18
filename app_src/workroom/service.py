from collections import defaultdict
import json
import logging
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
from datetime import datetime, timezone
from .schema import ImageAnalysisResult, UserDailyKPIReport


GROQ_API_KEY = Config.GROQ_API_KEY
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
    # Fetch the workroom and its members
    result = await session.execute(
        select(Workroom)
        .options(selectinload(Workroom.members).selectinload(User.streak))
        .where(Workroom.id == workroom_id)
    )
    workroom = result.scalar_one_or_none()
    if not workroom:
        raise ValueError(f"Workroom {workroom_id} not found")

    leaderboard_data = []

    for member in workroom.members:
        try:
            # 1. KPI Alignment Score - Get the most recent one to handle duplicates
            kpi_result = await session.execute(
                select(UserKPISummary)
                .where(
                    UserKPISummary.user_id == member.id,
                    UserKPISummary.workroom_id == workroom_id
                )
            )
            user_kpi_summary = kpi_result.scalar_one_or_none()
            kpi_score = user_kpi_summary.overall_alignment_percentage if user_kpi_summary else 0.0

            # 2. Task Score
            completed_tasks_result = await session.execute(
                select(Task).where(
                    Task.workroom_id == workroom_id,
                    Task.assigned_users.contains(member),
                    Task.status == TaskStatus.COMPLETED,
                    Task.kpi_link.isnot(None)
                )
            )
            completed_tasks = completed_tasks_result.scalars().all()
            completed_task_points = sum(task.task_point for task in completed_tasks if task.task_point)

            total_tasks_result = await session.execute(
                select(func.count(Task.id)).where(
                    Task.workroom_id == workroom_id,
                    Task.assigned_users.contains(member)
                )
            )
            total_assigned_tasks = total_tasks_result.scalar() or 0
            task_score = completed_task_points / total_assigned_tasks if total_assigned_tasks > 0 else 0.0

            # 3. Engagement Score
            live_sessions_result = await session.execute(
                select(func.count(WorkroomLiveSession.id)).where(
                    WorkroomLiveSession.workroom_id == workroom_id,
                    WorkroomLiveSession.screen_sharer_id == member.id
                )
            )
            live_session_count = live_sessions_result.scalar() or 0
            live_session_points = live_session_count * 2

            # Safe handling of potentially None values
            active_minutes_score = (member.daily_active_minutes or 0) / 30
            streak_bonus = (member.streak.current_streak or 0) * 0.5 if member.streak else 0.0
            engagement_score = live_session_points + active_minutes_score + streak_bonus

            # 4. Penalty Score
            missed_tasks_result = await session.execute(
                select(Task).where(
                    Task.workroom_id == workroom_id,
                    Task.assigned_users.contains(member),
                    Task.status != TaskStatus.COMPLETED,
                    Task.due_by < datetime.utcnow()
                )
            )
            missed_tasks = missed_tasks_result.scalars().all()
            penalty_score = len(missed_tasks) * 2

            # Final Score Calculation
            total_score = kpi_score + task_score + engagement_score - penalty_score

            leaderboard_data.append({
                "user_id": member.id,
                "username": member.first_name or "Unnamed",
                "score": total_score,
                "kpi_score": kpi_score,
                "task_score": task_score,
                "engagement_score": engagement_score,
            })

        except Exception as e:
            logging.error(f"Error calculating scores for user {member.id}: {e}")
            # Add user with zero scores to avoid missing them entirely
            leaderboard_data.append({
                "user_id": member.id,
                "username": member.first_name or "Unnamed",
                "score": 0.0,
                "kpi_score": 0.0,
                "task_score": 0.0,
                "engagement_score": 0.0,
            })

    # Sort leaderboard by score descending, then by username
    leaderboard_data.sort(key=lambda x: (-x["score"], x["username"]))

    # Save leaderboard - Use merge or upsert pattern
    for rank, entry in enumerate(leaderboard_data, start=1):
        try:
            existing_result = await session.execute(
                select(Leaderboard).where(
                    Leaderboard.workroom_id == workroom_id,
                    Leaderboard.user_id == entry["user_id"]
                )
            )
            leaderboard_entry = existing_result.scalar_one_or_none()

            if leaderboard_entry:
                # Update existing entry
                leaderboard_entry.score = entry["score"]
                leaderboard_entry.rank = rank
                leaderboard_entry.kpi_score = entry["kpi_score"]
                leaderboard_entry.task_score = entry["task_score"]
                leaderboard_entry.engagement_score = entry["engagement_score"]
                leaderboard_entry.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            else:
                # Create new entry
                new_leaderboard_entry = Leaderboard(
                    workroom_id=workroom_id,
                    user_id=entry["user_id"],
                    score=entry["score"],
                    rank=rank,
                    kpi_score=entry["kpi_score"],
                    task_score=entry["task_score"],
                    engagement_score=entry["engagement_score"],
                )
                session.add(new_leaderboard_entry)

        except Exception as e:
            logging.error(f"Error updating leaderboard for user {entry['user_id']}: {e}")

    logging.info(f"Updated leaderboard for workroom {workroom_id} with {len(leaderboard_data)} entries")


# --------------------------------------------------------------------------------
#  Image Analysis Functions
# --------------------------------------------------------------------------------

async def analyze_image(image_url: str, kpi_names: set) -> str:
    """
    Analyzes the image using Groq API and returns a plain text description.
    """
    try:
        if not kpi_names:
            kpi_names = {"productivity", "focus", "collaboration"}
            
        kpi_list = ", ".join(kpi_names)
        
        prompt_content = [
            {
                "type": "text",
                "text": (
                    "Analyze this screenshot of a user's work session. Focus on identifying activities that relate "
                    "to these specific performance metrics: " + kpi_list + ". "
                    "Describe what applications/tools are visible and how they're being used. "
                    "Note any signs of productive work, collaboration, or distractions. "
                    "For example, if you see coding tools, document editors, communication apps, "
                    "or entertainment sites, mention how they relate to the KPIs. "
                    "Keep your response concise (50-100 words) and directly relevant to work performance. "
                    "Format as plain text with no special formatting or bullet points."
                )
            },
            {"type": "image_url", "image_url": {"url": image_url}},
        ]

        completion = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role": "user", "content": prompt_content}],
            temperature=0.5,
            max_completion_tokens=200,
        )

        analysis_text = completion.choices[0].message.content
        return analysis_text.strip() if analysis_text else "No analysis returned."

    except Exception as e:
        logging.error(f"Groq API error during image analysis: {e}")
        return f"Groq API error: {str(e)}"

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
    await store_analysis_result(analysis_result.json(), image_filename)

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

    # Prepare KPI metrics with weights
    kpi_metrics = [
        {
            "name": pm.kpi_name,
            "weight": pm.weight,
            "description": f"Importance: {pm.weight}/10"
        } 
        for pm in workroom.performance_metrics
    ]

    # Prepare LLM prompt with personalized context
    prompt = f"""
    You are an AI performance analyst evaluating a team member's work session.

    Team Member: {user.first_name} {user.last_name}
    Workroom: {workroom.name}
    Session Date: {session_obj.start_time.date() if session_obj.start_time else 'Today'}

    Below are the performance metrics for this workroom with their importance weights:
    {json.dumps(kpi_metrics, indent=2)}

    Here are the detected activities from {user.first_name}'s session:
    {json.dumps(all_activities, indent=2)}

    Your task:
    1. Write a concise 3-4 sentence summary of {user.first_name}'s performance, highlighting:
       - Positive behaviors aligned with KPIs
       - Areas needing improvement
       - Overall work quality
    2. For each KPI, provide an alignment percentage (0-100) based on:
       - Time spent on relevant activities
       - Quality of engagement
       - Weight/importance of the KPI

    Return a JSON object with this exact structure:
    {{
        "summary_text": "Your summary here...",
        "kpi_breakdown": [
            {{
                "kpi_name": "KPI Name",
                "percentage": 85.0
            }},
            ...
        ]
    }}

    Important:
    - Only return valid JSON
    - Include all KPIs in the breakdown
    - Percentages should be floats
    - Be objective but constructive
    """

    # LLM Completion Call
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            response_format={"type": "json_object"},
            max_completion_tokens=800
        )

        # Parse and validate response
        result = completion.choices[0].message.content
        summary_data = UserDailyKPIReport.parse_raw(result)
        
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
        # for kpi in summary_data.kpi_breakdown:
        #     # Check for existing entry
        #     existing_entry = await db.execute(
        #         select(UserKPIMetricHistory).where(
        #             UserKPIMetricHistory.user_id == user_id,
        #             UserKPIMetricHistory.workroom_id == workroom.id,
        #             UserKPIMetricHistory.kpi_name == kpi.kpi_name,
        #             UserKPIMetricHistory.date == today
        #         )
        #     )
        #     existing_entry = existing_entry.scalars().first()

        #     if existing_entry:
        #         existing_entry.alignment_percentage = kpi.percentage
        #     else:
        #         db.add(UserKPIMetricHistory(
        #             user_id=user_id,
        #             workroom_id=workroom.id,
        #             kpi_name=kpi.kpi_name,
        #             alignment_percentage=kpi.percentage,
        #             date=today
        #         ))

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

    except Exception as e:
        await db.rollback()
        logging.error(f"Error generating KPI summary: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="Failed to generate performance summary"
        )

#   --------------------------------------------------------------------------------
#   KPI Functions
#   --------------------------------------------------------------------------------

async def calculate_workroom_kpi_overview(workroom_id: UUID, session: AsyncSession):
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

    # Get all user summaries for today
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

    # Generate summary text using LLM
    all_summary_texts = [s.summary_text for s in summaries if s.summary_text]
    llm_prompt = f"""
    You are analyzing daily performance summaries for an entire workroom team.
    Below are individual member summaries from today:

    Member Summaries:
    {chr(10).join(all_summary_texts)}

    Key Metrics:
    - Overall Alignment: {round(average_alignment, 2)}%
    - KPI Breakdown: {json.dumps(averaged_kpi_breakdown, indent=2)}

    Generate a concise 3-4 paragraph executive summary highlighting:
    1. Overall team performance today
    2. Key strengths and areas for improvement
    3. Notable individual contributions (mention names if particularly good/bad)
    4. Recommendations for tomorrow

    Write in professional but approachable tone for managers.
    Focus on patterns and team-level insights rather than individual details.
    """
    
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": llm_prompt}],
            temperature=0.5,
            max_completion_tokens=500
        )
        generated_summary = completion.choices[0].message.content
    except Exception as e:
        logging.error(f"LLM summary generation failed: {e}")
        generated_summary = "Automatic summary unavailable. Please check individual member reports."

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

    # Update or create metric entries
    for kpi_name, value in averaged_kpi_breakdown.items():
        existing_metric = next(
            (m for m in existing_metrics if m.kpi_name == kpi_name),
            None
        )
        if existing_metric:
            existing_metric.metric_value = round(value, 2)
        else:
            session.add(WorkroomKPIMetricHistory(
                workroom_id=workroom_id,
                date=today,
                kpi_name=kpi_name,
                metric_value=round(value, 2)
            ))

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


