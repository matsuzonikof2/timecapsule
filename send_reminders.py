# send_reminders.py (修正後)
import os
import logging
from datetime import datetime, timezone
import json
from sqlalchemy import text, update # create_engine, sessionmaker は削除
import io
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError


import mimetypes
import base64
from googleapiclient.discovery import build # build は common_utils にも必要
from zoneinfo import ZoneInfo
# ★★★ Mailjet ライブラリをインポート ★★★
from mailjet_rest import Client


# --- common_utils からインポート ---
from common_utils import (
    get_credentials, get_gdrive_service, calculate_elapsed_period_simple,
    SERVICE_ACCOUNT_FILE, SCOPES, MAIL_SENDER_NAME, DATABASE_URL,
    SessionLocal, engine, # engine, SessionLocal をインポート
    TEMP_DOWNLOAD_FOLDER, # ダウンロード用一時フォルダ
    # ★★★ Mailjet 用の環境変数をインポート ★★★
    MAILJET_API_KEY, MAILJET_SECRET_KEY, MAIL_FROM_EMAIL
)
# ★★★ APIキー等のチェック ★★★
if not MAILJET_API_KEY or not MAILJET_SECRET_KEY or not MAIL_FROM_EMAIL:
    logging.error("[Job] Mailjet APIキー、シークレットキー、または送信元メールアドレスが未設定です。メール送信できません。")

# --- ロギング設定 (common_utils で設定済みなら不要かも) ---
# ログフォーマットを少し変更して、どのプロセスからのログか分かりやすくする
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [CronJob] - %(message)s')

# --- DB接続チェック ---
if not engine or not SessionLocal:
     logging.error("★★★ 致命的エラー: データベース接続が初期化されていません (common_utilsを確認) ★★★")
     exit(1) # CronジョブはDBないと実行不可

