# send_reminders.py (修正後)
import os
import logging
from datetime import datetime, timezone
import json
from sqlalchemy import text, update # create_engine, sessionmaker は削除
import io
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import Header
import mimetypes
import base64
from googleapiclient.discovery import build # build は common_utils にも必要
from zoneinfo import ZoneInfo 
# --- common_utils からインポート ---
from common_utils import (
    get_credentials, get_gdrive_service, calculate_elapsed_period_simple,
    SERVICE_ACCOUNT_FILE, SCOPES, MAIL_SENDER_NAME, DATABASE_URL,
    SessionLocal, engine, # engine, SessionLocal をインポート
    TEMP_DOWNLOAD_FOLDER # ダウンロード用一時フォルダ
)
# ★★★ 追加: 環境変数からサービスアカウントのメールアドレスを取得 ★★★
SERVICE_ACCOUNT_EMAIL = os.environ.get('SERVICE_ACCOUNT_EMAIL')
if not SERVICE_ACCOUNT_EMAIL:
    logging.warning("[Job] 環境変数 SERVICE_ACCOUNT_EMAIL が未設定です。Fromヘッダーが不完全になる可能性があります。")
    # 必要に応じてデフォルト値を設定するか、エラーにする

# --- ロギング設定 (common_utils で設定済みなら不要かも) ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [CronJob] - %(message)s')

# --- DB接続チェック ---
if not engine or not SessionLocal:
     logging.error("★★★ 致命的エラー: データベース接続が初期化されていません (common_utilsを確認) ★★★")
     exit(1) # CronジョブはDBないと実行不可

# --- メール送信関数 (Driveダウンロード版) ---
def send_reminder_email_with_download(to_email, upload_time, file_details, message_body=''):
    logging.info(f"--- [Job Start] シンプルメール送信テスト: 宛先={to_email} ---")
    email_sent_successfully = False
    try:
        # --- Gmail API 認証情報取得 ---
        creds = get_credentials()
        if not creds: raise Exception("Failed to get credentials for Gmail API")
        gmail_service = build('gmail', 'v1', credentials=creds)

        # --- シンプルな件名と本文 ---
        subject = "【テスト】タイムカプセル開封通知"
        body = f"これはタイムカプセルからのテストメールです。\n宛先: {to_email}\nアップロード日時(UTC): {upload_time}"

        # --- メールメッセージの作成 (添付なし) ---
        message = MIMEMultipart() # 添付なくても Multipart で良い
        message['to'] = to_email
        if SERVICE_ACCOUNT_EMAIL:
            from_address = f"{MAIL_SENDER_NAME} <{SERVICE_ACCOUNT_EMAIL}>"
            message['from'] = Header(from_address, 'utf-8').encode()
        else:
            message['from'] = Header(MAIL_SENDER_NAME, 'utf-8').encode()
        message['subject'] = Header(subject, 'utf-8').encode()
        message.attach(MIMEText(body, 'plain', 'utf-8'))

        # --- メールの送信 ---
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': raw_message}
        logging.info(f"[Job] Gmail API を使用してシンプルメールを送信します (userId='me')...")
        send_message = gmail_service.users().messages().send(userId='me', body=create_message).execute()
        logging.info(f"[Job] シンプルテストメールを {to_email} に送信しました。 Message ID: {send_message['id']}")
        email_sent_successfully = True

    except HttpError as error:
        logging.error(f"[Job] Gmail APIエラー (シンプルテスト): {error}") # エラー内容は変わるか？
        # エラー詳細をさらに詳しくログ出力
        try:
            error_details = json.loads(error.content.decode())
            logging.error(f"[Job] Gmail APIエラー詳細 (シンプルテスト): {error_details}")
        except:
            logging.error(f"[Job] Gmail APIエラー内容 (raw, シンプルテスト): {error.content}")
    except Exception as e:
        logging.error(f"[Job] シンプルメール送信処理中に予期せぬエラー: {e}", exc_info=True)
    finally:
        # 一時ファイル削除は不要
        logging.info(f"--- [Job End] シンプルメール送信テスト終了: 宛先={to_email}, 成功={email_sent_successfully} ---")
        return email_sent_successfully

