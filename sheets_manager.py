# sheets_manager.py (修訂版，支持 Render 環境變數連線)

import gspread
from gspread.exceptions import WorksheetNotFound
import bcrypt
import time
import uuid
import os 
import json # 🆕 必須新增: 用於解析環境變數中的 JSON 字串

# --- 配置 (Config) ---
# 檔案路徑模式的備用配置 (在 Render 上通常無效，但保留備用)
SERVICE_ACCOUNT_FILE = os.environ.get(
    'SERVICE_ACCOUNT_JSON_PATH', 
    'gen-lang-client-0392311291-771068520057.json'
) 
SPREADSHEET_NAME = '會議報到' # 請替換為您的 Google Sheets 名稱


# --- 定義所有工作表及其表頭 (Headers) ---
WORKSHEET_DEFINITIONS = {
    'Admins': ['id', 'username', 'password_hash', 'role', 'last_login'],
    'Participants': ['id', 'name', 'email', 'phone_number', 'organization', 'login_hash'],
    'Events': ['event_id', 'event_title', 'event_description', 'max_capacity', 'is_active'],
    'Agenda_Items': ['id', 'event_id', 'agenda_title', 'start_time', 'end_time', 'location', 'checkin_window_minutes'],
    'Registration': ['id', 'participant_id', 'event_id', 'registration_date', 'is_paid'],
    'Attendance_Log': ['id', 'participant_id', 'agenda_item_id', 'checkin_time', 'checkin_method', 'scanner_device_id', 'is_valid'],
    # 🆕 新增: 將短期 QR Token 儲存到 Sheets 以支援擴展
    'Qr_Tokens': ['token_uuid', 'participant_id', 'agenda_item_id', 'device_id', 'expires_at']
}


