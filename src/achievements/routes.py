# from fastapi import FastAPI, HTTPException, Depends
# from sqlmodel import Session, select
# from typing import List
# from datetime import datetime
# from uuid import UUID, uuid4
# from models import Task, TaskStatus, Workroom, DailyChallenge, Team, Leaderboard, Achievement, create_datetime_column
# from database import engine, get_db

# app = FastAPI()


# # Achievement Endpoints

# @app.get("/api/achievements", response_model=List[Achievement])
# def get_achievements(db: Session = Depends(get_db)):
#     achievements = db.exec(select(Achievement)).all()
#     return achievements

# @app.get("/api/achievements/{achievement_id}", response_model=Achievement)
# def get_achievement(achievement_id: UUID, db: Session = Depends(get_db)):
#     achievement = db.get(Achievement, achievement_id)
#     if not achievement:
#         raise HTTPException(status_code=404, detail="Achievement not found")
#     return achievement

# @app.post("/api/achievements", response_model=Achievement)
# def create_achievement(achievement: Achievement, db: Session = Depends(get_db)):
#     db.add(achievement)
#     db.commit()
#     db.refresh(achievement)
#     return achievement

# @app.put("/api/achievements/{achievement_id}", response_model=Achievement)
# def update_achievement(achievement_id: UUID, achievement_update: Achievement, db: Session = Depends(get_db)):
#     achievement = db.get(Achievement, achievement_id)
#     if not achievement:
#         raise HTTPException(status_code=404, detail="Achievement not found")
#     for key, value in achievement_update.dict().items():
#         setattr(achievement, key, value)
#     db.commit()
#     db.refresh(achievement)
#     return achievement

# @app.delete("/api/achievements/{achievement_id}")
# def delete_achievement(achievement_id: UUID, db: Session = Depends(get_db)):
#     achievement = db.get(Achievement, achievement_id)
#     if not achievement:
#         raise HTTPException(status_code=404, detail="Achievement not found")
#     db.delete(achievement)
#     db.commit()
#     return {"message": "Achievement deleted successfully"}

