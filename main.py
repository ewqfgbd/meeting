# main.py - 修正版本 (使用 Sheets 作為短期 Token 儲存)

from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import jwt 
import bcrypt
import datetime
import time
import uuid

# --- 引入 sheets_manager (只引入類和定義，不立即實例化) ---
from sheets_manager import SheetsManager, WORKSHEET_DEFINITIONS 


# --- 實例化 SheetsManager (在程式啟動時只執行一次) ---
sheets_manager_instance = None
try:
    # 這裡會使用 sheets_manager.py 中定義的 SERVICE_ACCOUNT_FILE
    sheets_manager_instance = SheetsManager()
except Exception as e:
    print(f"致命錯誤：無法初始化 SheetsManager 實例。錯誤: {e}")
    # 這裡 sheets_manager_instance 仍然為 None

# --- 簡易配置 ---
# 這些值應從環境變數或 config.py 載入，這裡為確保運行使用硬編碼
JWT_SECRET_KEY = "your_strong_and_secret_jwt_key_32bytes" 
JWT_ALGORITHM = "HS256"
INIT_MASTER_KEY = "your_super_secret_init_key" 
QR_CODE_EXP_SECONDS = 15 # QR Code 憑證有效時間
ADMIN_SESSION_EXP_MINUTES = 60 * 24 
PARTICIPANT_SESSION_EXP_DAYS = 7 # 學員 Token 有效期 7 天

# --- ⚠️ 移除 全局記憶體快取：已改用 Google Sheets ---
# QR_CODE_CACHE = {} 
# --------------------------------------------------


# --- Pydantic 模型定義 ---
class AdminLoginRequest(BaseModel):
    username: str
    password: str

class InitializationRequest(BaseModel):
    secret_key: str
    clear_data: bool = False

class TokenRequest(BaseModel):
    participant_id: str
    agenda_item_id: str
    device_id: str

class CheckInRequest(BaseModel):
    qr_code_token: str
    agenda_item_id: str
    scanner_device_id: str

class ParticipantSignupRequest(BaseModel):
    name: str
    email: str
    phone_number: str
    organization: Optional[str] = None
    password: str

class ParticipantLoginRequest(BaseModel):
    email: str
    password: str


# --- FastAPI 應用程式實例化 ---
app = FastAPI(title="會議報到系統 API 後端", version="1.0.0")


# --- 新增: CORS 配置 ---
origins = ["*"] 
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,        # 允許的來源列表
    allow_credentials=True,       # 允許發送 Cookie/授權標頭
    allow_methods=["*"],          # 允許所有 HTTP 方法
    allow_headers=["*"],          # 允許所有 HTTP 請求標頭
)
# -------------------------


# --- 輔助函數區 ---
def get_sheets_manager():
    """依賴注入函數：確保 sheets_manager_instance 存在且已連線"""
    global sheets_manager_instance
    if sheets_manager_instance is None or not sheets_manager_instance.is_connected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail="服務不可用：Google Sheets 後端連線失敗或初始化錯誤"
        )
    return sheets_manager_instance
    
def create_jwt_token(data: dict, expires_delta: datetime.timedelta):
    """創建 JWT Token，包含到期時間 (用於 Session Token)"""
    to_encode = data.copy()
    to_encode.update({"exp": int(time.time() + expires_delta.total_seconds())})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded_jwt

# 解碼 JWT Token 函數可移除，因為 QR Token 不再使用 JWT，但保留用於 Session Token 驗證 (如果需要)
def decode_jwt_token(token: str):
    """解碼 JWT Token 並處理過期或無效錯誤"""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return {"error": "Token expired"}
    except jwt.InvalidTokenError:
        return {"error": "Invalid token"}

def generate_new_participant_id(sheets_manager: SheetsManager):
    """查找 Participants 表中最大的 ID (Pxxx) 並生成新的 ID"""
    participants = sheets_manager.get_all_records('Participants')
    if not participants:
        return "P001"
    
    id_numbers = [int(p['id'][1:]) for p in participants if p.get('id', '').startswith('P')]
    
    if not id_numbers:
        return "P001"
        
    max_id_num = max(id_numbers)
    new_id_num = max_id_num + 1
    
    return f"P{new_id_num:03d}"

# --- API 路由區 ---

