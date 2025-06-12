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
from datetime import datetime
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
    """
    # Fetch the workroom and its members
    result = await session.execute(
        select(Workroom)
        .options(selectinload(Workroom.members).selectinload(User.streak))
        .where(Workroom.id == workroom_id)
    )
    workroom = result.scalar_one_or_none()
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")

    leaderboard_data = []

    for member in workroom.members:
        # 1. KPI Alignment Score
        kpi_result = await session.execute(
            select(UserKPISummary).where(
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
        completed_task_points = sum(task.task_point for task in completed_tasks)

        total_tasks_result = await session.execute(
            select(func.count(Task.id)).where(
                Task.workroom_id == workroom_id,
                Task.assigned_users.contains(member)
            )
        )
        total_assigned_tasks = total_tasks_result.scalar() or 0
        task_score = completed_task_points / total_assigned_tasks if total_assigned_tasks else 0.0

        # 3. Engagement Score
        live_sessions_result = await session.execute(
            select(func.count(WorkroomLiveSession.id)).where(
                WorkroomLiveSession.workroom_id == workroom_id,
                WorkroomLiveSession.screen_sharer_id == member.id
            )
        )
        live_session_count = live_sessions_result.scalar() or 0
        live_session_points = live_session_count * 2

        activity_boost = float(member.productivity or 0) * 0.2
        active_minutes_score = (member.daily_active_minutes or 0) / 30

        # ✅ Streak bonus using current_streak
        streak_bonus = member.streak.current_streak * 0.5 if member.streak else 0.0

        engagement_score = live_session_points + activity_boost + active_minutes_score + streak_bonus

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

    # Sort leaderboard by score descending, then by username
    leaderboard_data.sort(key=lambda x: (-x["score"], x["username"]))

    # Save leaderboard
    for rank, entry in enumerate(leaderboard_data, start=1):
        existing_result = await session.execute(
            select(Leaderboard).where(
                Leaderboard.workroom_id == workroom_id,
                Leaderboard.user_id == entry["user_id"]
            )
        )
        leaderboard_entry = existing_result.scalar_one_or_none()

        if leaderboard_entry:
            leaderboard_entry.score = entry["score"]
            leaderboard_entry.rank = rank
            leaderboard_entry.kpi_score = entry["kpi_score"]
            leaderboard_entry.task_score = entry["task_score"]
            leaderboard_entry.engagement_score = entry["engagement_score"]
        else:
            session.add(Leaderboard(
                workroom_id=workroom_id,
                user_id=entry["user_id"],
                score=entry["score"],
                rank=rank,
                kpi_score=entry["kpi_score"],
                task_score=entry["task_score"],
                engagement_score=entry["engagement_score"],
            ))

    await session.commit()


# --------------------------------------------------------------------------------
#  Image Analysis Functions
# --------------------------------------------------------------------------------

async def analyze_image(image_url: str, workroom_kpis: str) -> str:
    """
    Analyzes the image using Groq API and returns a plain text description.
    """
    try:
        prompt_content = [
            {
                "type": "text",
                "text": (
                    "You are analyzing a screenshot of a remote worker's screen. "
                    "Describe the applications and activities visible. Mention any observations "
                    "that relate to the following KPIs: "
                    f"{workroom_kpis}. "
                    "Avoid performance metrics or unrelated details. "
                    "Your response should be plain, readable text (not in JSON or any other format)."
                    "And kepp it short and concise to no more than 100 words."
                )
            },
            {"type": "image_url", "image_url": {"url": image_url}},
        ]

        completion = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role": "user", "content": prompt_content}],
            temperature=0.5,
            max_completion_tokens=500,
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
    # Get session
    session_obj = await db.get(WorkroomLiveSession, session_id)
    if not session_obj:
        raise HTTPException(status_code=404, detail="Session not found")

    # Eagerly load workroom + relationships to avoid lazy load issues
    result = await db.execute(
        select(Workroom)
        .options(selectinload(Workroom.performance_metrics))
        .where(Workroom.id == workroom_id)
    )
    workroom = result.scalar_one_or_none()
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")

    # Get user activity results
    all_activities = await get_all_analysis_results(user_id, session_id)
    if not all_activities:
        raise HTTPException(status_code=404, detail="No analysis results found")
    logging.info(f"Activities found: {len(all_activities)}")

    # Prepare LLM prompt
    activity_summaries = all_activities
    kpi_metrics = [f"- {pm.kpi_name}: weight-{pm.weight}" for pm in workroom.performance_metrics]
    kpi_metrics_text = "\n".join(kpi_metrics)

    prompt = f"""
    You are an AI assistant helping evaluate user performance in a remote workroom session.

    You are given a list of user activities detected from screenshots and audio recordings during the session:

    User Activities:
    {activity_summaries}

    Below are the detailed performance metrics associated with this KPI:
    {kpi_metrics_text}

    Your task is to assess how well the user's activities during the session align with the primary KPI and the individual performance metrics.

    Return the following:

    1. A brief, natural language summary of the session (focusing on user behavior and alignment with the KPI) Write this like you are writing to the team member's manager.
    2. An overall alignment percentage (0 to 100) reflecting how well the user's activities align with the main KPI.
    3. Don't say anything regarding confidence scores just respond like a human accessing the user's activities.
    4. A breakdown of each performance metric, including:
    - the metric's name
    - a percentage (0 to 100) indicating the user's alignment with it.

    ⚠️ Strictly return the response as a **valid JSON object** matching this schema:

    {UserDailyKPIReport.schema_json(indent=2)}

    ⚠️ Do NOT include any additional text or explanations. Only the JSON object.

    Ensure the percentages are numerical values, not strings, and that keys match the schema exactly.
    """

    # LLM Completion Call
    try:
        # Send the request to the LLM model
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_completion_tokens=500
        )

        # Get the response content
        result = completion.choices[0].message.content

        # Step 1: Remove leading and trailing backticks
        cleaned_result = result.strip("`").strip()

        # Step 2: Parse the cleaned result as JSON
        try:
            summary_data = UserDailyKPIReport.parse_raw(cleaned_result)
            logging.info(f"LLM summary result: {summary_data}")
        except json.JSONDecodeError as e:
            logging.error(f"LLM JSON decode error: {e} - {cleaned_result}")
            raise HTTPException(status_code=500, detail="LLM summary parse failed")

    except Exception as e:
        logging.error(f"LLM response parse error: {e} - {result}")
        raise HTTPException(status_code=500, detail="LLM summary parse failed")

    existing_summary = await db.execute(
        select(UserKPISummary)
        .where(UserKPISummary.user_id == user_id, 
               UserKPISummary.workroom_id == workroom.id,
               UserKPISummary.session_id == session_id)
    )
    existing_summary = existing_summary.scalar_one_or_none()

    if existing_summary:
        # If it exists, update the existing entry
        existing_summary.overall_alignment_percentage = summary_data.overall_alignment_percentage
        existing_summary.kpi_breakdown = {k.kpi_name: k.percentage for k in summary_data.kpi_breakdown}
        existing_summary.summary_text = summary_data.summary_text
        existing_summary.date = session_obj.ended_at.date() if session_obj.ended_at else datetime.utcnow().date()
    else:
        # If not found, create a new entry
        new_summary = UserKPISummary(
            user_id=user_id,
            session_id=session_id,
            workroom_id=workroom.id,
            overall_alignment_percentage=summary_data.overall_alignment_percentage,
            kpi_breakdown={k.kpi_name: k.percentage for k in summary_data.kpi_breakdown},
            summary_text=summary_data.summary_text,
            date=session_obj.ended_at.date() if session_obj.ended_at else datetime.utcnow().date()
        )
        db.add(new_summary)

    # Save KPI Breakdown History
    today = datetime.utcnow().date()

    # Save or Update KPI Breakdown History
    for kpi in summary_data.kpi_breakdown:
        # Check for existing entry
        existing_entry = await db.execute(
            select(UserKPIMetricHistory).where(
                UserKPIMetricHistory.user_id == user_id,
                UserKPIMetricHistory.workroom_id == workroom.id,
                UserKPIMetricHistory.kpi_name == kpi.kpi_name,
                UserKPIMetricHistory.date == today
            )
        )
        existing_entry = existing_entry.scalars().first()

        if existing_entry:
            # Update existing entry
            existing_entry.alignment_percentage = kpi.percentage
        else:
            # Create new entry
            db.add(UserKPIMetricHistory(
                user_id=user_id,
                workroom_id=workroom.id,
                kpi_name=kpi.kpi_name,
                alignment_percentage=kpi.percentage,
                date=today
            ))

    await db.commit()
    return summary_data

#   --------------------------------------------------------------------------------
#   KPI Functions
#   --------------------------------------------------------------------------------
async def generate_summary_text(
    all_summary_texts: str,
    average_alignment: float,
    combined_kpi_breakdown: dict[str, float],
) -> str:
    # Format breakdown into readable bullet points
    kpi_breakdown_text = "\n".join(
        f"- {kpi}: {round(score, 2)}%" for kpi, score in combined_kpi_breakdown.items()
    )

    prompt = f"""
    You are a productivity analyst AI. Your job is to help a manager understand how their team performed in a remote workroom session.

    The team's **average alignment score** with this KPI was: {round(average_alignment, 2)}%

    The **aggregated KPI breakdown** across all users is:
    {kpi_breakdown_text}

    Below are detailed summaries for each user (generated by AI based on screenshots and session activity):
    {all_summary_texts}

    Using the data above:
    1. Write a concise, professional summary for the manager about how the team performed.
    2. Highlight strengths and any concerning patterns.
    3. Offer 1-2 useful suggestions or recommendations for the manager.
    4. Make sure your tone is informative and not judgmental.

    Only return the summary. Do not include any additional markup or headers.
    """

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_completion_tokens=500
        )
        result = response.choices[0].message.content.strip()
        return result

    except Exception as e:
        logging.error(f"Failed to generate workroom KPI summary: {e}")
        return "Summary generation failed due to an internal error."

async def calculate_workroom_kpi_overview(workroom_id: UUID, session: AsyncSession):
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")

    today = datetime.utcnow().date()

    summaries_result = await session.execute(
        select(UserKPISummary).where(
            UserKPISummary.workroom_id == workroom_id,
            UserKPISummary.date == today
        )
    )
    summaries = summaries_result.scalars().all()

    if not summaries:
        raise HTTPException(status_code=404, detail="No KPI summaries found for today.")

    total_alignment = sum(s.overall_alignment_percentage for s in summaries)
    average_alignment = total_alignment / len(summaries)

    combined_kpi_breakdown = defaultdict(float)
    for summary in summaries:
        for kpi_name, percentage in summary.kpi_breakdown.items():
            combined_kpi_breakdown[kpi_name] += percentage

    # Average the scores
    for kpi in combined_kpi_breakdown:
        combined_kpi_breakdown[kpi] /= len(summaries)

    # Store in WorkroomKPIMetricHistory
    for kpi_name, value in combined_kpi_breakdown.items():
        session.add(WorkroomKPIMetricHistory(
            workroom_id=workroom_id,
            date=today,
            kpi_name=kpi_name,
            metric_value=round(value, 2)
        ))
    # if workroom.kpis:
    #     session.add(WorkroomKPIMetricHistory(
    #         workroom_id=workroom_id,
    #         date=today,
    #         kpi_name=workroom.kpis,
    #         metric_value=round(average_alignment, 2)
    #     ))

    all_summary_texts = " ".join(s.summary_text for s in summaries)

    # Call LLM to generate the final managerial summary
    generated_summary = await generate_summary_text(
        all_summary_texts,
        average_alignment,
        dict(combined_kpi_breakdown)
    )

    session.add(WorkroomKPISummary(
        workroom_id=workroom_id,
        date=today,
        overall_alignment_percentage=average_alignment,
        kpi_breakdown=dict(combined_kpi_breakdown),
        summary_text=generated_summary
    ))

    existing_kpi_result = await session.execute(
        select(WorkroomOverallKPI).where(WorkroomOverallKPI.workroom_id == workroom_id)
    )
    existing_kpi = existing_kpi_result.scalar_one_or_none()

    if existing_kpi:
        existing_kpi.overall_alignment_score = average_alignment
    else:
        session.add(WorkroomOverallKPI(
            workroom_id=workroom_id,
            overall_alignment_score=average_alignment
        ))

    await session.commit()

    return {
        "overall_alignment": round(average_alignment, 2),
        "kpi_breakdown": {k: round(v, 2) for k, v in combined_kpi_breakdown.items()},
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