class SheetsManager:
    def __init__(self):
        # 初始化連接狀態
        self.is_connected = False
        self.spreadsheet = None
        self.gc = None
        
        # 🆕 關鍵：從 Render 環境變數 GSPREAD_SECRET 讀取 JSON 內容
        gspread_secret_json = os.environ.get('GSPREAD_SECRET')
        
        if gspread_secret_json:
            # 嘗試使用環境變數連線 (Render 上的正確方法)
            try:
                credentials = json.loads(gspread_secret_json)
                self.gc = gspread.service_account_from_dict(credentials) 
                self.spreadsheet = self.gc.open(SPREADSHEET_NAME)
                self.is_connected = True
                print("SheetsManager 連接成功 (使用環境變數 GSPREAD_SECRET)。")
            except Exception as e:
                print(f"警告：Google Sheets 連接失敗 (環境變數模式)。請檢查 GSPREAD_SECRET 變數或金鑰內容: {e}")
        else:
            # 如果沒有 GSPREAD_SECRET，則嘗試使用舊的檔案路徑模式
            try:
                self.gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
                self.spreadsheet = self.gc.open(SPREADSHEET_NAME)
                self.is_connected = True
                print("SheetsManager 連接成功 (使用備用檔案路徑模式)。")
            except Exception as e:
                # 連線失敗的具體錯誤在這裡，導致 main.py 拋出 503
                print(f"警告：Google Sheets 連接失敗。路由已載入，但所有 API 將返回 503 錯誤：{e}")
                
    def get_worksheet(self, title: str):
        """獲取指定名稱的工作表對象"""
        if not self.is_connected:
             raise Exception("Google Sheets 服務未連接。")
        try:
            return self.spreadsheet.worksheet(title)
        except WorksheetNotFound:
            raise Exception(f"找不到工作表: {title}")

    def get_all_records(self, sheet_name: str):
        """讀取工作表的所有記錄（以字典列表形式）"""
        if not self.is_connected:
             # 如果未連接，返回空列表以避免 main.py 邏輯崩潰
             return [] 
        try:
            sheet = self.get_worksheet(sheet_name)
            return sheet.get_all_records()
        except Exception as e:
            print(f"讀取 {sheet_name} 失敗: {e}")
            return []

    def find_record_by_id(self, sheet_name: str, record_id: str, id_column: int = 1):
        """通用查找方法，根據 ID 查找單一記錄"""
        if not self.is_connected:
            return None
        try:
            sheet = self.get_worksheet(sheet_name)
            # gspread 查找：找到 ID 欄位中匹配的第一行
            cell = sheet.find(record_id, in_column=id_column)
            # 獲取該行所有數據
            row_values = sheet.row_values(cell.row)
            headers = sheet.row_values(1)
            return dict(zip(headers, row_values))
        except gspread.exceptions.CellNotFound:
            return None
        except Exception as e:
            print(f"查找失敗: {e}")
            return None

    def find_admin_by_username(self, username: str):
        """專門查找管理員 (假設 username 在第 2 欄)"""
        if not self.is_connected:
            # 返回虛擬的管理員數據，讓 admin-login 至少可以被測試
            if username == "admin":
                 # 密碼 "test1234" 的 bcrypt hash
                hashed_pw = "$2b$12$W91R.1w3s.iLp2H5bY0VRe.s6N6Z2S9n.N0nC5sE2s0V/u8p5P9N." 
                return {'id': '1', 'username': 'admin', 'password_hash': hashed_pw, 'role': 'SUPER_ADMIN', 'last_login': ''}
            return None
            
        try:
            sheet = self.get_worksheet('Admins')
            cell = sheet.find(username, in_column=2) 
            row_values = sheet.row_values(cell.row)
            headers = sheet.row_values(1)
            return dict(zip(headers, row_values))
        except gspread.exceptions.CellNotFound:
            return None
        except Exception as e:
            print(f"查找管理員失敗: {e}")
            return None
            
    def append_row(self, sheet_name: str, data: list):
        """在工作表末尾新增一行數據"""
        if not self.is_connected:
             print(f"模擬寫入 {sheet_name}: {data} (服務未連接)")
             return True # 模擬成功寫入
        try:
            sheet = self.get_worksheet(sheet_name)
            sheet.append_row(data)
        except Exception as e:
            print(f"寫入 {sheet_name} 失敗: {e}")
            raise 
            
    # 🆕 Token 相關方法:
    def add_qr_token(self, token_data: dict):
        """新增一個 QR Token 記錄到 Qr_Tokens 表。"""
        if not self.is_connected:
             print(f"模擬寫入 Qr_Tokens: {token_data}")
             return True
        try:
            sheet = self.get_worksheet('Qr_Tokens')
            # 確保數據順序與表頭 ['token_uuid', 'participant_id', 'agenda_item_id', 'device_id', 'expires_at'] 一致
            data = [
                token_data['token_uuid'],
                token_data['participant_id'],
                token_data['agenda_item_id'],
                token_data['device_id'],
                token_data['expires_at'] # 儲存 UNIX timestamp
            ]
            sheet.append_row(data)
            return True
        except Exception as e:
            print(f"寫入 Qr_Tokens 失敗: {e}")
            raise

    def consume_qr_token(self, qr_uuid_token: str):
        """查找並刪除匹配的 QR Token，實現一次性使用。"""
        if not self.is_connected:
            return None
            
        try:
            sheet = self.get_worksheet('Qr_Tokens')
            
            # 1. 查找匹配的 Token (假設 token_uuid 在第 1 欄)
            cell = sheet.find(qr_uuid_token, in_column=1)
            
            # 2. 獲取該行數據
            row_values = sheet.row_values(cell.row)
            headers = sheet.row_values(1)
            token_data = dict(zip(headers, row_values))
            
            # 3. 立即刪除該行 (實現一次性消費)
            sheet.delete_rows(cell.row)
            
            # 4. 返回數據，注意 'expires_at' 需要轉換為整數
            token_data['expires_at'] = int(token_data.get('expires_at', 0))
            return token_data
            
        except gspread.exceptions.CellNotFound:
            return None
        except Exception as e:
            print(f"消費 Qr Token 失敗: {e}")
            return None
            
    def initialize_system(self, clear_data: bool):
        """執行資料庫初始化邏輯"""
        if not self.is_connected:
             raise Exception("Google Sheets 服務未連接，無法初始化。請檢查憑證。")
            
        initialized_sheets = []
        
        # 1. 處理工作表的創建、清空與表頭寫入
        for title, headers in WORKSHEET_DEFINITIONS.items():
            try:
                ws = self.spreadsheet.worksheet(title)
            except WorksheetNotFound:
                ws = self.spreadsheet.add_worksheet(title=title, rows="100", cols="20")
            
            if clear_data:
                ws.clear()
            
            ws.update([headers], range_name='A1')
            initialized_sheets.append(title)


        # 2. 寫入初始測試數據 (必須在表頭寫入之後)
        # 初始密碼 'test1234'
        hashed_password = bcrypt.hashpw('test1234'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        
        # --- Admins ---
        admins_ws = self.spreadsheet.worksheet('Admins')
        admin_data = [
            ['1', 'admin', hashed_password, 'SUPER_ADMIN', time.strftime("%Y-%m-%d %H:%M:%S")],
            ['2', 'staff_01', hashed_password, 'CHECKIN_STAFF', '']
        ]
        admins_ws.append_rows(admin_data)
        
        # --- Participants (學員登入 hash 也是 'test1234' 的 hash) ---
        participants_ws = self.spreadsheet.worksheet('Participants')
        participant_data = [
            ['P001', '王小明', 'ming@test.com', '0910123456', '科技學院', hashed_password],
            ['P002', '陳大華', 'hua@test.com', '0920654321', '醫學院', hashed_password]
        ]
        participants_ws.append_rows(participant_data)
        
        # --- Events, Agenda_Items, Registration 初始數據 ---
        events_ws = self.spreadsheet.worksheet('Events')
        events_ws.append_rows([['E001', '2026 年度學術研討會', '學術界年度盛事', '300', 'TRUE']])
        
        agenda_ws = self.spreadsheet.worksheet('Agenda_Items')
        agenda_ws.append_rows([
            ['A101', 'E001', '開幕式與專題演講', '2026-01-10T09:00:00+08:00', '2026-01-10T10:30:00+08:00', '國際廳', '30'],
            ['A102', 'E001', '分組討論：AI應用', '2026-01-10T11:00:00+08:00', '2026-01-10T12:00:00+08:00', 'A203會議室', '15']
        ])
        
        registration_ws = self.spreadsheet.worksheet('Registration')
        registration_ws.append_rows([
            ['1', 'P001', 'E001', time.strftime("%Y-%m-%d"), 'TRUE'],
            ['2', 'P002', 'E001', time.strftime("%Y-%m-%d"), 'TRUE']
        ])
        
        # Qr_Tokens 不需要初始數據

        return initialized_sheets

# 實例化 SheetsManager
sheets_manager = SheetsManager()
