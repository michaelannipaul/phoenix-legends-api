"""
Phoenix Legends — FastAPI Backend
Handles: Auth, Tournaments, Scores, Standings, Announcements, Members
Database: Firebase Realtime Database
"""

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import firebase_admin
from firebase_admin import credentials, db, auth
import os, json, uuid
from datetime import datetime

app = FastAPI(title="Phoenix Legends API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Firebase init ─────────────────────────────────────────────
# On Render, set FIREBASE_CREDENTIALS env var with your service account JSON string
FIREBASE_CREDS = os.environ.get("FIREBASE_CREDENTIALS")
FIREBASE_DB_URL = "https://phoenix-legends-default-rtdb.firebaseio.com"

if FIREBASE_CREDS:
    cred_dict = json.loads(FIREBASE_CREDS)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
else:
    print("WARNING: FIREBASE_CREDENTIALS not set. Running without Firebase.")

# ── Auth helper ───────────────────────────────────────────────
async def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ")[1]
    try:
        decoded = auth.verify_id_token(token)
        return decoded
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

async def get_admin_user(user=Depends(get_current_user)):
    uid = user["uid"]
    snap = db.reference(f"pl/users/{uid}").get()
    if not snap or snap.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return {**snap, "uid": uid}

# ── Models ────────────────────────────────────────────────────
class Tournament(BaseModel):
    name: str
    format: str  # league, knockout, group+knockout
    teams: List[str]
    matchdays: int
    status: str = "upcoming"  # upcoming, ongoing, completed
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    description: Optional[str] = None

class MatchResult(BaseModel):
    tournament_id: str
    matchday: int
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    status: str = "FT"  # FT, TBD, LIVE

class StandingUpdate(BaseModel):
    tournament_id: str
    standings: List[Dict[str, Any]]

class Announcement(BaseModel):
    text: str
    type: str = "info"  # info, warning, success

class UserUpdate(BaseModel):
    role: Optional[str] = None
    name: Optional[str] = None
    fc26uid: Optional[str] = None

class FixtureSet(BaseModel):
    tournament_id: str
    matchday: int
    matches: List[Dict[str, str]]

# ── HEALTH ───────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "Phoenix Legends API is running", "version": "2.0"}

@app.get("/health")
def health():
    return {"status": "ok"}

# ── TOURNAMENTS ───────────────────────────────────────────────
@app.get("/tournaments")
def get_tournaments():
    """Get all tournaments"""
    data = db.reference("pl/tournaments").get() or {}
    result = []
    for tid, t in data.items():
        result.append({**t, "id": tid})
    result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return result

@app.get("/tournaments/active")
def get_active_tournament():
    """Get currently active/ongoing tournament"""
    data = db.reference("pl/tournaments").get() or {}
    for tid, t in data.items():
        if t.get("status") in ("ongoing", "upcoming"):
            return {**t, "id": tid}
    return None

@app.get("/tournaments/{tournament_id}")
def get_tournament(tournament_id: str):
    data = db.reference(f"pl/tournaments/{tournament_id}").get()
    if not data:
        raise HTTPException(status_code=404, detail="Tournament not found")
    return {**data, "id": tournament_id}

@app.post("/tournaments")
def create_tournament(t: Tournament, admin=Depends(get_admin_user)):
    """Admin: Create a new tournament"""
    tid = str(uuid.uuid4())[:8]
    now = datetime.utcnow().isoformat()
    # Build empty matchday structure
    scores = {}
    for i in range(t.matchdays):
        scores[str(i)] = []
    # Build standings from teams
    standings = [{"name": team, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0} for team in t.teams]

    data = {
        "name": t.name,
        "format": t.format,
        "teams": t.teams,
        "matchdays": t.matchdays,
        "status": t.status,
        "start_date": t.start_date or now[:10],
        "end_date": t.end_date or "",
        "description": t.description or "",
        "created_at": now,
        "created_by": admin["uid"],
        "scores": scores,
        "standings": standings,
        "winner": None
    }
    db.reference(f"pl/tournaments/{tid}").set(data)
    return {"id": tid, **data}

@app.put("/tournaments/{tournament_id}/status")
def update_tournament_status(tournament_id: str, body: dict, admin=Depends(get_admin_user)):
    """Admin: Update tournament status (upcoming/ongoing/completed) and set winner"""
    allowed = ["upcoming", "ongoing", "completed"]
    status = body.get("status")
    winner = body.get("winner")
    if status and status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid status")
    update = {}
    if status:
        update["status"] = status
    if winner:
        update["winner"] = winner
    if status == "completed" and winner:
        # Archive to history
        t = db.reference(f"pl/tournaments/{tournament_id}").get()
        if t:
            db.reference(f"pl/history/{tournament_id}").set({
                **t, "status": "completed", "winner": winner,
                "completed_at": datetime.utcnow().isoformat()
            })
    db.reference(f"pl/tournaments/{tournament_id}").update(update)
    return {"success": True}

@app.delete("/tournaments/{tournament_id}")
def delete_tournament(tournament_id: str, admin=Depends(get_admin_user)):
    """Admin: Delete a tournament"""
    db.reference(f"pl/tournaments/{tournament_id}").delete()
    return {"success": True}

# ── SCORES & FIXTURES ─────────────────────────────────────────
@app.get("/tournaments/{tournament_id}/scores")
def get_scores(tournament_id: str):
    data = db.reference(f"pl/tournaments/{tournament_id}/scores").get() or {}
    return data

@app.post("/scores")
def save_match_result(result: MatchResult, admin=Depends(get_admin_user)):
    """Admin: Save a match result"""
    tid = result.tournament_id
    md = str(result.matchday)
    # Get existing matches for this matchday
    matches = db.reference(f"pl/tournaments/{tid}/scores/{md}").get() or {}
    if isinstance(matches, list):
        matches = {str(i): m for i, m in enumerate(matches)}
    # Find existing match or add new one
    match_key = f"{result.home_team}_vs_{result.away_team}".replace(" ", "_")
    matches[match_key] = {
        "h": result.home_team,
        "hs": result.home_score,
        "a": result.away_team,
        "as_": result.away_score,
        "status": result.status
    }
    db.reference(f"pl/tournaments/{tid}/scores/{md}").set(matches)
    return {"success": True}

@app.post("/fixtures")
def save_fixtures(fixture: FixtureSet, admin=Depends(get_admin_user)):
    """Admin: Save fixtures for a matchday"""
    tid = fixture.tournament_id
    md = str(fixture.matchday)
    matches = {}
    for i, m in enumerate(fixture.matches):
        matches[str(i)] = {"h": m["home"], "hs": 0, "a": m["away"], "as_": 0, "status": "TBD"}
    db.reference(f"pl/tournaments/{tid}/scores/{md}").set(matches)
    return {"success": True}

# ── STANDINGS ─────────────────────────────────────────────────
@app.get("/tournaments/{tournament_id}/standings")
def get_standings(tournament_id: str):
    data = db.reference(f"pl/tournaments/{tournament_id}/standings").get() or []
    if isinstance(data, dict):
        data = list(data.values())
    data.sort(key=lambda x: (-(x.get("w",0)*3 + x.get("d",0)), -(x.get("gf",0) - x.get("ga",0))))
    return data

@app.put("/standings")
def update_standings(update: StandingUpdate, admin=Depends(get_admin_user)):
    """Admin: Update standings for a tournament"""
    tid = update.tournament_id
    standings = update.standings
    standings.sort(key=lambda x: (-(x.get("w",0)*3 + x.get("d",0)), -(x.get("gf",0) - x.get("ga",0))))
    db.reference(f"pl/tournaments/{tid}/standings").set(standings)
    return {"success": True}

# ── HISTORY ───────────────────────────────────────────────────
@app.get("/history")
def get_history():
    """Get all completed tournaments"""
    data = db.reference("pl/history").get() or {}
    result = []
    for tid, t in data.items():
        result.append({**t, "id": tid})
    result.sort(key=lambda x: x.get("completed_at", ""), reverse=True)
    return result

# ── ANNOUNCEMENTS ─────────────────────────────────────────────
@app.get("/announcement")
def get_announcement():
    return db.reference("pl/announcement").get()

@app.post("/announcement")
def post_announcement(ann: Announcement, admin=Depends(get_admin_user)):
    """Admin: Post a site-wide announcement"""
    db.reference("pl/announcement").set({"text": ann.text, "type": ann.type})
    return {"success": True}

@app.delete("/announcement")
def clear_announcement(admin=Depends(get_admin_user)):
    """Admin: Clear announcement"""
    db.reference("pl/announcement").delete()
    return {"success": True}

# ── MEMBERS ───────────────────────────────────────────────────
@app.get("/members")
def get_members(admin=Depends(get_admin_user)):
    """Admin: Get all members"""
    data = db.reference("pl/users").get() or {}
    result = []
    for uid, u in data.items():
        result.append({**u, "uid": uid})
    return result

@app.put("/members/{uid}")
def update_member(uid: str, update: UserUpdate, admin=Depends(get_admin_user)):
    """Admin: Update a member's details/role"""
    changes = {k: v for k, v in update.dict().items() if v is not None}
    if not changes:
        raise HTTPException(status_code=400, detail="No changes provided")
    db.reference(f"pl/users/{uid}").update(changes)
    return {"success": True}

@app.delete("/members/{uid}")
def delete_member(uid: str, admin=Depends(get_admin_user)):
    """Admin: Remove a member"""
    try:
        auth.delete_user(uid)
    except:
        pass
    db.reference(f"pl/users/{uid}").delete()
    return {"success": True}

@app.get("/members/me")
def get_me(user=Depends(get_current_user)):
    """Get current user's profile"""
    data = db.reference(f"pl/users/{user['uid']}").get()
    if not data:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {**data, "uid": user["uid"]}
