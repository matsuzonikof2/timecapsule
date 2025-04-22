# test0320.py (サービスアカウント認証版, Cronジョブ連携版)
from flask import Flask, request, jsonify, redirect, url_for, render_template
import os
# import pickle # pickle は現在使用されていないためコメントアウト
from datetime import datetime, timedelta, timezone # timezone をインポート
# import smtplib # Gmail APIを使うためコメントアウト
import base64
from email.mime.text import MIMEText
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import mimetypes
import logging
# import time # timeモジュールは直接使われていないためコメントアウト
from sqlalchemy import create_engine, text, Column, Integer, String, DateTime, Text # JSONは下でインポート
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.dialects.postgresql import JSONB # PostgreSQLの場合
import json
import sys
import threading # <<< 追加: /run-cron のバックグラウンド実行用 (オプション)

# Google Drive API関連
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
# from google.oauth2.credentials import Credentials # サービスアカウント認証のためコメントアウト
from google.oauth2 import service_account
# from google.auth.transport.requests import Request # サービスアカウント認証のためコメントアウト

# <<< 修正: send_reminders モジュールから処理関数をインポート >>>
# 注意: 循環参照を避けるため、共通関数は別ファイル(common_utils.pyなど)に切り出すのが理想
try:
    # --- ★★★ 注意: ファイル名が send_reminders.py であることを確認 ★★★ ---
    from send_reminders import process_pending_reminders
except ImportError as e:
    logging.error(f"send_reminders.py から process_pending_reminders のインポートに失敗しました: {e}")
    # 起動時にエラーにするか、/run-cron でエラーにするか検討
    process_pending_reminders = None # 実行できないように設定

# --- 設定 ---
# <<< 削除: APSchedulerのデバッグログは不要 >>>
# logging.getLogger('apscheduler').setLevel(logging.DEBUG)

app = Flask(__name__, static_folder='.', static_url_path='')
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'default-secret-key')
SERVICE_ACCOUNT_FILE = '/etc/secrets/service_account.json'

# Google Drive API, Gmail API の設定
SCOPES = ['openid', 'https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/gmail.send', 'https://www.googleapis.com/auth/userinfo.email'] # mail.google.com は通常不要
FOLDER_ID = '1ju1sS1aJxyUXRZxTFXpSO-sN08UKSE0s'

# <<< 削除: メールサーバー設定はGmail API利用のため不要 >>>
# MAIL_SERVER = 'smtp.gmail.com'
# MAIL_PORT = 587
MAIL_USERNAME = os.environ.get('MAIL_USERNAME') # サービスアカウントの代理設定で使う可能性はある
# MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD') # アプリパスワードは不要
MAIL_SENDER_NAME = 'Time Capsule Keeper'

# --- SQLAlchemy 設定 ---
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    logging.error("★★★ 致命的エラー: 環境変数 DATABASE_URL が設定されていません ★★★")
    sys.exit(1)
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- Reminder モデル定義 (変更なし) ---
class Reminder(Base):
    __tablename__ = "reminders"
    id = Column(Integer, primary_key=True, index=True)
    remind_email = Column(String(255), nullable=False)
    remind_at = Column(DateTime(timezone=True), nullable=False) # timezone=True を推奨
    message_body = Column(Text)
    gdrive_file_details = Column(JSONB) # PostgreSQLの場合。他DBならJSONやText
    upload_time = Column(DateTime(timezone=True), nullable=False) # timezone=True を推奨
    status = Column(String(20), nullable=False, default='pending') # 例: pending, sent, failed
    created_at = Column(DateTime(timezone=True), server_default=text('CURRENT_TIMESTAMP'))
    updated_at = Column(DateTime(timezone=True), server_default=text('CURRENT_TIMESTAMP'), onupdate=text('CURRENT_TIMESTAMP'))

# --- 初回実行時などにテーブルを作成 (開発時や単純な場合に利用) ---
# Base.metadata.create_all(bind=engine) # 本番環境ではAlembic等推奨