# --- 保留中のリマインダー処理関数 ---
def process_pending_reminders():
    logging.info("--- 保留中のリマインダーの確認を開始 ---")
    db = None
    processed_count = 0; error_count = 0
    try:
        db = SessionLocal() # common_utils からインポート
        now_utc = datetime.now(timezone.utc)
        logging.info(f"現在時刻 (UTC): {now_utc}")
        stmt = text("""SELECT id, remind_email, remind_at, message_body, gdrive_file_details, upload_time FROM reminders WHERE status = 'pending' AND remind_at <= :now ORDER BY remind_at LIMIT 10""")
        result = db.execute(stmt, {'now': now_utc})
        pending_reminders = result.fetchall()

        if not pending_reminders: logging.info("処理対象の保留中リマインダーはありません。"); return
        logging.info(f"処理対象のリマインダー数: {len(pending_reminders)}")

        for reminder_data in pending_reminders:
            reminder_id, email, remind_at_db, msg, details_json, upload_time_db = reminder_data
            logging.info(f"--- リマインダー処理開始: ID={reminder_id}, Email={email}, RemindAt={remind_at_db} ---")
            file_details = []
            if details_json:
                try:
                    if isinstance(details_json, (dict, list)): file_details = details_json
                    elif isinstance(details_json, str): file_details = json.loads(details_json)
                    else: logging.warning(f"リマインダーID {reminder_id}: 予期しない gdrive_file_details の型: {type(details_json)}")
                except json.JSONDecodeError as e_json:
                    logging.error(f"リマインダーID {reminder_id}: gdrive_file_details のJSONパースに失敗: {e_json}")
                    try:
                        update_stmt = text("UPDATE reminders SET status = 'failed', updated_at = NOW() WHERE id = :id")
                        db.execute(update_stmt, {'id': reminder_id}); db.commit()
                        error_count += 1
                    except Exception as e_update_fail: logging.error(f"リマインダーID {reminder_id} の failed へのステータス更新に失敗 (JSONパースエラー後): {e_update_fail}")
                    continue
            try:
                if upload_time_db.tzinfo is None or remind_at_db.tzinfo is None:
                     logging.warning(f"リマインダーID {reminder_id}: DB日時にタイムゾーン情報なし。UTCと仮定。")
                     upload_time_db = upload_time_db.replace(tzinfo=timezone.utc)
                     remind_at_db = remind_at_db.replace(tzinfo=timezone.utc)
                success = send_reminder_email_with_download(email, upload_time_db, file_details, msg) # このファイル内で定義
                new_status = 'sent' if success else 'failed'
                update_stmt = text("UPDATE reminders SET status = :status, updated_at = NOW() WHERE id = :id")
                db.execute(update_stmt, {'status': new_status, 'id': reminder_id}); db.commit()
                logging.info(f"リマインダーID {reminder_id} のステータスを '{new_status}' に更新しました。")
                if success: processed_count += 1
                else: error_count += 1
            except Exception as e_send:
                logging.error(f"リマインダーID {reminder_id} のメール送信/更新処理中にエラー: {e_send}", exc_info=True)
                error_count += 1
                try:
                    if db.is_active:
                         update_stmt = text("UPDATE reminders SET status = 'failed', updated_at = NOW() WHERE id = :id")
                         db.execute(update_stmt, {'id': reminder_id}); db.commit()
                    else: logging.warning(f"リマインダーID {reminder_id}: DBセッション無効のためfailed更新スキップ")
                except Exception as e_update_fail: logging.error(f"リマインダーID {reminder_id} の failed へのステータス更新に失敗 (送信エラー後): {e_update_fail}")
            logging.info(f"--- リマインダー処理終了: ID={reminder_id} ---")
    except Exception as e:
        logging.error(f"リマインダー処理全体でエラーが発生しました: {e}", exc_info=True)
        if db and db.is_active: 
            try: 
                db.rollback()
            except Exception as e_rollback: 
                logging.error(f"DBロールバックエラー: {e_rollback}")
    finally:
        if db: db.close()
        logging.info(f"--- 保留中のリマインダーの確認を終了。処理済み: {processed_count}件, エラー: {error_count}件 ---")

# --- スクリプト直接実行時の処理 (変更なし) ---
if __name__ == "__main__":
    # ... (変更なし) ...
    logging.info("--- Cronジョブスクリプト (send_reminders.py) を直接実行モードで開始します ---")
    if not DATABASE_URL: logging.error("DATABASE_URL 未設定"); exit(1)
    if not os.path.exists(SERVICE_ACCOUNT_FILE): logging.error(f"サービスアカウントファイルが見つかりません: {SERVICE_ACCOUNT_FILE}"); exit(1)
    process_pending_reminders()
    logging.info("--- Cronジョブスクリプト (send_reminders.py) を直接実行モードで終了します ---")

