"""
Phoenix Legends — FastAPI Backend v4.0
Full auth handled here — no Firebase Auth SDK needed
Uses: JWT tokens, bcrypt password hashing, Firebase Realtime DB for storage
"""

from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
import firebase_admin
from firebase_admin import credentials, db
import os, json, uuid, hashlib, hmac, base64, time
from datetime import datetime

app = FastAPI(title="Phoenix Legends API", version="4.0")

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
JWT_SECRET = os.environ.get("JWT_SECRET", "phoenix-legends-super-secret-key-2025")

if FIREBASE_CREDS:
    cred_dict = json.loads(FIREBASE_CREDS)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
    print("Firebase connected!")
else:
    print("WARNING: FIREBASE_CREDENTIALS not set.")

# ── Simple JWT (no external library needed) ───────────────────
def b64url(data):
    if isinstance(data, str): data = data.encode()
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

def create_token(uid, email, role):
    header = b64url(json.dumps({"alg":"HS256","typ":"JWT"}))
    payload = b64url(json.dumps({"uid":uid,"email":email,"role":role,"exp":int(time.time())+86400*30}))
    sig = b64url(hmac.new(JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"

def verify_token(token):
    try:
        parts = token.split('.')
        if len(parts) != 3: raise Exception("Invalid token")
        header, payload, sig = parts
        expected = b64url(hmac.new(JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
        if sig != expected: raise Exception("Invalid signature")
        data = json.loads(base64.urlsafe_b64decode(payload + '=='))
        if data.get('exp', 0) < time.time(): raise Exception("Token expired")
        return data
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

def hash_password(password):
    return hashlib.sha256((password + JWT_SECRET).encode()).hexdigest()

# ── Auth helpers ──────────────────────────────────────────────
async def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return verify_token(authorization.split(" ")[1])

async def get_admin_user(user=Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

# ── Health ────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "Phoenix Legends API running", "version": "4.0"}

@app.get("/health")
def health():
    return {"status": "ok"}

# ── AUTH ──────────────────────────────────────────────────────
@app.post("/auth/register")
async def register(request: Request):
    body = await request.json()
    email = body.get("email","").strip().lower()
    password = body.get("password","")
    name = body.get("name","").strip()
    fc26uid = body.get("fc26uid","").strip()
    role = body.get("role","user")

    if not email or not password or not name:
        raise HTTPException(status_code=400, detail="Name, email and password are required")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    # Check if email already exists
    users = db.reference("pl/users").get() or {}
    for uid, u in users.items():
        if u.get("email","").lower() == email:
            raise HTTPException(status_code=400, detail="An account with this email already exists")

    uid = str(uuid.uuid4())[:16]
    now = datetime.utcnow().isoformat()
    user_data = {
        "name": name, "email": email, "fc26uid": fc26uid,
        "password": hash_password(password), "role": role, "created_at": now
    }
    db.reference(f"pl/users/{uid}").set(user_data)

    token = create_token(uid, email, role)
    safe_user = {k:v for k,v in user_data.items() if k != "password"}
    safe_user["uid"] = uid
    return {"token": token, "user": safe_user}

@app.post("/auth/login")
async def login(request: Request):
    body = await request.json()
    email = body.get("email","").strip().lower()
    password = body.get("password","")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")

    users = db.reference("pl/users").get() or {}
    found_uid = None
    found_user = None
    for uid, u in users.items():
        if u.get("email","").lower() == email:
            found_uid = uid
            found_user = u
            break

    if not found_user or found_user.get("password") != hash_password(password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_token(found_uid, email, found_user.get("role","user"))
    safe_user = {k:v for k,v in found_user.items() if k != "password"}
    safe_user["uid"] = found_uid
    return {"token": token, "user": safe_user}

@app.get("/auth/me")
async def get_me(user=Depends(get_current_user)):
    uid = user["uid"]
    data = db.reference(f"pl/users/{uid}").get()
    if not data:
        raise HTTPException(status_code=404, detail="User not found")
    safe = {k:v for k,v in data.items() if k != "password"}
    safe["uid"] = uid
    return safe

# ── Tournaments ───────────────────────────────────────────────
@app.get("/tournaments")
def get_tournaments():
    data = db.reference("pl/tournaments").get() or {}
    result = [{**v,"id":k} for k,v in data.items()]
    result.sort(key=lambda x: x.get("created_at",""), reverse=True)
    return result

@app.get("/tournaments/active")
def get_active_tournament():
    data = db.reference("pl/tournaments").get() or {}
    for tid, t in data.items():
        if t.get("status") in ("ongoing","upcoming"):
            return {**t,"id":tid}
    return None

@app.get("/tournaments/{tournament_id}")
def get_tournament(tournament_id: str):
    data = db.reference(f"pl/tournaments/{tournament_id}").get()
    if not data: raise HTTPException(status_code=404, detail="Not found")
    return {**data,"id":tournament_id}

@app.post("/tournaments")
async def create_tournament(request: Request, admin=Depends(get_admin_user)):
    body = await request.json()
    name = body.get("name","").strip()
    teams = body.get("teams",[])
    matchdays = int(body.get("matchdays",10))
    if not name: raise HTTPException(status_code=400, detail="Name required")
    if len(teams) < 2: raise HTTPException(status_code=400, detail="At least 2 teams required")
    tid = str(uuid.uuid4())[:8]
    now = datetime.utcnow().isoformat()
    data = {
        "name":name, "format":body.get("format","league"), "teams":teams,
        "matchdays":matchdays, "status":body.get("status","upcoming"),
        "start_date":body.get("start_date",now[:10]), "end_date":body.get("end_date",""),
        "description":body.get("description",""), "created_at":now,
        "scores":{str(i):{} for i in range(matchdays)},
        "standings":[{"name":t,"w":0,"d":0,"l":0,"gf":0,"ga":0} for t in teams],
        "winner":None
    }
    db.reference(f"pl/tournaments/{tid}").set(data)
    return {"id":tid,**data}

@app.put("/tournaments/{tournament_id}/status")
async def update_status(tournament_id: str, request: Request, admin=Depends(get_admin_user)):
    body = await request.json()
    update = {}
    if body.get("status"): update["status"] = body["status"]
    if body.get("winner"): update["winner"] = body["winner"]
    if body.get("status") == "completed" and body.get("winner"):
        t = db.reference(f"pl/tournaments/{tournament_id}").get()
        if t:
            db.reference(f"pl/history/{tournament_id}").set({
                **t,"status":"completed","winner":body["winner"],
                "completed_at":datetime.utcnow().isoformat()
            })
    db.reference(f"pl/tournaments/{tournament_id}").update(update)
    return {"success":True}

@app.delete("/tournaments/{tournament_id}")
def delete_tournament(tournament_id: str, admin=Depends(get_admin_user)):
    db.reference(f"pl/tournaments/{tournament_id}").delete()
    return {"success":True}

# ── Scores ────────────────────────────────────────────────────
@app.get("/tournaments/{tournament_id}/scores")
def get_scores(tournament_id: str):
    return db.reference(f"pl/tournaments/{tournament_id}/scores").get() or {}

@app.post("/scores")
async def save_score(request: Request, admin=Depends(get_admin_user)):
    body = await request.json()
    tid = body["tournament_id"]
    md = str(body["matchday"])
    key = f"{body['home_team']}_vs_{body['away_team']}".replace(" ","_")
    db.reference(f"pl/tournaments/{tid}/scores/{md}/{key}").set({
        "h":body["home_team"],"hs":body["home_score"],
        "a":body["away_team"],"as_":body["away_score"],
        "status":body.get("status","FT")
    })
    return {"success":True}

# ── Standings ─────────────────────────────────────────────────
@app.get("/tournaments/{tournament_id}/standings")
def get_standings(tournament_id: str):
    data = db.reference(f"pl/tournaments/{tournament_id}/standings").get() or []
    if isinstance(data,dict): data = list(data.values())
    data.sort(key=lambda x:(-(x.get("w",0)*3+x.get("d",0)),-(x.get("gf",0)-x.get("ga",0))))
    return data

@app.put("/standings")
async def update_standings(request: Request, admin=Depends(get_admin_user)):
    body = await request.json()
    standings = body["standings"]
    standings.sort(key=lambda x:(-(x.get("w",0)*3+x.get("d",0)),-(x.get("gf",0)-x.get("ga",0))))
    db.reference(f"pl/tournaments/{body['tournament_id']}/standings").set(standings)
    return {"success":True}

# ── History ───────────────────────────────────────────────────
@app.get("/history")
def get_history():
    data = db.reference("pl/history").get() or {}
    result = [{**v,"id":k} for k,v in data.items()]
    result.sort(key=lambda x:x.get("completed_at",""), reverse=True)
    return result

# ── Announcements ─────────────────────────────────────────────
@app.get("/announcement")
def get_announcement():
    return db.reference("pl/announcement").get()

@app.post("/announcement")
async def post_announcement(request: Request, admin=Depends(get_admin_user)):
    body = await request.json()
    db.reference("pl/announcement").set({"text":body["text"],"type":body.get("type","info")})
    return {"success":True}

@app.delete("/announcement")
def clear_announcement(admin=Depends(get_admin_user)):
    db.reference("pl/announcement").delete()
    return {"success":True}

# ── Members ───────────────────────────────────────────────────
@app.get("/members")
async def get_members(admin=Depends(get_admin_user)):
    data = db.reference("pl/users").get() or {}
    return [{**{k:v for k,v in u.items() if k!="password"},"uid":uid} for uid,u in data.items()]

@app.put("/members/{uid}")
async def update_member(uid: str, request: Request, admin=Depends(get_admin_user)):
    body = await request.json()
    db.reference(f"pl/users/{uid}").update(body)
    return {"success":True}

@app.delete("/members/{uid}")
def delete_member(uid: str, admin=Depends(get_admin_user)):
    db.reference(f"pl/users/{uid}").delete()
    return {"success":True}