# --- ロギング設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Google API 認証関数 (サービスアカウント版) ---
# <<< 注意: この関数は send_reminders.py でも使われるため、共通モジュール化推奨 >>>
def get_credentials():
    """サービスアカウントキーファイルを使用してGoogle APIの認証情報を取得する"""
    try:
        if not os.path.exists(SERVICE_ACCOUNT_FILE):
            logging.error(f"サービスアカウントキーファイルが見つかりません: {SERVICE_ACCOUNT_FILE}")
            return None
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        # logging.info("サービスアカウント認証情報を正常に読み込みました。") # 頻繁に出力されるためコメントアウトしても良い
        return creds
    except Exception as e:
        logging.error(f"サービスアカウント認証情報の読み込み中にエラーが発生しました: {e}", exc_info=True)
        return None

# --- Google Drive 関連関数 ---
# <<< 注意: この関数は send_reminders.py でも使われるため、共通モジュール化推奨 >>>
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

# <<< 注意: この関数は send_reminders.py でも使われるため、共通モジュール化推奨 >>>
def upload_to_gdrive(file_path, file_name, folder_id):
    service = get_gdrive_service()
    if service is None:
        logging.error("Google Driveサービスへの接続に失敗しました。認証を確認してください。")
        return None # <<< 修正: 失敗時は None を返す >>>

    file_metadata = {
        'name': file_name,
        'parents': [folder_id]
    }
    # <<< 修正: 一時フォルダを使うように変更推奨 >>>
    # TEMP_FOLDER = '/tmp/timecapsule_uploads' # 例
    # media = MediaFileUpload(os.path.join(TEMP_FOLDER, file_path), resumable=True) # file_pathはファイル名のみ渡すなど調整
    media = MediaFileUpload(file_path, resumable=True) # 現在の実装に合わせる
    try:
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        file_id = file.get('id')
        logging.info(f"Google Driveへのアップロード成功: File ID={file_id}, Name='{file_name}'")
        return file_id
    except Exception as e:
        logging.error(f"Google Driveへのアップロード中にエラーが発生しました: {e}")
        return None # <<< 修正: 失敗時は None を返す >>>

# --- 経過期間計算関数 (簡易版) ---
# <<< 注意: この関数は send_reminders.py でも使われるため、共通モジュール化推奨 >>>
def calculate_elapsed_period_simple(start_time):
    """開始時刻から現在までの経過期間を文字列で返す (簡易版)"""
    # <<< 修正: タイムゾーン対応 >>>
    # start_time が naive datetime の場合、aware にする必要があるかもしれない
    # DBから取得した値は timezone aware になっている想定
    if start_time.tzinfo is None:
        logging.warning("calculate_elapsed_period_simple に naive datetime が渡されました。UTCと仮定します。")
        start_time = start_time.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc) # 現在時刻も timezone aware にする
    delta = now - start_time
    days = delta.days

    # (以降の計算ロジックは変更なし)
    if days < 0:
        return "未来"
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
    elif days > 0:
        period_str = f"{days}日"
    else:
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        if hours > 0: period_str = f"約{hours}時間"
        elif minutes > 0: period_str = f"約{minutes}分"
        else: period_str = "ほんの少し"
    return period_str

# <<< 削除: send_reminder_email 関数は send_reminders.py に集約 >>>
# def send_reminder_email(...):
#     ...

