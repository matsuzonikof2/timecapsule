# send_reminders.py
import os
import logging
from datetime import datetime, timezone # timezone をインポート
import json
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import sessionmaker
# --- test0320.py から必要な関数や設定をインポートまたは再定義 ---
from test0320 import get_credentials, get_gdrive_service, calculate_elapsed_period_simple, SERVICE_ACCOUNT_FILE, SCOPES, MAIL_SENDER_NAME # 例
# ★★★ send_reminder_email 関数をここに定義するか、共通モジュールからインポート ★★★
# (Google Driveからダウンロードするバージョン)
# --- メール送信関数 (Gmail API版, サービスアカウント認証, Driveダウンロード対応版) ---
# (以前のレビューで提示したコードをここに貼り付けるか、importする)
# 例: from common_utils import send_reminder_email_with_download
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
from googleapiclient.discovery import build # build をインポート

# --- ロギング設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [CronJob] - %(message)s')

# --- データベース接続設定 ---
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    logging.error("★★★ 致命的エラー: 環境変数 DATABASE_URL が設定されていません ★★★")
    exit(1)
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# --- 一時フォルダ (メール添付用) ---
TEMP_FOLDER = '/tmp/timecapsule_downloads' # Render環境で書き込み可能なパス
os.makedirs(TEMP_FOLDER, exist_ok=True)

