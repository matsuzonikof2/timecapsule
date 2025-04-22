# test0320.py (修正後)
from flask import Flask, request, jsonify, redirect, url_for, render_template
import os
from datetime import datetime, timezone
import base64
from email.mime.text import MIMEText
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import mimetypes
import logging
from sqlalchemy import text, Column, Integer, String, DateTime, Text # create_engine, sessionmaker, declarative_base は削除
from sqlalchemy.dialects.postgresql import JSONB
import json
import sys
import threading
from googleapiclient.http import MediaFileUpload
from zoninfo import ZoneInfo 

# --- common_utils からインポート ---
from common_utils import (
    get_credentials, get_gdrive_service, calculate_elapsed_period_simple,
    SERVICE_ACCOUNT_FILE, SCOPES, FOLDER_ID, MAIL_SENDER_NAME, DATABASE_URL,
    SessionLocal, engine, Base, # Base もインポート
    TEMP_UPLOAD_FOLDER # アップロード用一時フォルダ
)

# --- send_reminders からインポート ---
try:
    from send_reminders import process_pending_reminders
except ImportError as e:
    # ★★★ 循環インポート解消後もエラーが出る場合は、send_reminders.py 側のインポートも確認 ★★★
    logging.error(f"send_reminders.py から process_pending_reminders のインポートに失敗しました: {e}", exc_info=True)
    process_pending_reminders = None

# --- Flask アプリ設定 ---
app = Flask(__name__, static_folder='.', static_url_path='')
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'default-secret-key')

# --- ロギング設定 (common_utils で設定済みなら不要かも) ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- DB接続チェック ---
if not engine or not SessionLocal or not Base:
     logging.error("★★★ 致命的エラー: データベース接続が初期化されていません (common_utilsを確認) ★★★")
     # 必要ならここで sys.exit(1)

# --- Reminder モデル定義 (Base を common_utils からインポート) ---
class Reminder(Base):
    __tablename__ = "reminders"
    id = Column(Integer, primary_key=True, index=True)
    remind_email = Column(String(255), nullable=False)
    remind_at = Column(DateTime(timezone=True), nullable=False)
    message_body = Column(Text)
    gdrive_file_details = Column(JSONB)
    upload_time = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, default='pending')
    created_at = Column(DateTime(timezone=True), server_default=text('CURRENT_TIMESTAMP'))
    updated_at = Column(DateTime(timezone=True), server_default=text('CURRENT_TIMESTAMP'), onupdate=text('CURRENT_TIMESTAMP'))

# --- Google Drive アップロード関数 (get_gdrive_service を common_utils から使用) ---
def upload_to_gdrive(file_path, file_name, folder_id):
    service = get_gdrive_service() # common_utils からインポートした関数を使用
    if service is None:
        logging.error("Google Driveサービスへの接続に失敗しました。認証を確認してください。")
        return None
    file_metadata = {'name': file_name, 'parents': [folder_id]}
    media = MediaFileUpload(file_path, resumable=True)
    try:
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        file_id = file.get('id')
        logging.info(f"Google Driveへのアップロード成功: File ID={file_id}, Name='{file_name}'")
        return file_id
    except Exception as e:
        logging.error(f"Google Driveへのアップロード中にエラーが発生しました: {e}")
        return None

# --- Flask ルート (index, login は変更なし) ---
@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    # ... (変更なし) ...
    if request.method == 'POST':
        username = request.form.get('username')
        login_status = request.form.get('login')
        if login_status != 'true' or username != 'ai_academy':
            logging.warning(f"ログイン失敗: username={username}, login_status={login_status}")
            return jsonify({"code": 401, "msg": "Unauthorized"}), 401
        logging.info(f"ログイン成功: username={username}")
        return redirect(url_for('upload', username=username, login=login_status))
    else:
        return app.send_static_file('login.html')

