from collections import defaultdict
import json
import logging
import re
import requests
import io
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID
from app_src.db.models import (TaskStatus, UserKPIMetricHistory, UserKPISummary, Workroom, Leaderboard, 
                       Task, WorkroomKPIMetricHistory, WorkroomKPISummary, 
                       WorkroomLiveSession, WorkroomOverallKPI, User)
import cloudinary
import cloudinary.uploader
import cloudinary.api
import time
from app_src.config import Config
from google import genai
from google.genai import types
from datetime import datetime, timezone, timedelta
from .schema import UserDailyKPIReport
from typing import List


GEMINI_API_KEY = Config.GEMINI_API_KEY
if not GEMINI_API_KEY:
    logging.error("GEMINI_API_KEY is not set in the environment variables.")

# Initialize the new Google Gen AI Client
genai_client = genai.Client(api_key=GEMINI_API_KEY)
DEFAULT_MODEL = "gemini-2.5-flash"

# Cloudinary Configuration
cloudinary.config(
    cloud_name=Config.CLOUDINARY_CLOUD_NAME,
    api_key=Config.CLOUDINARY_API_KEY,
    api_secret=Config.CLOUDINARY_API_SECRET
)

def generate_presigned_url(public_id: str, expiry_seconds: int = 3600) -> str:
    """Generate a presigned URL to access a Cloudinary resource."""
    try:
        # For Cloudinary, secure URLs are always accessible, but we can generate signed URLs for security
        url = cloudinary.utils.cloudinary_url(
            public_id,
            secure=True,
            sign_url=True,
            expires_at=int(time.time()) + expiry_seconds
        )[0]
        return url
    except Exception as e:
        logging.error(f"Error generating presigned URL: {e}")
        return None

