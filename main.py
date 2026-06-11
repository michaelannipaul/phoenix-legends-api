"""
Phoenix Legends — FastAPI Backend v3
No Pydantic models — uses plain dicts for Python 3.14 compatibility
"""

from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
import firebase_admin
from firebase_admin import credentials, db, auth
import os, json, uuid
from datetime import datetime

app = FastAPI(title="Phoenix Legends API", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Firebase init ─────────────────────────────────────────────
FIREBASE_CREDS = os.environ.get("FIREBASE_CREDENTIALS")
FIREBASE_DB_URL = "https://phoenix-legends-default-rtdb.firebaseio.com"

if FIREBASE_CREDS:
    cred_dict = json.loads(FIREBASE_CREDS)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
else:
    print("WARNING: FIREBASE_CREDENTIALS not set.")

# ── Auth helpers ──────────────────────────────────────────────
async def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ")[1]
    try:
        return auth.verify_id_token(token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

async def get_admin_user(user=Depends(get_current_user)):
    uid = user["uid"]
    snap = db.reference(f"pl/users/{uid}").get()
    if not snap or snap.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return {**snap, "uid": uid}

# ── Health ────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "Phoenix Legends API running", "version": "3.0"}

@app.get("/health")
def health():
    return {"status": "ok"}

# ── Tournaments ───────────────────────────────────────────────
@app.get("/tournaments")
def get_tournaments():
    data = db.reference("pl/tournaments").get() or {}
    result = [{**v, "id": k} for k, v in data.items()]
    result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return result

@app.get("/tournaments/active")
def get_active_tournament():
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
async def create_tournament(request: Request, admin=Depends(get_admin_user)):
    body = await request.json()
    name = body.get("name", "").strip()
    teams = body.get("teams", [])
    matchdays = int(body.get("matchdays", 10))
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
    if len(teams) < 2:
        raise HTTPException(status_code=400, detail="At least 2 teams required")

    tid = str(uuid.uuid4())[:8]
    now = datetime.utcnow().isoformat()
    scores = {str(i): {} for i in range(matchdays)}
    standings = [{"name": t, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0} for t in teams]

    data = {
        "name": name,
        "format": body.get("format", "league"),
        "teams": teams,
        "matchdays": matchdays,
        "status": body.get("status", "upcoming"),
        "start_date": body.get("start_date", now[:10]),
        "end_date": body.get("end_date", ""),
        "description": body.get("description", ""),
        "created_at": now,
        "created_by": admin["uid"],
        "scores": scores,
        "standings": standings,
        "winner": None
    }
    db.reference(f"pl/tournaments/{tid}").set(data)
    return {"id": tid, **data}

@app.put("/tournaments/{tournament_id}/status")
async def update_tournament_status(tournament_id: str, request: Request, admin=Depends(get_admin_user)):
    body = await request.json()
    status = body.get("status")
    winner = body.get("winner")
    update = {}
    if status:
        update["status"] = status
    if winner:
        update["winner"] = winner
    if status == "completed" and winner:
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
    db.reference(f"pl/tournaments/{tournament_id}").delete()
    return {"success": True}

# ── Scores ────────────────────────────────────────────────────
@app.get("/tournaments/{tournament_id}/scores")
def get_scores(tournament_id: str):
    return db.reference(f"pl/tournaments/{tournament_id}/scores").get() or {}

@app.post("/scores")
async def save_match_result(request: Request, admin=Depends(get_admin_user)):
    body = await request.json()
    tid = body["tournament_id"]
    md = str(body["matchday"])
    key = f"{body['home_team']}_vs_{body['away_team']}".replace(" ", "_")
    db.reference(f"pl/tournaments/{tid}/scores/{md}/{key}").set({
        "h": body["home_team"],
        "hs": body["home_score"],
        "a": body["away_team"],
        "as_": body["away_score"],
        "status": body.get("status", "FT")
    })
    return {"success": True}

# ── Standings ─────────────────────────────────────────────────
@app.get("/tournaments/{tournament_id}/standings")
def get_standings(tournament_id: str):
    data = db.reference(f"pl/tournaments/{tournament_id}/standings").get() or []
    if isinstance(data, dict):
        data = list(data.values())
    data.sort(key=lambda x: (-(x.get("w",0)*3+x.get("d",0)), -(x.get("gf",0)-x.get("ga",0))))
    return data

@app.put("/standings")
async def update_standings(request: Request, admin=Depends(get_admin_user)):
    body = await request.json()
    tid = body["tournament_id"]
    standings = body["standings"]
    standings.sort(key=lambda x: (-(x.get("w",0)*3+x.get("d",0)), -(x.get("gf",0)-x.get("ga",0))))
    db.reference(f"pl/tournaments/{tid}/standings").set(standings)
    return {"success": True}

# ── History ───────────────────────────────────────────────────
@app.get("/history")
def get_history():
    data = db.reference("pl/history").get() or {}
    result = [{**v, "id": k} for k, v in data.items()]
    result.sort(key=lambda x: x.get("completed_at", ""), reverse=True)
    return result

# ── Announcements ─────────────────────────────────────────────
@app.get("/announcement")
def get_announcement():
    return db.reference("pl/announcement").get()

@app.post("/announcement")
async def post_announcement(request: Request, admin=Depends(get_admin_user)):
    body = await request.json()
    db.reference("pl/announcement").set({"text": body["text"], "type": body.get("type","info")})
    return {"success": True}

@app.delete("/announcement")
def clear_announcement(admin=Depends(get_admin_user)):
    db.reference("pl/announcement").delete()
    return {"success": True}

# ── Members ───────────────────────────────────────────────────
@app.get("/members")
def get_members(admin=Depends(get_admin_user)):
    data = db.reference("pl/users").get() or {}
    return [{**v, "uid": k} for k, v in data.items()]

@app.put("/members/{uid}")
async def update_member(uid: str, request: Request, admin=Depends(get_admin_user)):
    body = await request.json()
    db.reference(f"pl/users/{uid}").update(body)
    return {"success": True}

@app.delete("/members/{uid}")
def delete_member(uid: str, admin=Depends(get_admin_user)):
    try:
        auth.delete_user(uid)
    except:
        pass
    db.reference(f"pl/users/{uid}").delete()
    return {"success": True}