# --- メール送信関数 (Driveダウンロード版) ---
# ★★★ 元の関数名に戻し、機能を復元 ★★★
def send_reminder_email_with_download(to_email, upload_time, file_details, message_body=''):
    logging.info(f"--- [Job Start] リマインドメール送信開始 (Drive Download): 宛先={to_email}, アップロード日時={upload_time} ---")
    downloaded_temp_paths = []
    gdrive_service = None
    email_sent_successfully = False

    try:
        # --- 0. Google Drive サービス取得 ---
        gdrive_service = get_gdrive_service() # common_utils からインポート
        if not gdrive_service: 
            logging.error("[Job] Google Driveサービスへの接続に失敗しました。ファイル添付はスキップされます。")

        # --- 1. Google Driveからファイルをダウンロード ---
        if gdrive_service and file_details:
            logging.info(f"[Job] Google Driveから {len(file_details)} 個のファイルのダウンロードを開始します...")
            # ★★★ TEMP_DOWNLOAD_FOLDER の存在確認を追加 ★★★
            if not os.path.exists(TEMP_DOWNLOAD_FOLDER):
                try:
                    os.makedirs(TEMP_DOWNLOAD_FOLDER)
                    logging.info(f"[Job] 一時ダウンロードフォルダを作成しました: {TEMP_DOWNLOAD_FOLDER}")
                except OSError as e:
                    logging.error(f"[Job] 一時ダウンロードフォルダの作成に失敗しました: {TEMP_DOWNLOAD_FOLDER}, Error: {e}")
                    # フォルダがなければダウンロードはできないので、処理を続けるが、ファイルは添付されない
                    gdrive_service = None # ダウンロード処理をスキップさせる

            if gdrive_service: # フォルダ作成失敗などで None になっている可能性を考慮
                for file_info in file_details:
                    file_id = file_info.get('id')
                    original_name = file_info.get('name')
                    if not file_id or not original_name:
                        logging.warning(f"[Job] 無効なファイル情報のためスキップ: {file_info}")
                        continue
                    # ファイル名に使用できない文字を置換
                    safe_original_name = "".join(c if c.isalnum() or c in ('-', '_', '.') else '_' for c in original_name)
                    temp_download_path = os.path.join(TEMP_DOWNLOAD_FOLDER, f"downloaded_{datetime.now().timestamp()}_{safe_original_name}")
                    logging.info(f"[Job] ダウンロード試行: ID={file_id}, Name='{original_name}', TempPath='{temp_download_path}'")
                    try:
                        request = gdrive_service.files().get_media(fileId=file_id)
                        # ★★★ 'wb' モードで開く ★★★
                        with io.FileIO(temp_download_path, 'wb') as fh:
                            downloader = MediaIoBaseDownload(fh, request)
                            done = False
                            while done is False:
                                status, done = downloader.next_chunk()
                                if status:
                                    logging.info(f"[Job] Download {int(status.progress() * 100)}% for {original_name}.")
                        downloaded_temp_paths.append(temp_download_path)
                        logging.info(f"[Job] ファイルを一時保存しました: {temp_download_path}")
                    except HttpError as e_download_http:
                         logging.error(f"[Job] DriveダウンロードHTTPエラー (ID: {file_id}, Name: {original_name}): {e_download_http}")
                         # エラー詳細を出力
                         try:
                             error_details = json.loads(e_download_http.content.decode())
                             logging.error(f"[Job] DriveダウンロードHTTPエラー詳細: {error_details}")
                         except:
                             logging.error(f"[Job] DriveダウンロードHTTPエラー内容 (raw): {e_download_http.content}")
                    except Exception as e_download:
                        logging.error(f"[Job] Driveダウンロード一般エラー (ID: {file_id}, Name: {original_name}): {e_download}", exc_info=True)

        # # --- 2. Gmail API 認証情報取得 ---
        # creds = get_credentials() # common_utils からインポート
        # if not creds:
        #     raise Exception("Failed to get credentials for Gmail API")
        # gmail_service = build('gmail', 'v1', credentials=creds)

        # --- 3. メールの件名と本文を生成 ---
        subject = "あなたのタイムカプセルの開封日です"
        elapsed_str = calculate_elapsed_period_simple(upload_time) # common_utils からインポート
        try:
            jst = ZoneInfo("Asia/Tokyo")
            upload_time_jst = upload_time.astimezone(jst)
            # ★★★ upload_time_local -> upload_time_jst に修正 ★★★
            upload_time_str = upload_time_jst.strftime('%Y年%m月%d日 %H時%M分')
        except Exception as e_tz_fmt:
            logging.warning(f"[Job] JSTへの変換またはフォーマット中にエラー: {e_tz_fmt}. UTCで表示します。")
            # タイムゾーン情報がない場合も考慮してフォーマット
            upload_time_str = upload_time.strftime('%Y-%m-%d %H:%M (%Z)') if upload_time.tzinfo else upload_time.strftime('%Y-%m-%d %H:%M (Unknown TZ)')


        message_section = f"\n--- あの日のあなたからのメッセージ ---\n{message_body.strip()}\n------------------------------------\n" if message_body and message_body.strip() else ""

        # ★★★ 本文を修正 (以前のコードを参照、とあった部分を具体化) ★★★
        body = f"""未来のあなたへ

ついにこの日がやってきましたね！
あなたがこのタイムカプセルを準備したのは {upload_time_str} ({elapsed_str}前) のこと。

どんな気持ちでこのカプセルを開封していますか？
過去のあなたが託した想いやファイルが、現在のあなたにとって素敵な贈り物となりますように。

{message_section}
添付ファイルをご確認ください。

From: {MAIL_SENDER_NAME}
"""

          # --- 4. Mailjet API リクエストデータの準備 ---
        mailjet_data = {
            'Messages': [
                {
                    "From": {
                        "Email": MAIL_FROM_EMAIL,
                        "Name": MAIL_SENDER_NAME
                    },
                    "To": [
                        {
                            "Email": to_email
                            # "Name": "Recipient Name" # 必要なら
                        }
                    ],
                    "Subject": subject,
                    "TextPart": body,
                    # "HTMLPart": "<h3>HTML version...</h3>" # HTMLメールの場合
                    "Attachments": [] # 添付ファイルは後で追加
                }
            ]
        }
        # --- 5. 添付ファイルの処理 (Mailjet 用) ---
        if downloaded_temp_paths:
            logging.info(f"[Job] {len(downloaded_temp_paths)} 個のファイルをメールに添付します (Mailjet)...")
            attachments_data = []
            for file_path in downloaded_temp_paths:
                if not os.path.exists(file_path):
                    logging.error(f"[Job] 添付予定のファイルが見つかりません: {file_path}")
                    continue
                try:
                    filename = os.path.basename(file_path)
                    # プレフィックス除去
                    if filename.startswith("downloaded_"):
                        try: filename = filename.split('_', 2)[-1]
                        except IndexError: logging.warning(f"[Job] 添付ファイル名のプレフィックス除去に失敗: {filename}")

                    content_type, _ = mimetypes.guess_type(file_path)
                    if content_type is None: content_type = 'application/octet-stream'

                    with open(file_path, 'rb') as f:
                        file_data = f.read()
                    base64_content = base64.b64encode(file_data).decode('utf-8')

                    attachments_data.append({
                        "ContentType": content_type,
                        "Filename": filename,
                        "Base64Content": base64_content
                    })
                    logging.info(f"[Job] ファイルを添付準備 (Mailjet): {filename} (Type: {content_type})")

                except FileNotFoundError:
                     logging.error(f"[Job] 添付ファイルを開けません (削除された可能性): {file_path}")
                except Exception as e_attach:
                    logging.error(f"[Job] ファイル添付準備エラー (Mailjet) ({file_path}): {e_attach}", exc_info=True)

            if attachments_data:
                mailjet_data['Messages'][0]['Attachments'] = attachments_data

        # --- 6. メールの送信 (Mailjet) ---
        try:
            # Mailjet クライアント初期化
            mailjet = Client(auth=(MAILJET_API_KEY, MAILJET_SECRET_KEY), version='v3.1')

            logging.info(f"[Job] Mailjet API を使用してメールを送信します (From: {MAIL_FROM_EMAIL}, To: {to_email})...")
            result = mailjet.send.create(data=mailjet_data)

            logging.info(f"[Job] Mailjet 応答ステータスコード: {result.status_code}")
            # logging.debug(f"[Job] Mailjet 応答内容: {result.json()}") # 必要なら詳細ログ

            # Mailjet は成功時 200 OK を返す
            if result.status_code == 200:
                # さらに詳細なステータスを確認 (例: 各メッセージのステータス)
                response_json = result.json()
                message_status = response_json.get('Messages', [{}])[0].get('Status')
                if message_status == 'success':
                    logging.info(f"[Job] リマインドメールを {to_email} に送信しました (Mailjet)。")
                    email_sent_successfully = True
                else:
                    # API呼び出しは成功したが、メッセージ処理で問題があった場合
                    logging.error(f"[Job] Mailjet メッセージ処理ステータスが success ではありません: {message_status}")
                    logging.error(f"[Job] Mailjet 応答詳細: {json.dumps(response_json, indent=2)}")
            else:
                # API呼び出し自体が失敗した場合
                logging.error(f"[Job] Mailjet でのエラー応答: Status={result.status_code}")
                try:
                    error_details = result.json()
                    logging.error(f"[Job] Mailjet エラー詳細: {json.dumps(error_details, indent=2)}")
                except json.JSONDecodeError:
                    logging.error(f"[Job] Mailjet エラー内容 (非JSON): {result.text}")

        except Exception as e_mailjet:
            # Mailjet API 呼び出し中のエラー (ネットワークエラー、ライブラリエラーなど)
            logging.error(f"[Job] Mailjet API 呼び出し中にエラー: {e_mailjet}", exc_info=True)

    except Exception as e:
        logging.error(f"[Job] メール送信処理中に予期せぬエラー: {e}", exc_info=True)
    finally:
        # --- 7. ダウンロードした一時ファイルを削除 (変更なし) ---
        logging.info(f"[Job] 一時ダウンロードファイル ({len(downloaded_temp_paths)}個) の削除を開始...")
        # ... (削除処理) ...
        logging.info(f"--- [Job End] リマインドメール送信処理終了 (Mailjet): 宛先={to_email}, 成功={email_sent_successfully} ---")
        return email_sent_successfully