# --- /upload ルート (TEMP_UPLOAD_FOLDER を common_utils から使用) ---
@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'GET':
        # ... (GET処理は変更なし) ...
        username = request.args.get('username')
        login_status = request.args.get('login')
        if login_status != 'true' or username != 'ai_academy':
             logging.warning(f"アップロードページへの不正アクセス試行: username={username}, login={login_status}")
             return redirect(url_for('login'))
        return app.send_static_file('upload.html')

    if request.method == 'POST':
        # ... (ファイル処理、リマインダー情報取得は変更なし) ...
        logging.info("--- ファイルアップロード処理開始 ---")
        if 'file' not in request.files: return jsonify({"msg": "No file part"}), 400
        files = request.files.getlist('file')
        if not files or files[0].filename == '': return jsonify({"msg": "No selected file"}), 400
        remind_datetime_str = request.form.get('remind_datetime')
        remind_email = request.form.get('remind_email')
        message_body = request.form.get('message', '')
        upload_time = datetime.now(timezone.utc)
        if not remind_datetime_str or not remind_email: return jsonify({"msg": "Reminder date/time and email are required"}), 400
        try:
            # --- ↓↓↓ タイムゾーン処理部分を修正 ↓↓↓ ---
            # 1. ユーザー入力を naive datetime に変換
            remind_datetime_naive = datetime.strptime(remind_datetime_str, '%Y-%m-%dT%H:%M')

            # 2. 日本時間 (JST) のタイムゾーン情報を取得
            jst = ZoneInfo("Asia/Tokyo")

            # 3. naive な日時を JST として解釈 (aware datetime にする)
            remind_datetime_jst = remind_datetime_naive.replace(tzinfo=jst)
            logging.info(f"ユーザー入力日時を JST として解釈: {remind_datetime_jst}")

            # 4. JST の日時を UTC に変換してDB保存用とする
            remind_datetime_utc = remind_datetime_jst.astimezone(timezone.utc)
            logging.info(f"DB保存用のUTC日時に変換: {remind_datetime_utc}")

            # 5. 未来の日時かチェック (比較はUTCで行う)
            if remind_datetime_utc <= datetime.now(timezone.utc):
                 logging.warning(f"リマインダー日時が過去です: {remind_datetime_str} (JST: {remind_datetime_jst}, UTC: {remind_datetime_utc})")
                 return jsonify({"msg": "Reminder date/time must be in the future"}), 400
        except ValueError:
            logging.error(f"無効な日時フォーマットです: {remind_datetime_str}")
            return jsonify({"msg": "Invalid date/time format"}), 400
        except Exception as e_tz: # ZoneInfo関連のエラーも考慮
             logging.error(f"タイムゾーン処理中にエラー: {e_tz}", exc_info=True)
             return jsonify({"msg": "Error processing timezone."}), 500

        uploaded_file_details = []
        temp_file_paths = []
        save_error = False
        upload_error = False
        # TEMP_FOLDER = '/tmp/timecapsule_uploads' # common_utils からインポートした TEMP_UPLOAD_FOLDER を使う

        for file in files:
            if file and file.filename:
                original_filename = file.filename
                safe_filename = f"{datetime.now().timestamp()}_{original_filename}"
                # --- ★★★ 修正: common_utils の TEMP_UPLOAD_FOLDER を使用 ★★★ ---
                file_path = os.path.join(TEMP_UPLOAD_FOLDER, safe_filename)
                try:
                    file.save(file_path)
                    temp_file_paths.append(file_path)
                    file_id = upload_to_gdrive(file_path, original_filename, FOLDER_ID) # upload_to_gdrive はこのファイル内で定義
                    if file_id: uploaded_file_details.append({'id': file_id, 'name': original_filename})
                    else: upload_error = True
                except Exception as e:
                    logging.error(f"ファイル処理中にエラーが発生しました ({original_filename}): {e}", exc_info=True)
                    save_error = True; break

        if save_error or upload_error:
            # ... (エラー処理、一時ファイル削除) ...
            for fp in temp_file_paths:
                if os.path.exists(fp): 
                    try: 
                        os.remove(fp)
                    except Exception as e_rem: 
                        logging.error(f"エラー時の一時ファイル削除失敗: {fp}, Error: {e_rem}")
            if save_error: return jsonify({"msg": "Error saving file temporarily."}), 500
            else: return jsonify({"msg": "Failed to upload one or more files to Google Drive. Reminder not set."}), 500

        if uploaded_file_details:
            db = SessionLocal() # common_utils からインポート
            try:
                new_reminder = Reminder(
                    remind_email=remind_email,
                    remind_at=remind_datetime_utc, # ★★★ UTCで保存 ★★★
                    message_body=message_body,
                    gdrive_file_details=uploaded_file_details,
                    upload_time=upload_time,
                    status='pending'
                )
                db.add(new_reminder)
                db.commit()
                logging.info(f"リマインダー情報をデータベースに保存しました。ID: {new_reminder.id}, Email: {remind_email}, RemindAt(UTC): {remind_datetime_utc}") # ログにUTCであることを明記
            except Exception as e_db:
                db.rollback(); logging.error(f"データベースへのリマインダー保存中にエラー: {e_db}", exc_info=True)
                for fp in temp_file_paths:
                    if os.path.exists(fp): 
                        try: 
                            os.remove(fp)
                        except Exception as e_rem: 
                            logging.error(f"DBエラー後の一時ファイル削除失敗: {fp}, Error: {e_rem}")
                return jsonify({"msg": "Failed to save reminder details to database."}), 500
            finally: db.close()
        else:
            # ... (アップロード成功ファイルなしの場合の処理) ...
             return jsonify({"msg": "No files were successfully uploaded. Reminder not set."}), 400

        # --- 正常終了時の一時ファイル削除 ---
        for fp in temp_file_paths:
            if os.path.exists(fp): 
                try: 
                    os.remove(fp)
                except Exception as e_rem: 
                    logging.error(f"正常終了時の一時ファイル削除失敗: {fp}, Error: {e_rem}")

        # --- 完了メッセージ (変更済み) ---
        try:
            weekdays_jp = ["月", "火", "水", "木", "金", "土", "日"]
            weekday_jp = weekdays_jp[remind_datetime_naive.weekday()]
            formatted_remind_date = remind_datetime_naive.strftime(f'%Y年%m月%d日({weekday_jp})')
            success_message = f"あなたのタイムカプセルは土の中深くに埋められました。開封予定日は{formatted_remind_date}です！"
        except Exception as e_fmt:
            logging.warning(f"リマインダー日時のフォーマット中にエラー: {e_fmt}")
            success_message = f"あなたのタイムカプセルは土の中深くに埋められました。開封予定日は{remind_datetime_str}です！"
        return jsonify({"msg": success_message}), 200