# 1. 初始化資料庫 API
@app.post("/api/v1/admin/initialize-database", tags=["Admin"], summary="Initialize Db")
def initialize_db(request: InitializationRequest, sheets_manager: SheetsManager = Depends(get_sheets_manager)):
    """
    初始化 Google Sheets 資料庫。只有持有正確密鑰才能執行。
    """
    if request.secret_key != INIT_MASTER_KEY:
        raise HTTPException(status_code=403, detail="初始化密鑰錯誤")
    
    try:
        initialized_sheets = sheets_manager.initialize_system(request.clear_data)

        return {
            "status": "success",
            "sheets_initialized": initialized_sheets,
            "message": "系統初始化成功，請勿再次運行。"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"初始化失敗: {e}")


# 2. 管理員登入 API (App A 登入)
@app.post("/api/v1/auth/admin-login", tags=["Auth"], summary="Admin Login")
def admin_login(request: AdminLoginRequest, sheets_manager: SheetsManager = Depends(get_sheets_manager)):
    """管理員登入，成功後返回 Session Token。"""
    admin_data = sheets_manager.find_admin_by_username(request.username)

    if not admin_data:
        raise HTTPException(status_code=401, detail="用戶名或密碼錯誤")
    
    password_hash = admin_data.get('password_hash')
    if not password_hash:
        raise HTTPException(status_code=500, detail="服務器配置錯誤：缺少密碼雜湊")
        
    password_hash = password_hash.encode('utf-8')
    
    if not bcrypt.checkpw(request.password.encode('utf-8'), password_hash):
        raise HTTPException(status_code=401, detail="用戶名或密碼錯誤")

    # 生成 JWT Session Token
    token_payload = {
        "sub": admin_data.get('id'),
        "user_role": admin_data.get('role'),
        "token_type": "session"
    }
    session_token = create_jwt_token(
        token_payload,
        datetime.timedelta(minutes=ADMIN_SESSION_EXP_MINUTES)
    )

    return {
        "status": "success",
        "admin_name": admin_data.get('username'),
        "role": admin_data.get('role'),
        "session_token": session_token
    }


# 3. Token 生成 API (App B 請求 QR Code Token) - 💥 修正使用 Google Sheets
@app.post("/api/v1/attendance/token", tags=["Attendance"], summary="Generate Qr Token")
def generate_qr_token(request: TokenRequest, sheets_manager: SheetsManager = Depends(get_sheets_manager)):
    """
    參與者App (App B) 請求生成用於報到的 QR Code Token。
    現在將 Token 儲存到 Google Sheets 以支援擴展。
    """
    
    # 1. 驗證參與者 ID
    participant = sheets_manager.find_record_by_id('Participants', request.participant_id, id_column=1)
    if not participant:
        raise HTTPException(status_code=404, detail="參與者 ID 無效")
    
    # 2. 驗證議程 ID
    agenda_item = sheets_manager.find_record_by_id('Agenda_Items', request.agenda_item_id, id_column=1)
    if not agenda_item:
        raise HTTPException(status_code=404, detail="議程 ID 無效")

    # 3. 🆕 生成一個 UUID 作為短期 Token
    short_uuid_token = str(uuid.uuid4())
    
    # 4. 儲存到 Sheets 中，設置到期時間 (UNIX timestamp)
    expires_at = int(time.time() + QR_CODE_EXP_SECONDS)
    
    token_payload = {
        "token_uuid": short_uuid_token,
        "participant_id": request.participant_id,
        "agenda_item_id": request.agenda_item_id,
        "device_id": request.device_id,
        "expires_at": expires_at # 儲存為整數 timestamp
    }
    
    sheets_manager.add_qr_token(token_payload) # 🆕 改為寫入 Sheets

    # 5. 返回短 Token 和到期時間
    return {
        "status": "success",
        "qr_code_token": short_uuid_token, # 返回 UUID
        "expires_in": QR_CODE_EXP_SECONDS
    }