# ★★★ send_reminder_email 関数 (Driveダウンロード版) をここに定義 ★★★
# (以前のレビューで提示したコードを参考に、必要なインポートも行う)
def send_reminder_email_with_download(to_email, upload_time, file_details, message_body=''):
    """
    指定されたメールアドレスにリマインドメールを送信する (Gmail API, Driveダウンロード)。
    Args:
        to_email (str): 送信先メールアドレス
        upload_time (datetime): 元のアップロード日時
        file_details (list): Google Driveのファイル情報リスト [{'id': '...', 'name': '...'}, ...]
        message_body (str): ユーザーからのメッセージ
    """
    logging.info(f"--- [Job Start] リマインドメール送信開始 (Drive Download): 宛先={to_email}, アップロード日時={upload_time} ---")
    # ... (以前提示した send_reminder_email の実装をここに記述) ...
    # 注意: get_credentials(), get_gdrive_service(), TEMP_FOLDER, MAIL_SENDER_NAME などが
    # このスクリプトのスコープで利用可能であること。
    # 成功したら True、失敗したら False を返すようにすると良いかもしれない。

    downloaded_temp_paths = []
    gdrive_service = None
    email_sent_successfully = False # 送信成否フラグ

    try:
        # --- 0. Google Drive サービス取得 ---
        gdrive_service = get_gdrive_service() # このスクリプト内で定義 or インポート
        if not gdrive_service:
            logging.error("[Job] Google Driveサービスへの接続に失敗しました。ファイル添付はスキップされます。")

        # --- 1. Google Driveからファイルをダウンロード ---
        if gdrive_service and file_details:
            logging.info("[Job] Google Driveからファイルのダウンロードを開始します...")
            for file_info in file_details:
                # ... (ダウンロード処理、downloaded_temp_pathsに追加) ...
                file_id = file_info.get('id')
                original_name = file_info.get('name')
                if not file_id or not original_name: continue
                temp_download_path = os.path.join(TEMP_FOLDER, f"downloaded_{file_id}_{original_name}")
                try:
                    request = gdrive_service.files().get_media(fileId=file_id)
                    fh = io.FileIO(temp_download_path, 'wb')
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while done is False: status, done = downloader.next_chunk()
                    fh.close()
                    downloaded_temp_paths.append(temp_download_path)
                except Exception as e_download:
                    logging.error(f"[Job] Driveダウンロードエラー (ID: {file_id}): {e_download}")

        # --- 2. Gmail API 認証情報取得 ---
        creds = get_credentials() # このスクリプト内で定義 or インポート
        if not creds: raise Exception("Failed to get credentials for Gmail API")
        gmail_service = build('gmail', 'v1', credentials=creds)

        # --- 3. メールの件名と本文を生成 ---
        subject = "あなたのタイムカプセルの開封日です"
        # upload_time は timezone aware であることを確認 (DBから取得時にそうなっているはず)
        elapsed_str = calculate_elapsed_period_simple(upload_time.astimezone(timezone.utc)) # UTC基準で計算するか、ローカルTZに変換
        upload_time_str = upload_time.strftime('%Y年%m月%d日 %H時%M分') # 必要ならローカルTZ表示
        attachment_names = [f.get('name', '不明') for f in file_details] if file_details else []
        # ... (メール本文 body の生成) ...
        body = f"""未来のあなたへ... (省略) ...""" # 以前のコードを参照

        # --- 4. メールメッセージの作成 ---
        message = MIMEMultipart()
        message['to'] = to_email
        message['from'] = Header(MAIL_SENDER_NAME, 'utf-8').encode()
        message['subject'] = Header(subject, 'utf-8')
        message.attach(MIMEText(body, 'plain', 'utf-8'))

        # --- 5. 添付ファイルの処理 ---
        for file_path in downloaded_temp_paths:
            # ... (添付処理) ...
            content_type, encoding = mimetypes.guess_type(file_path)
            if content_type is None or encoding is not None: content_type = 'application/octet-stream'
            main_type, sub_type = content_type.split('/', 1)
            with open(file_path, 'rb') as fp:
                part = MIMEBase(main_type, sub_type)
                part.set_payload(fp.read())
            encoders.encode_base64(part)
            filename = os.path.basename(file_path).split('_', 2)[-1]
            part.add_header('Content-Disposition', 'attachment', filename=Header(filename, 'utf-8').encode())
            message.attach(part)

        # --- 6. メールの送信 ---
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': raw_message}
        send_message = gmail_service.users().messages().send(userId='me', body=create_message).execute()
        logging.info(f"[Job] リマインドメールを {to_email} に送信しました。 Message ID: {send_message['id']}")
        email_sent_successfully = True # 送信成功

    except HttpError as error:
        logging.error(f"[Job] Gmail APIエラー: {error}")
    except Exception as e:
        logging.error(f"[Job] メール送信処理中に予期せぬエラー: {e}", exc_info=True)

    finally:
        # --- 7. ダウンロードした一時ファイルを削除 ---
        for fp in downloaded_temp_paths:
            if os.path.exists(fp):
                try: os.remove(fp)
                except Exception as e_rem: logging.error(f"[Job] 一時ファイル削除失敗: {fp}, Error: {e_rem}")
        logging.info(f"--- [Job End] リマインドメール送信処理終了: 宛先={to_email} ---")
        return email_sent_successfully # 成否を返す