async def get_all_analysis_results(user_id: UUID, session_id: UUID) -> List[str]:
    """
    Retrieves all plain text analysis result files stored in Cloudinary for a given user session.

    Returns:
        A list of strings, each representing the text content of one analysis result file.
    """
    folder_path = f"hudddleBackend/user_{user_id}/session_{session_id}"
    analysis_data = []

    try:
        # Search for analysis text files in user's session folder
        # Use Admin API for immediate consistency (Search API has latency)
        # prefix matches the folder path
        result = cloudinary.api.resources(
            type="upload",
            prefix=folder_path, 
            resource_type="raw",
            max_results=500
        )
        
        logging.info(f"Analysis fetch for {folder_path} found {len(result.get('resources', []))} files")

        for resource in result.get('resources', []):
            public_id = resource['public_id']
            try:
                # Get the raw content from Cloudinary
                # Note: For raw files, we need to fetch the content differently
                secure_url = resource['secure_url']
                
                # Download the content
    
                response = requests.get(secure_url)
                if response.status_code == 200:
                    content = response.text.strip()
                    if content:
                        analysis_data.append(content)
                    else:
                        logging.warning(f"File {public_id} is empty. Skipping.")
                else:
                    logging.error(f"Failed to download file {public_id}: HTTP {response.status_code}")
                    
            except Exception as read_error:
                logging.error(f"Failed to read file {public_id}: {read_error}")
                
        return analysis_data

    except Exception as e:
        logging.error(f"Error listing analysis results from Cloudinary: {e}")
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
    # Log top 3 for verification
    for i, entry in enumerate(leaderboard_data[:3]):
        logging.info(f"Leaderboard Rank {i+1}: {entry['username']} - Score: {entry['score']}")


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
    Analyzes the image using Gemini Vision API and returns a plain text description.
    """
    try:
        if not kpi_names:
            kpi_names = {"productivity", "focus", "collaboration"}
            
        kpi_list = ", ".join(kpi_names)
        
        # Validate image URL
        if not validate_image_url(image_url):
            logging.error(f"Invalid or inaccessible image URL: {image_url}")
            return "[Image analysis skipped: Invalid or inaccessible image URL]"

        # Download image for Gemini
        img_response = requests.get(image_url)
        if img_response.status_code != 200:
            return "[Image analysis failed: Could not download image]"
        
        prompt = (
            "Analyze this screenshot to determine the user's current work activity and focus level. "
            f"Evaluate against these KPIs: {kpi_list}.\n\n"
            
            "ANALYSIS REQUIREMENTS:\n"
            "1. Application Identification: List all visible applications, IDEs, browsers, and their specific content\n"
            "2. Activity Detection: Determine the primary task (coding, debugging, documentation, communication, browsing, etc.)\n"
            "3. Focus Assessment: Evaluate work relevance and concentration level based on visible content\n"
            "4. Evidence: Reference specific UI elements, text, code snippets, or tab titles visible in the screenshot\n\n"
            
            "GOOD EXAMPLES:\n"
            "Example 1: 'VS Code displaying Python file (auth_service.py) with OAuth implementation code visible. "
            "Terminal shows pytest running unit tests. Chrome has Stack Overflow tab open about Python decorators. "
            "High productivity - actively developing authentication feature with research support.'\n\n"
            
            "Example 2: 'Figma design tool active showing mobile app mockups labeled \"Dashboard v2\". "
            "Slack window visible with #design-review channel. Notion tab contains sprint planning notes. "
            "Moderate-high focus - UI/UX design work with team collaboration.'\n\n"
            
            "BAD EXAMPLES:\n"
            "Example 1: 'The user is working on a computer.' "
            "[TOO VAGUE - no specific applications, activities, or evidence identified]\n\n"
            
            "Example 2: 'User has multiple tabs open and seems busy.' "
            "[LACKS DETAIL - doesn't specify what tabs, what work, or provide objective evidence]\n\n"
            
            "OUTPUT FORMAT:\n"
            "Provide a 50-80 word analysis that:\n"
            "- Names specific applications and their visible content\n"
            "- Identifies the concrete task being performed\n"
            "- Assesses focus/productivity with evidence\n"
            "- Maps findings to relevant KPIs\n"
            "- Uses objective, factual language (avoid assumptions)"
        )

        contents = [
            types.Part.from_bytes(
                data=img_response.content,
                mime_type='image/png'
            ),
            prompt
        ]
        
        response = await genai_client.aio.models.generate_content(
            model=DEFAULT_MODEL,
            contents=contents
        )
        return response.text.strip() if response.text else "No analysis returned."

    except Exception as e:
        logging.error(f"Gemini Vision error for {image_url}: {e}")
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

    # Store the analysis result to Cloudinary
    await store_analysis_result(analysis_result, image_filename, user_id, session_id)

async def store_analysis_result(analysis_text: str, original_image_filename: str, user_id: UUID, session_id: UUID) -> bool:
    """
    Stores the plain text analysis result in Cloudinary as a raw file.
    """
    try:
        # Create the file path structure similar to your original S3 structure
        file_name_without_ext = original_image_filename.rsplit('.', 1)[0]
        cloudinary_public_id = f"hudddleBackend/user_{user_id}/session_{session_id}/analysis_{file_name_without_ext}"

        # Upload the analysis text as a raw file to Cloudinary
        # Convert text to bytes stream so Cloudinary doesn't treat it as a file path
        file_stream = io.BytesIO(analysis_text.encode('utf-8'))
        
        result = cloudinary.uploader.upload(
            file_stream,
            public_id=cloudinary_public_id,
            resource_type="raw",
            format="txt"
        )
        
        logging.info(f"Analysis result stored in Cloudinary: {result['public_id']}")
        return True
        
    except Exception as e:
        logging.error(f"Error storing analysis result in Cloudinary: {e}")
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


    # Create fallback response earlier so it can be used for early returns
    fallback_response = UserDailyKPIReport(
        summary_text="No analysis found",
        kpi_breakdown=[
            {"kpi_name": pm.kpi_name, "percentage": 0.0}
            for pm in workroom.performance_metrics
        ]
    )

    # Get all screenshots for this session
    screenshots, _ = await get_user_session_screenshots(user_id, session_id)
    
    # Get user activity results
    all_activities = await get_all_analysis_results(user_id, session_id)
    
    if screenshots and not all_activities:
        # Screenshots exist but no analysis yet, likely still processing
        raise HTTPException(status_code=404, detail=f"Analysis in progress for {len(screenshots)} screenshots...")
        
    summary_data = None
    
    if not screenshots and not all_activities:
        # Truly no data for this session
        logging.info(f"No data found for session {session_id}, using fallback summary")
        summary_data = fallback_response
    
    if not summary_data:
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
        
        try:
            # Create the prompt content
            kpi_metrics_json = json.dumps(kpi_metrics, indent=2)
            all_activities_json = json.dumps(all_activities, indent=2)
            recent_tasks_json = json.dumps(recent_tasks_info, indent=2)

            logging.info(f"Generating summary for User: {user.first_name}, Workroom: {workroom.name}")
            logging.info(f"Input Data - Activities: {len(all_activities)} items, Tasks: {len(recent_completed_tasks)} items")
            
            user_content = f"""
            Hey {user.first_name}! You're {user.first_name}'s personal performance analyst. Your job is to review their work session 
            and give them genuinely useful insights—not corporate fluff.

            SESSION DATA:
            - **Workroom**: {workroom.name}
            - **Workroom's KPI Metrics & Weights**: {kpi_metrics_json}
            - **Detected Activities**: {all_activities_json}
            - **Completed Tasks (last 6 hours)**: {recent_tasks_json}

            ANALYSIS APPROACH:
            1. **Match Tasks to Activities**: Look for evidence that completed tasks actually happened. 
            If they claimed "Fixed React bug" but you only see Spotify and Twitter—call it out (diplomatically).

            2. **Tool-Task Correlation**: Check if the tools they used align with their tasks. 
            Example: "Built API endpoint" should show IDE/terminal activity, not just Slack.

            3. **KPI Story**: Explain the "why" behind the numbers. High Focus + Low Productivity might mean 
            they're deep in research. High Collaboration + Low Focus could be meeting overload.

            4. **Smart Recommendations**: Give specific, actionable advice based on patterns you notice. 
            Skip the motivational poster quotes.

            GOOD EXAMPLES:

            Example 1 - Insights:
            "• Strong correlation detected: VS Code activity (Python files) aligns perfectly with completed 
            task 'Database migration script'. Terminal shows multiple git commits during this window.
            - {workroom.name} tools used effectively—Notion for planning, Figma for quick UI reference.
            - Focus score high (92%) but Productivity moderate (68%)—likely due to extended debugging session 
            visible in Chrome DevTools."

            Example 1 - Recommendations:
            "• Consider time-boxing debugging sessions to maintain productivity momentum.
            - Great use of Notion for documentation—maybe add a 'blockers' section to track recurring issues."

            Example 2 - Insights:
            "• Task claimed: 'Completed marketing slides' but screenshot analysis shows 6 different tools 
            active simultaneously (Slack, Gmail, Canva, Spotify, Twitter, Calendar). Possible context-switching overhead.
            - Collaboration score elevated (85%) due to active Slack conversations, but may be impacting deep work time."

            Example 2 - Recommendations:
            "• Try batching communication—dedicate specific time blocks for Slack/email to protect focus time.
            - Your multi-tool workflow suggests async work might help. Consider 'Do Not Disturb' mode for creative tasks."

            BAD EXAMPLES:

            Example 1:
            "• User was productive today.
            - Keep up the good work!
            - Tasks completed successfully."
            [WHY IT'S BAD: Zero specifics, no tool analysis, generic cheerleading, no actionable insights]

            Example 2:
            "• Low productivity detected. User needs to focus more.
            - Too many distractions observed.
            - Recommend better time management."
            [WHY IT'S BAD: Judgmental tone, vague observations, no evidence cited, unhelpful recommendations]

            OUTPUT REQUIREMENTS:
            - **Tone**: Friendly but professional. Like a helpful colleague, not a corporate bot.
            - **Evidence-Based**: Reference specific tools, tasks, and patterns from the data.
            - **Balanced**: Acknowledge what's working AND what could improve.
            - **Actionable**: Every recommendation should be something they can actually do.
            - **Concise**: 3-4 insights max, 2-3 recommendations max.

            Return this exact JSON structure:
            {{
                "summary_text": "**📊 Session Insights**\\n\\n• [Specific insight with evidence]\\n• [Pattern observation with data]\\n• [KPI interpretation with context]\\n\\n**💡 Recommendations**\\n\\n• [Actionable step with rationale]\\n• [Specific improvement suggestion]",
                "kpi_breakdown": [
                    {{"kpi_name": "KPI Name", "percentage": 95.5}}
                ]
            }}
            Remember: Be honest but constructive. The goal is to help {user.first_name} work smarter, not just harder.
            """
            
            response = await genai_client.aio.models.generate_content(
                model=DEFAULT_MODEL,
                contents=user_content,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    response_mime_type="application/json"
                )
            )
            
            parsed_data = json.loads(response.text)
            
            kpi_breakdown = [
                {"kpi_name": item["kpi_name"], "percentage": float(item["percentage"])}
                for item in parsed_data["kpi_breakdown"]
            ]
            summary_data = UserDailyKPIReport(
                summary_text=parsed_data["summary_text"],
                kpi_breakdown=kpi_breakdown
            )
        except Exception as e:
            logging.warning(f"Gemini summary generation failed: {str(e)}")
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
    await db.commit()
    logging.info(f"Saved User KPI Summary for {user.first_name}: Alignment={overall_alignment:.2f}%")
    logging.info(f"Summary Text Preview: {summary_data.summary_text[:100]}...")
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
        logging.warning(f"No KPI summary found for user {user_id} today. Skipping overview calculation.")
        return
    
    # Get all user summaries for today (only for alignment calculation)
    summaries_result = await session.execute(
        select(UserKPISummary).where(
            UserKPISummary.workroom_id == workroom_id,
            UserKPISummary.date == today
        )
    )
    summaries = summaries_result.scalars().all()

    if not summaries:
        logging.warning(f"No KPI summaries found for today in workroom {workroom_id}. Skipping overview calculation.")
        return

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
        
        existing_team_summary = f"**Existing Team Summary:**\n{texts_for_llm[1]}" if len(texts_for_llm) > 1 else ""
        
        logging.info(f"Generating Workroom Overview for {workroom.name}. Average Alignment: {average_alignment:.2f}%")
        
        user_content = f"""
        You're generating an executive summary for the {workroom.name} project manager. Your goal: give them 
        a clear, actionable snapshot of team performance without making them dig through data.

        📊 TODAY'S DATA:

        **Current User Summary:**
        {texts_for_llm[0]}

        {existing_team_summary}

        **Key Metrics:**
        - Overall Team Alignment: {round(average_alignment, 2)}%
        - KPI Breakdown: {kpi_breakdown_json}

        ANALYSIS GUIDELINES:

        1. **Connect the Dots**: Look for patterns across team members. Are multiple people blocked on the same thing? 
        Is collaboration strong or are people working in silos?

        2. **Be Specific**: Instead of "team is productive," say "3 engineers shipped features ahead of schedule; 
        2 designers completed 4 mockup iterations based on stakeholder feedback."

        3. **Context Matters**: A 65% alignment score might be great for a research-heavy day but concerning 
        for a sprint deadline. Explain the "why" behind the numbers.

        4. **Name Names Strategically**: Highlight standout performers and those who might need support, 
        but keep it constructive, not callout culture.

        5. **Actionable Over Observational**: Every insight should lead to a clear next step for the manager.

        GOOD EXAMPLES:

        Example 1 - Team Performance Summary:
        "• Team achieved 78% overall alignment today—strong showing during feature freeze period.
        - 4 out of 6 engineers maintained high focus (85%+) while resolving critical bugs in the payment module.
        - Cross-functional collaboration evident: design and frontend teams synced 3x via Figma comments and Slack, 
        reducing back-and-forth on the checkout UI."

        Example 1 - Key Strengths:
        "• Sarah (backend) shipped the API retry logic ahead of schedule with comprehensive test coverage—terminal 
        logs show 47 commits and successful CI/CD pipeline runs.
        - Documentation velocity up 40%—team actively using Notion to capture decisions during standups.
        - Zero context-switching waste detected during core working hours (10am-2pm), indicating effective meeting scheduling."

        Example 1 - Areas for Improvement:
        "• Two team members (Alex, Jordan) show low task-to-activity correlation (45%)—claimed tasks don't match 
        detected tool usage. May indicate unclear requirements or blockers not being communicated.
        - Collaboration score dropped to 52% after 3pm—suggests asynchronous handoffs aren't happening smoothly 
        between time zones.
        - Design team's Figma activity peaks during development team's focus time, creating potential review bottlenecks."

        Example 1 - Recommendations for Tomorrow:
        "• Quick 15-min check-in with Alex and Jordan to identify blockers—low correlation often signals stuck work.
        - Consider shifting design reviews to morning standup to align with dev team's active hours.
        - Dedicate first hour tomorrow to async updates in Slack #progress channel to boost afternoon alignment."

        Example 2 - Team Performance Summary:
        "• 82% alignment across the team—excellent coordination during the product demo prep week.
        - All 5 team members actively contributed to the presentation deck (Google Slides), showing strong collaborative ownership.
        - High focus detected (avg 88%) but productivity variance is wide (48%-91%), suggesting uneven workload distribution."

        Example 2 - Recommendations for Tomorrow:
        "• Rebalance task distribution—Mike and Lisa are at 91% productivity while others hover around 50-60%. 
        Check if they're over-allocated or if others need clearer priorities.
        - Lock down 2-4pm as 'no meetings' to protect deep work time—current calendar shows 6 team meetings scattered throughout the day.
        - Set up a quick knowledge transfer session—Mike's terminal activity suggests he's the only one who knows the deployment process."

        BAD EXAMPLES:

        Example 1:
        "**Team Performance Summary**
        - The team worked hard today.
        - Overall performance was good.
        - Everyone was busy with their tasks.

        **Key Strengths**
        - Team members are dedicated.
        - Good collaboration observed.

        **Areas for Improvement**
        - Some people could be more productive.
        - Communication needs improvement.

        **Recommendations for Tomorrow**
        - Keep up the good work.
        - Try to improve focus.
        - Communicate better."

        [WHY IT'S BAD: Zero specifics, no data cited, generic observations, no actionable insights, no names, 
        no tool/task correlation, sounds like a fortune cookie—completely useless for a manager]

        Example 2:
        "**Team Performance Summary**
        - Alignment was 78%.
        - People used computers today.
        - Tasks were completed.

        **Key Strengths**
        - Sarah did well.

        **Areas for Improvement**
        - Alex needs to focus more and stop being distracted.
        - Jordan is underperforming and should work harder.

        **Recommendations for Tomorrow**
        - Everyone should be more productive.
        - Fix the problems mentioned above."

        [WHY IT'S BAD: Fails to contextualize numbers, no evidence for claims, overly harsh/judgmental tone 
        without constructive framing, vague recommendations, doesn't help manager understand root causes or take action]

        OUTPUT REQUIREMENTS:

        **Structure**: Use this exact markdown format:

        **Team Performance Summary**

        - [Bullet point 1]
        - [Bullet point 2]
        - [Bullet point 3]

        **Key Strengths**

        - [Bullet point 1]
        - [Bullet point 2]
        - [Bullet point 3]

        **Areas for Improvement**

        - [Bullet point 1]
        - [Bullet point 2]
        - [Bullet point 3]

        **Recommendations for Tomorrow**

        - [Bullet point 1]
        - [Bullet point 2]
        - [Bullet point 3]

        **Tone & Style**:
        - Professional but conversational—write like you're briefing a busy manager over coffee
        - Data-driven but human—numbers need context and interpretation
        - Constructive not critical—frame challenges as opportunities
        - Specific not generic—cite tools, tasks, names, percentages, patterns

        **Content Rules**:
        - Every insight must reference specific data (percentages, tool names, task titles, team member names)
        - Mention names for top performers (celebrate wins) and those who might need support (offer help)
        - Each recommendation should be actionable within 24 hours
        - Connect today's patterns to tomorrow's actions
        - Avoid: vague praise, blame language, corporate jargon, anything a manager can't act on

        **Length**: 3-4 bullet points per section (12-16 total). Each bullet should be 1-2 sentences max.

        Remember: This manager has 10 minutes to read this before their next meeting. Make every word count.
        """
        
        generated_summary = ""
        try:
            response = await genai_client.aio.models.generate_content(
                model=DEFAULT_MODEL,
                contents=user_content,
                config=types.GenerateContentConfig(
                    temperature=0.5,
                )
            )
            generated_summary = response.text.strip()
        except Exception as ai_err:
            logging.error(f"Gemini workroom summary failed: {ai_err}")
            generated_summary = fallback_summary
        
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

async def delete_cloudinary_object(public_id: str) -> bool:
    """
    Deletes an object from Cloudinary.
    """
    try:
        result = cloudinary.uploader.destroy(
            public_id,
            resource_type="image"
        )
        
        if result.get('result') == 'ok':
            logging.info(f"Deleted object: {public_id} from Cloudinary")
            return True
        else:
            logging.warning(f"Cloudinary deletion returned: {result}")
            return False
            
    except Exception as e:
        logging.error(f"Error deleting object from Cloudinary: {e}")
        return False
    
async def delete_cloudinary_objects(public_ids: list[str]) -> bool:
    """
    Deletes multiple objects from Cloudinary.
    """
    try:
        if not public_ids:
            return True
            
        # Cloudinary can delete multiple resources at once
        result = cloudinary.api.delete_resources(
            public_ids,
            resource_type="image"
        )
        
        # Check if all deletions were successful
        deleted = result.get('deleted', {})
        failed = result.get('not_found', []) + result.get('partial', [])
        
        if failed:
            logging.warning(f"Some Cloudinary objects failed to delete: {failed}")
        
        logging.info(f"Deleted {len(deleted)} objects from Cloudinary")
        return len(failed) == 0
        
    except Exception as e:
        logging.error(f"Error deleting objects from Cloudinary: {e}")
        return False

async def delete_user_session_files(user_id: UUID, session_id: UUID) -> bool:
    """
    Deletes all files (screenshots and analysis results) for a user session from Cloudinary.
    """
    try:
        folder_path = f"hudddleBackend/user_{user_id}/session_{session_id}"
        
        # Search for all resources in the session folder
        result = cloudinary.Search()\
            .expression(f"folder:{folder_path}")\
            .max_results(500)\
            .execute()
        
        if not result.get('resources'):
            logging.info(f"No files found for user {user_id}, session {session_id}")
            return True
        
        # Extract public_ids
        public_ids = [resource['public_id'] for resource in result['resources']]
        
        # Delete all resources
        return await delete_cloudinary_objects(public_ids)
        
    except Exception as e:
        logging.error(f"Error deleting user session files: {e}")
        return False

async def get_user_session_screenshots(user_id: UUID, session_id: UUID) -> tuple[list[str], list[str]]:
    """
    Get all screenshot URLs and public_ids for a user session from Cloudinary.
    Returns tuple of (image_urls, public_ids)
    """
    try:
        folder_path = f"hudddleBackend/user_{user_id}/session_{session_id}"
        
        # Search for screenshot images only (not analysis files)
        # Use Admin API for immediate consistency
        result = cloudinary.api.resources(
            type="upload",
            prefix=folder_path,
            resource_type="image", 
            max_results=500
        )
        
        logging.info(f"Screenshots fetch for {folder_path} found {len(result.get('resources', []))} images")
        
        image_urls = []
        public_ids = []
        
        for resource in result.get('resources', []):
            image_urls.append(resource['secure_url'])
            public_ids.append(resource['public_id'])
        
        return image_urls, public_ids
        
    except Exception as e:
        logging.error(f"Error listing user session screenshots: {e}")
        return [], []