# --- Flask ルート ---
@app.route('/')
def index():
    # static_folder を設定しているのでこれで index.html が返るはず
    return app.send_static_file('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    # (変更なし)
    if request.method == 'POST':
        username = request.form.get('username')
        login_status = request.form.get('login')
        if login_status != 'true' or username != 'ai_academy':
            logging.warning(f"ログイン失敗: username={username}, login_status={login_status}")
            return jsonify({"code": 401, "msg": "Unauthorized"}), 401
        logging.info(f"ログイン成功: username={username}")
        # セッションを使う方がより安全だが、ここではリダイレクトで情報を渡す
        return redirect(url_for('upload', username=username, login=login_status)) # mypage をスキップして upload へ
    else:
        return app.send_static_file('login.html')

# <<< 削除: mypage ルートは直接使われなくなったため削除 (必要なら残す) >>>
# @app.route('/mypage')
# def mypage(): ...

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    # <<< 追加: GETリクエスト時の認証チェック (loginルートからパラメータを受け取る想定) >>>
    if request.method == 'GET':
        username = request.args.get('username')
        login_status = request.args.get('login')
        if login_status != 'true' or username != 'ai_academy':
             logging.warning(f"アップロードページへの不正アクセス試行: username={username}, login={login_status}")
             # ログインページへリダイレクトするか、エラーページを表示
             return redirect(url_for('login'))
        return app.send_static_file('upload.html')

    # --- POST リクエスト (アップロード処理) ---
    if request.method == 'POST':
        logging.info("--- ファイルアップロード処理開始 ---")
        # <<< 追加: ログイン状態の再確認 (フォームにhidden inputなどで含めるか、セッション推奨) >>>
        # ここでは簡易的に省略

        # --- ファイル処理 ---
        if 'file' not in request.files:
            logging.warning("アップロードリクエストにファイルパートがありません。")
            return jsonify({"msg": "No file part"}), 400
        files = request.files.getlist('file')
        if not files or files[0].filename == '':
             logging.warning("アップロードファイルが選択されていません。")
             return jsonify({"msg": "No selected file"}), 400

        # --- リマインダー情報取得 ---
        remind_datetime_str = request.form.get('remind_datetime')
        remind_email = request.form.get('remind_email')
        message_body = request.form.get('message', '')
        # <<< 修正: upload_time はDB保存時に設定するのでここで取得 >>>
        upload_time = datetime.now(timezone.utc) # タイムゾーン付きで現在時刻を取得

        logging.info(f"リマインダー情報: 日時={remind_datetime_str}, メール={remind_email}, メッセージ='{message_body[:20]}...'")

        if not remind_datetime_str or not remind_email:
            logging.warning("リマインダー日時またはメールアドレスが指定されていません。")
            return jsonify({"msg": "Reminder date/time and email are required"}), 400

        try:
            # <<< 修正: 入力文字列を timezone naive な datetime に変換 >>>
            remind_datetime_naive = datetime.strptime(remind_datetime_str, '%Y-%m-%dT%H:%M')
            # <<< 修正: アプリケーションの基準タイムゾーン (例: JST) を考慮し、UTCに変換してDB保存 >>>
            # 例: pytz を使う場合
            # import pytz
            # local_tz = pytz.timezone('Asia/Tokyo') # アプリのタイムゾーン
            # remind_datetime_local = local_tz.localize(remind_datetime_naive)
            # remind_datetime_utc = remind_datetime_local.astimezone(timezone.utc)
            # --- 簡易的に、入力された日時をそのままUTCとみなす場合 (ユーザー入力時のTZに依存) ---
            # remind_datetime_utc = remind_datetime_naive.replace(tzinfo=timezone.utc)
            # --- または、サーバーのローカルタイムゾーンとみなしてUTCに変換 ---
            remind_datetime_local = remind_datetime_naive.astimezone() # OSのTZを付与
            remind_datetime_utc = remind_datetime_local.astimezone(timezone.utc) # UTCに変換

            if remind_datetime_utc <= datetime.now(timezone.utc):
                 logging.warning(f"リマインダー日時が過去です: {remind_datetime_str} (UTC: {remind_datetime_utc})")
                 return jsonify({"msg": "Reminder date/time must be in the future"}), 400
        except ValueError:
            logging.error(f"無効な日時フォーマットです: {remind_datetime_str}")
            return jsonify({"msg": "Invalid date/time format"}), 400

        uploaded_file_details = []
        temp_file_paths = []
        save_error = False
        upload_error = False
        # <<< 追加: 一時フォルダの定義 >>>
        TEMP_FOLDER = '/tmp/timecapsule_uploads' # Renderで書き込み可能なパス
        os.makedirs(TEMP_FOLDER, exist_ok=True)

        # --- ファイルの一時保存とGoogle Driveへのアップロード ---
        for file in files:
            if file and file.filename:
                original_filename = file.filename # secure_filename推奨
                # <<< 修正: 一時フォルダに保存 >>>
                # ファイル名は一意にする（例: タイムスタンプ + 元ファイル名）
                safe_filename = f"{datetime.now().timestamp()}_{original_filename}"
                file_path = os.path.join(TEMP_FOLDER, safe_filename)

                try:
                    file.save(file_path)
                    logging.info(f"一時ファイル '{original_filename}' を保存しました: {file_path}")
                    temp_file_paths.append(file_path)

                    logging.info(f"Uploading '{original_filename}' to Google Drive...")
                    file_id = upload_to_gdrive(file_path, original_filename, FOLDER_ID) # Driveには元の名前で
                    if file_id:
                        logging.info(f"Successfully uploaded '{original_filename}'. File ID: {file_id}")
                        uploaded_file_details.append({'id': file_id, 'name': original_filename})
                    else:
                        logging.warning(f"Failed to upload '{original_filename}' to Google Drive.")
                        upload_error = True
                except Exception as e:
                    logging.error(f"ファイル処理中にエラーが発生しました ({original_filename}): {e}", exc_info=True)
                    save_error = True
                    break # エラー発生時はループ中断

        # --- エラーハンドリング ---
        if save_error or upload_error:
            logging.error("ファイル処理またはDriveアップロード中にエラーが発生しました。")
            # 一時ファイルを削除
            for fp in temp_file_paths:
                if os.path.exists(fp):
                    try: os.remove(fp)
                    except Exception as e_rem: logging.error(f"エラー時の一時ファイル削除失敗: {fp}, Error: {e_rem}")
            if save_error:
                return jsonify({"msg": "Error saving file temporarily."}), 500
            else: # upload_error
                # アップロード失敗してもDBには記録しない（または失敗情報を記録する？）
                # ここではエラーとして処理終了
                return jsonify({"msg": "Failed to upload one or more files to Google Drive. Reminder not set."}), 500

        # --- データベースへの保存 ---
        if uploaded_file_details: # 少なくとも1つのファイルがDriveにアップロード成功した場合
            db = SessionLocal()
            try:
                new_reminder = Reminder(
                    remind_email=remind_email,
                    remind_at=remind_datetime_utc, # UTCで保存
                    message_body=message_body,
                    gdrive_file_details=uploaded_file_details, # JSONB/JSON型
                    upload_time=upload_time, # UTCで保存
                    status='pending'
                )
                db.add(new_reminder)
                db.commit()
                logging.info(f"リマインダー情報をデータベースに保存しました。ID: {new_reminder.id}, Email: {remind_email}, RemindAt: {remind_datetime_utc}")
            except Exception as e_db:
                db.rollback()
                logging.error(f"データベースへのリマインダー保存中にエラー: {e_db}", exc_info=True)
                # 一時ファイルを削除
                for fp in temp_file_paths:
                    if os.path.exists(fp):
                        try: os.remove(fp)
                        except Exception as e_rem: logging.error(f"DBエラー後の一時ファイル削除失敗: {fp}, Error: {e_rem}")
                return jsonify({"msg": "Failed to save reminder details to database."}), 500
            finally:
                db.close()
        else:
            # Driveへのアップロードが全て失敗した場合
            logging.warning("アップロードに成功したファイルがないため、リマインダーは設定されません。")
            # 一時ファイルは削除済みのはずだが念のため
            for fp in temp_file_paths:
                if os.path.exists(fp):
                    try: os.remove(fp)
                    except Exception as e_rem: logging.error(f"アップロード成功ファイルなし時の一時ファイル削除失敗: {fp}, Error: {e_rem}")
            return jsonify({"msg": "No files were successfully uploaded. Reminder not set."}), 400

        # --- 正常終了時の一時ファイル削除 ---
        logging.info("処理正常完了。一時ファイルを削除します。")
        for fp in temp_file_paths:
            if os.path.exists(fp):
                try: os.remove(fp)
                except Exception as e_rem: logging.error(f"正常終了時の一時ファイル削除失敗: {fp}, Error: {e_rem}")

        logging.info("--- ファイルアップロード処理正常終了 ---")
        # <<< 修正: スケジュールはCronで行うため、完了メッセージのみ返す >>>
        return jsonify({"msg": f"タイムカプセルを {remind_datetime_str} に設定しました。指定日時にメールでお知らせします。"}), 200

    # <<< 削除: GETリクエストの処理は上部に移動 >>>
    # else: # GETリクエスト
    #     return app.send_static_file('upload.html')

# --- 追加: Cronジョブ実行用エンドポイント ---
@app.route('/run-cron', methods=['POST'])
def run_cron_job():
    logging.info("--- /run-cron エンドポイント受信 ---")

    # --- 認証 ---
    # 環境変数からCron実行用のAPIキーを取得
    CRON_SECRET_KEY = os.environ.get('CRON_SECRET_KEY')
    if not CRON_SECRET_KEY:
        logging.warning("★★★ 警告: 環境変数 CRON_SECRET_KEY が設定されていません。Cronエンドポイントは保護されません。 ★★★")

    # 例: 'X-Cron-Secret' ヘッダーでキーを受け取る
    request_key = request.headers.get('X-Cron-Secret')

    if CRON_SECRET_KEY and request_key != CRON_SECRET_KEY:
        logging.warning("不正なCron実行リクエスト (キー不一致)")
        return jsonify({"msg": "Unauthorized"}), 401
    elif not CRON_SECRET_KEY:
         logging.warning("CRON_SECRET_KEYが未設定のため、認証をスキップします。")
    else:
         logging.info("Cron実行キー認証成功。")

    # --- リマインダー処理の実行 ---
    if process_pending_reminders is None:
         logging.error("/run-cron: process_pending_reminders 関数がインポートされていません。")
         return jsonify({"msg": "Internal server error: Reminder processing function not available."}), 500

    try:
        logging.info("process_pending_reminders を呼び出します...")

        # --- 同期的に実行する場合 ---
        # process_pending_reminders()
        # logging.info("process_pending_reminders の同期実行が完了しました。")
        # return jsonify({"msg": "Reminder check process completed."}), 200

        # --- 非同期 (バックグラウンドスレッド) で実行する場合 (推奨) ---
        # Gunicornなどのワーカー数によっては注意が必要
        # 実行完了を待たずにすぐにレスポンスを返す
        thread = threading.Thread(target=process_pending_reminders)
        thread.start()
        logging.info("process_pending_reminders をバックグラウンドで開始しました。")
        # 202 Accepted はリクエストを受け付けたが処理は非同期で進行中を示す
        return jsonify({"msg": "Reminder check process started in background."}), 202

    except Exception as e:
        logging.error(f"/run-cron でエラーが発生: {e}", exc_info=True)
        return jsonify({"msg": "Error initiating reminder processing."}), 500

# --- アプリケーションの実行 ---
if __name__ == "__main__":
    # 環境変数 SECRET_KEY のチェック
    if app.config['SECRET_KEY'] == 'default-secret-key':
        logging.warning("警告: FlaskのSECRET_KEYがデフォルト値です。本番環境では必ず環境変数 FLASK_SECRET_KEY を設定してください。")
    # サービスアカウントファイルの存在チェック
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
         logging.error(f"★★★ 致命的エラー: サービスアカウントファイルが見つかりません: {SERVICE_ACCOUNT_FILE} ★★★")
         logging.error("RenderのSecret Files設定を確認してください。")
         # exit(1) # 起動を中止する場合

    # <<< 追加: Cron実行用キーの存在チェック >>>
    if not os.environ.get('CRON_SECRET_KEY'):
        logging.warning("★★★ 警告: 環境変数 CRON_SECRET_KEY が設定されていません。Cronエンドポイントが保護されません。 ★★★")

    # Gunicornから実行される場合は __name__ == "__main__" は通らない
    # ローカルでのデバッグ実行用
    logging.info("ローカルデバッグモードでアプリケーションを起動します...")
    # use_reloader=True はデバッグに便利だが、スレッドが2重起動する場合があるので注意
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=True, use_reloader=False)