# --- /run-cron ルート (変更なし) ---
@app.route('/run-cron', methods=['POST'])
def run_cron_job():
    # ... (変更なし) ...
    logging.info("--- /run-cron エンドポイント受信 ---")
    CRON_SECRET_KEY = os.environ.get('CRON_SECRET_KEY')
    request_key = request.headers.get('X-Cron-Secret')
    if CRON_SECRET_KEY and request_key != CRON_SECRET_KEY: return jsonify({"msg": "Unauthorized"}), 401
    elif not CRON_SECRET_KEY: logging.warning("CRON_SECRET_KEY未設定のため認証スキップ")
    else: logging.info("Cron実行キー認証成功。")

    if process_pending_reminders is None:
         logging.error("/run-cron: process_pending_reminders 関数がインポートされていません。")
         return jsonify({"msg": "Internal server error: Reminder processing function not available."}), 500
    try:
        thread = threading.Thread(target=process_pending_reminders)
        thread.start()
        logging.info("process_pending_reminders をバックグラウンドで開始しました。")
        return jsonify({"msg": "Reminder check process started in background."}), 202
    except Exception as e:
        logging.error(f"/run-cron でエラーが発生: {e}", exc_info=True)
        return jsonify({"msg": "Error initiating reminder processing."}), 500

# --- アプリケーション実行 (変更なし) ---
if __name__ == "__main__":
    # ... (変更なし) ...
    if app.config['SECRET_KEY'] == 'default-secret-key': logging.warning("Flask SECRET_KEYがデフォルト")
    if not os.path.exists(SERVICE_ACCOUNT_FILE): logging.error(f"サービスアカウントファイルが見つかりません: {SERVICE_ACCOUNT_FILE}")
    if not os.environ.get('CRON_SECRET_KEY'): logging.warning("CRON_SECRET_KEYが未設定")
    logging.info("ローカルデバッグモードでアプリケーションを起動します...")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=True, use_reloader=False)