def process_pending_reminders():
    logging.info("保留中のリマインダーの確認を開始...")
    db = SessionLocal()
    try:
        # 現在時刻 (UTC) を取得
        now_utc = datetime.now(timezone.utc)
        logging.info(f"現在時刻 (UTC): {now_utc}")

        # 送信対象のリマインダーを取得 (status='pending' かつ remind_at が過去または現在)
        # SQLAlchemy ORM を使う場合:
        # from test0320 import Reminder # Reminderモデルをインポート
        # pending_reminders = db.query(Reminder).filter(
        #     Reminder.status == 'pending',
        #     Reminder.remind_at <= now_utc
        # ).order_by(Reminder.remind_at).limit(10).all() # 一度に処理する数を制限 (limit)

        # SQLAlchemy Core (text) を使う場合:
        stmt = text("""
            SELECT id, remind_email, remind_at, message_body, gdrive_file_details, upload_time
            FROM reminders
            WHERE status = 'pending' AND remind_at <= :now
            ORDER BY remind_at
            LIMIT 10 -- 一度に処理する数を制限
        """)
        result = db.execute(stmt, {'now': now_utc})
        pending_reminders = result.fetchall() # [(id, email, ...), ...] のリスト

        logging.info(f"処理対象のリマインダー数: {len(pending_reminders)}")

        for reminder_data in pending_reminders:
            # ORMの場合: reminder = reminder_data
            # Coreの場合: reminder_id, email, remind_at_db, msg, details_json, upload_time_db = reminder_data
            reminder_id = reminder_data[0]
            email = reminder_data[1]
            remind_at_db = reminder_data[2] # DBから取得した日時は timezone aware のはず
            msg = reminder_data[3]
            details_json = reminder_data[4] # JSONBまたはTEXT(JSON文字列)
            upload_time_db = reminder_data[5] # DBから取得した日時は timezone aware のはず

            logging.info(f"リマインダー処理中: ID={reminder_id}, Email={email}, RemindAt={remind_at_db}")

            # gdrive_file_details をPythonリスト/辞書に戻す
            file_details = []
            if details_json:
                try:
                    # DBの型がJSONB/JSONなら自動でデシリアライズされる場合がある
                    if isinstance(details_json, (dict, list)):
                         file_details = details_json
                    else: # TEXT型の場合は明示的にパース
                        file_details = json.loads(details_json)
                except json.JSONDecodeError:
                    logging.error(f"リマインダーID {reminder_id}: gdrive_file_details のJSONパースに失敗しました。")
                    # パース失敗時の処理（ステータスをfailedにするなど）
                    update_stmt = text("UPDATE reminders SET status = 'failed', updated_at = NOW() WHERE id = :id")
                    db.execute(update_stmt, {'id': reminder_id})
                    db.commit() # 個別にコミット
                    continue # 次のリマインダーへ

            # メール送信関数を呼び出す
            try:
                # ★★★ send_reminder_email_with_download を呼び出す ★★★
                success = send_reminder_email_with_download(
                    to_email=email,
                    upload_time=upload_time_db, # DBから取得した upload_time
                    file_details=file_details,
                    message_body=msg
                )

                # 結果に基づいてステータスを更新
                new_status = 'sent' if success else 'failed'
                update_stmt = text("UPDATE reminders SET status = :status, updated_at = NOW() WHERE id = :id")
                db.execute(update_stmt, {'status': new_status, 'id': reminder_id})
                db.commit() # 個別にコミット
                logging.info(f"リマインダーID {reminder_id} のステータスを '{new_status}' に更新しました。")

            except Exception as e_send:
                logging.error(f"リマインダーID {reminder_id} のメール送信/更新処理中にエラー: {e_send}", exc_info=True)
                # エラー発生時も failed に更新 (リトライしない場合)
                try:
                    update_stmt = text("UPDATE reminders SET status = 'failed', updated_at = NOW() WHERE id = :id")
                    db.execute(update_stmt, {'id': reminder_id})
                    db.commit() # 個別にコミット
                except Exception as e_update_fail:
                     logging.error(f"リマインダーID {reminder_id} の failed へのステータス更新に失敗: {e_update_fail}")
                # ループは継続して他のリマインダーを試みる

    except Exception as e:
        logging.error(f"リマインダー処理全体でエラーが発生しました: {e}", exc_info=True)
        db.rollback() # ORM使用時など、トランザクション全体をロールバックする場合
    finally:
        db.close()
        logging.info("保留中のリマインダーの確認を終了。")

if __name__ == "__main__":
    logging.info("Cronジョブスクリプトを開始します...")
    # サービスアカウントファイルの存在チェック
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
         logging.error(f"★★★ 致命的エラー: サービスアカウントファイルが見つかりません: {SERVICE_ACCOUNT_FILE} ★★★")
         exit(1)
    process_pending_reminders()
    logging.info("Cronジョブスクリプトを終了します。")

