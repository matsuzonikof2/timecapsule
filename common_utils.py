# common_utils.py
import os
import logging
from datetime import datetime, timezone
from sqlalchemy import create_engine, text, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.dialects.postgresql import JSONB
from googleapiclient.discovery import build
from google.oauth2 import service_account
import sys

# --- 設定 ---
SERVICE_ACCOUNT_FILE = '/etc/secrets/service_account.json'
SCOPES = ['openid', 'https://www.googleapis.com/auth/drive',  'https://www.googleapis.com/auth/userinfo.email']
FOLDER_ID = '1ju1sS1aJxyUXRZxTFXpSO-sN08UKSE0s'
MAIL_SENDER_NAME = 'Time Capsule Keeper'
DATABASE_URL = os.environ.get('DATABASE_URL')

# ★★★ Mailjet 用の環境変数を追加 ★★★
MAILJET_API_KEY = os.environ.get('MAILJET_API_KEY')
MAILJET_SECRET_KEY = os.environ.get('MAILJET_SECRET_KEY')
MAIL_FROM_EMAIL = os.environ.get('MAIL_FROM_EMAIL') # 送信元アドレスは共通


# --- 一時フォルダ ---
# (必要に応じてアップロード用とダウンロード用を分けるか、共通にする)
TEMP_UPLOAD_FOLDER = '/tmp/timecapsule_uploads'
TEMP_DOWNLOAD_FOLDER = '/tmp/timecapsule_downloads'
os.makedirs(TEMP_UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_DOWNLOAD_FOLDER, exist_ok=True)

# --- ロギング設定 ---
# (どちらか一方、またはアプリ全体で統一した設定を行う)
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- SQLAlchemy 設定 ---
if not DATABASE_URL:
    logging.error("★★★ 致命的エラー: 環境変数 DATABASE_URL が設定されていません ★★★")
    # アプリケーションの起動を止めるか、エラー処理を行う
    # sys.exit(1) # ここで止めるとインポートだけでも失敗する可能性
    engine = None
    SessionLocal = None
    Base = None
else:
    try:
        engine = create_engine(DATABASE_URL)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        Base = declarative_base() # Reminderモデル定義は test0320.py に残す
    except Exception as e:
         logging.error(f"データベースエンジン/セッションの作成に失敗: {e}")
         engine = None
         SessionLocal = None
         Base = None

# --- Google API 認証関数 ---
def get_credentials():
    """サービスアカウントキーファイルを使用してGoogle APIの認証情報を取得する"""
    try:
        if not os.path.exists(SERVICE_ACCOUNT_FILE):
            logging.error(f"サービスアカウントキーファイルが見つかりません: {SERVICE_ACCOUNT_FILE}")
            return None
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        return creds
    except Exception as e:
        logging.error(f"サービスアカウント認証情報の読み込み中にエラーが発生しました: {e}", exc_info=True)
        return None

# --- Google Drive サービス取得関数 ---
def get_gdrive_service():
    creds = get_credentials()
    if not creds:
        logging.error("認証情報の取得に失敗しました。(get_gdrive_service)")
        return None
    try:
        service = build('drive', 'v3', credentials=creds)
        return service
    except Exception as e:
        logging.error(f"Google Driveサービスへの接続中にエラーが発生しました: {e}")
        return None

# --- 経過期間計算関数 ---
def calculate_elapsed_period_simple(start_time):
    """開始時刻から現在までの経過期間を文字列で返す (簡易版)"""
    if start_time.tzinfo is None:
        logging.warning("calculate_elapsed_period_simple に naive datetime が渡されました。UTCと仮定します。")
        start_time = start_time.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - start_time
    days = delta.days
    if days < 0: return "未来"
    if days >= 365:
        years = days // 365
        months = (days % 365) // 30
        period_str = f"約{years}年"
        if months > 0: period_str += f"{months}ヶ月"
    elif days >= 30:
        months = days // 30
        remaining_days = days % 30
        period_str = f"約{months}ヶ月"
        if remaining_days > 0: period_str += f"{remaining_days}日"
    elif days > 0: period_str = f"{days}日"
    else:
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        if hours > 0: period_str = f"約{hours}時間"
        elif minutes > 0: period_str = f"約{minutes}分"
        else: period_str = "ほんの少し"
    return period_str
# ★★★ 初期化時のチェックに Mailjet 関連を追加 ★★★
if not MAILJET_API_KEY:
    logging.warning("★★★ 警告: 環境変数 MAILJET_API_KEY が設定されていません。メール送信に失敗します。 ★★★")
if not MAILJET_SECRET_KEY:
    logging.warning("★★★ 警告: 環境変数 MAILJET_SECRET_KEY が設定されていません。メール送信に失敗します。 ★★★")
if not MAIL_FROM_EMAIL:
    logging.warning("★★★ 警告: 環境変数 MAIL_FROM_EMAIL が設定されていません。メール送信に失敗します。 ★★★")