# --- 保留中のリマインダー処理関数 ---
def process_pending_reminders():
    logging.info("--- 保留中のリマインダーの確認を開始 ---")
    db = None
    processed_count = 0
    error_count = 0
    try:
        db = SessionLocal() # common_utils からインポート
        now_utc = datetime.now(timezone.utc)
        logging.info(f"現在時刻 (UTC): {now_utc}")
        # status が 'pending' で、remind_at が現在時刻以前のものを取得
        stmt = text("""
            SELECT id, remind_email, remind_at, message_body, gdrive_file_details, upload_time
            FROM reminders
            WHERE status = 'pending' AND remind_at <= :now
            ORDER BY remind_at
            LIMIT 10
        """)
        result = db.execute(stmt, {'now': now_utc})
        pending_reminders = result.fetchall()

        if not pending_reminders:
            logging.info("処理対象の保留中リマインダーはありません。")
            return

        logging.info(f"処理対象のリマインダー数: {len(pending_reminders)}")

        for reminder_data in pending_reminders:
            # reminder_data は Row オブジェクトなので、名前でアクセス可能
            reminder_id = reminder_data.id
            email = reminder_data.remind_email
            remind_at_db = reminder_data.remind_at
            msg = reminder_data.message_body
            details_json = reminder_data.gdrive_file_details
            upload_time_db = reminder_data.upload_time

            logging.info(f"--- リマインダー処理開始: ID={reminder_id}, Email={email}, RemindAt={remind_at_db} ---")
            file_details = []
            if details_json:
                try:
                    # DBから取得した時点で dict or list になっているはず (JSONB型の場合)
                    if isinstance(details_json, (dict, list)):
                        file_details = details_json
                    elif isinstance(details_json, str): # 文字列で格納されている場合 (Text型など)
                        file_details = json.loads(details_json)
                        logging.warning(f"リマインダーID {reminder_id}: gdrive_file_details が文字列で格納されていました。JSONとしてパースしました。")
                    else:
                        logging.warning(f"リマインダーID {reminder_id}: 予期しない gdrive_file_details の型: {type(details_json)}")
                except json.JSONDecodeError as e_json:
                    logging.error(f"リマインダーID {reminder_id}: gdrive_file_details のJSONパースに失敗: {e_json}")
                    # JSONパース失敗は致命的なので failed にする
                    try:
                        update_stmt = text("UPDATE reminders SET status = 'failed', updated_at = NOW() WHERE id = :id")
                        db.execute(update_stmt, {'id': reminder_id})
                        db.commit()
                        error_count += 1
                        logging.info(f"リマインダーID {reminder_id} のステータスを 'failed' に更新しました (JSONパースエラー)。")
                    except Exception as e_update_fail:
                        logging.error(f"リマインダーID {reminder_id} の failed へのステータス更新に失敗 (JSONパースエラー後): {e_update_fail}")
                        db.rollback() # ロールバックしておく
                    continue # 次のリマインダーへ

            try:
                # DBから取得した日時にタイムゾーン情報があるか確認
                if upload_time_db.tzinfo is None:
                     logging.warning(f"リマインダーID {reminder_id}: DBの upload_time にタイムゾーン情報なし。UTCと仮定します。")
                     upload_time_db = upload_time_db.replace(tzinfo=timezone.utc)
                if remind_at_db.tzinfo is None:
                     logging.warning(f"リマインダーID {reminder_id}: DBの remind_at にタイムゾーン情報なし。UTCと仮定します。")
                     remind_at_db = remind_at_db.replace(tzinfo=timezone.utc)

                # メール送信関数を呼び出す
                success = send_reminder_email_with_download(email, upload_time_db, file_details, msg)

                # 結果に基づいてステータスを更新
                new_status = 'sent' if success else 'failed'
                update_stmt = text("UPDATE reminders SET status = :status, updated_at = NOW() WHERE id = :id")
                db.execute(update_stmt, {'status': new_status, 'id': reminder_id})
                db.commit() # 各リマインダーごとにコミット
                logging.info(f"リマインダーID {reminder_id} のステータスを '{new_status}' に更新しました。")
                if success:
                    processed_count += 1
                else:
                    error_count += 1

            except Exception as e_send:
                logging.error(f"リマインダーID {reminder_id} のメール送信/更新処理中にエラー: {e_send}", exc_info=True)
                error_count += 1
                # エラーが発生した場合でも、DB更新を試みる (failed にする)
                try:
                    # ロールバックしてから更新
                    db.rollback()
                    update_stmt = text("UPDATE reminders SET status = 'failed', updated_at = NOW() WHERE id = :id")
                    db.execute(update_stmt, {'id': reminder_id})
                    db.commit()
                    logging.info(f"リマインダーID {reminder_id} のステータスを 'failed' に更新しました (送信/更新エラー後)。")
                except Exception as e_update_fail:
                    logging.error(f"リマインダーID {reminder_id} の failed へのステータス更新に失敗 (送信エラー後): {e_update_fail}")
                    # ここでさらにロールバックが必要か？セッションの状態による
                    try:
                        db.rollback()
                    except Exception as e_rollback_inner:
                         logging.error(f"内部ロールバックエラー: {e_rollback_inner}")

            logging.info(f"--- リマインダー処理終了: ID={reminder_id} ---")

    except Exception as e:
        logging.error(f"リマインダー処理全体でエラーが発生しました: {e}", exc_info=True)
        if db and db.is_active:
            try:
                db.rollback() # 全体エラーの場合はロールバック
                logging.info("全体エラーのためDB変更をロールバックしました。")
            except Exception as e_rollback:
                logging.error(f"DBロールバックエラー: {e_rollback}")
    finally:
        if db:
            db.close() # セッションを閉じる
            logging.info("データベースセッションを閉じました。")
        logging.info(f"--- 保留中のリマインダーの確認を終了。処理済み: {processed_count}件, エラー: {error_count}件 ---")

# --- スクリプト直接実行時の処理 (変更なし) ---
if __name__ == "__main__":
    logging.info("--- Cronジョブスクリプト (send_reminders.py) を直接実行モードで開始します ---")
    # 必要な環境変数のチェック
    if not DATABASE_URL:
        logging.error("★★★ 致命的エラー: 環境変数 DATABASE_URL が設定されていません。 ★★★")
        exit(1)
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        logging.error(f"★★★ 致命的エラー: サービスアカウントファイルが見つかりません: {SERVICE_ACCOUNT_FILE} ★★★")
        exit(1)
    # common_utils.py でチェック済みなので、ここでは主要なものだけ確認するか、省略しても良い
    if not MAILJET_API_KEY or not MAILJET_SECRET_KEY or not MAIL_FROM_EMAIL:
        logging.warning("★★★ 警告: Mailjet関連の環境変数が不足しています。common_utils.py の警告を確認してください。 ★★★")
        # exit(1) # 必要ならここで終了させる
    # 処理実行
    process_pending_reminders()

    logging.info("--- Cronジョブスクリプト (send_reminders.py) を直接実行モードで終了します ---")