# 4. 報到掃碼 API (App A 核心功能) - 💥 修正使用 Google Sheets
@app.post("/api/v1/attendance/check-in", tags=["Attendance"], summary="Check In")
def check_in(request: CheckInRequest, sheets_manager: SheetsManager = Depends(get_sheets_manager)):
    """
    報到掃碼設備 (App A) 掃描 QR Code 後調用此 API 進行報到。
    現在是基於 Google Sheets 的查詢和消費。
    """
    qr_uuid_token = request.qr_code_token
    
    # 1. 🆕 從 Sheets 中查找並刪除 Token（實現一次性消費）
    token_data = sheets_manager.consume_qr_token(qr_uuid_token) 

    if not token_data:
        raise HTTPException(status_code=400, detail="報到失敗：QR Code 無效或已使用。")
    
    # 2. 檢查是否過期
    if time.time() > token_data['expires_at']:
        raise HTTPException(status_code=400, detail="報到失敗：QR Code 已過期。")

    # 3. 獲取 Token 內含資訊 (從 Sheets 取得)
    p_id = token_data.get('participant_id')
    a_id_token = token_data.get('agenda_item_id')
    
    # 4. 業務邏輯驗證
    
    # a. 驗證議程 ID 是否匹配
    if a_id_token != request.agenda_item_id:
        raise HTTPException(status_code=400, detail="報到失敗：議程 ID 不匹配。")
    
    # b. 檢查是否已報到 
    attendance_logs = sheets_manager.get_all_records('Attendance_Log')
    is_already_checked_in = any(
        log.get('participant_id') == p_id and log.get('agenda_item_id') == a_id_token 
        for log in attendance_logs
    )
    if is_already_checked_in:
        raise HTTPException(status_code=400, detail="報到失敗：該學員已報到過。")

    # 5. 寫入報到記錄
    checkin_time_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    log_data = [
        str(uuid.uuid4()),  # 記錄 ID
        p_id, 
        request.agenda_item_id, 
        checkin_time_utc,
        "QR_CODE", 
        request.scanner_device_id,
        "TRUE"
    ]
    
    sheets_manager.append_row('Attendance_Log', log_data)
    
    # 6. 獲取學員名稱 (用於 App A 顯示)
    participant_data = sheets_manager.find_record_by_id('Participants', p_id, id_column=1)
    
    return {
        "status": "success",
        "participant_name": participant_data.get('name', '未知學員'),
        "participant_id": p_id,
        "checkin_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": "報到成功！"
    }


# 5. 參與者註冊 API (App B 流程 #1)
@app.post("/api/v1/auth/participant-signup", tags=["Auth"], summary="Participant Sign Up")
def participant_signup(request: ParticipantSignupRequest, sheets_manager: SheetsManager = Depends(get_sheets_manager)):
    """學員/參與者註冊新帳號。"""
    
    if len(request.password) < 6:
        raise HTTPException(status_code=400, detail="密碼長度至少需要 6 個字符")

    participants = sheets_manager.get_all_records('Participants')
    for p in participants:
        if p.get('email') == request.email:
            raise HTTPException(status_code=400, detail="此 Email 已被註冊")
        if p.get('phone_number') == request.phone_number:
            raise HTTPException(status_code=400, detail="此手機號碼已被註冊")

    hashed_password = bcrypt.hashpw(
        request.password.encode('utf-8'), 
        bcrypt.gensalt()
    ).decode('utf-8')
    
    new_id = generate_new_participant_id(sheets_manager)

    new_row_data = [
        new_id,
        request.name,
        request.email,
        request.phone_number,
        request.organization if request.organization else '',
        hashed_password
    ]

    try:
        sheets_manager.append_row('Participants', new_row_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"註冊寫入資料庫失敗: {e}")

    return {
        "status": "success",
        "participant_id": new_id,
        "message": "註冊成功！您現在可以使用 Email 和密碼登入 App B。"
    }

# 6. 參與者登入 API (App B 流程 #2)
@app.post("/api/v1/auth/participant-login", tags=["Auth"], summary="Participant Login")
def participant_login(request: ParticipantLoginRequest, sheets_manager: SheetsManager = Depends(get_sheets_manager)):
    """學員/參與者登入，成功後返回 Session Token。"""
    
    participants = sheets_manager.get_all_records('Participants')
    participant_data = next((p for p in participants if p.get('email') == request.email), None)

    if not participant_data:
        raise HTTPException(status_code=401, detail="Email 或密碼錯誤")
    
    password_hash = participant_data.get('login_hash')
    if not password_hash:
        raise HTTPException(status_code=500, detail="資料錯誤：參與者密碼雜湊遺失")
        
    password_hash = password_hash.encode('utf-8')
    
    if not bcrypt.checkpw(request.password.encode('utf-8'), password_hash):
        raise HTTPException(status_code=401, detail="Email 或密碼錯誤")

    # 生成 JWT Session Token (長時間有效)
    token_payload = {
        "sub": participant_data.get('id'),
        "user_role": "PARTICIPANT",
        "token_type": "session"
    }
    session_token = create_jwt_token(
        token_payload,
        datetime.timedelta(days=PARTICIPANT_SESSION_EXP_DAYS)
    )

    return {
        "status": "success",
        "participant_id": participant_data.get('id'),
        "name": participant_data.get('name'),
        "session_token": session_token,
        "message": "登入成功，歡迎使用會議報到 App。"
    }

# --- 運行應用程式 (註釋：由 uvicorn main:app --reload 執行) ---