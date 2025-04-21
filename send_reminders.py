# send_reminders.py
import os
import logging
from datetime import datetime, timezone
import json
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import sessionmaker
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
from googleapiclient.discovery import build

# --- 共通関数や設定をインポート ---
# <<< 注意: 本来は test0320.py と共通の関数/定数は common_utils.py などに切り出すべき >>>
# ここでは、test0320.py が実行環境に存在し、そこからインポートできることを前提とする
# 循環参照を避けるため、test0320.py 側でこのファイルをインポートしないように注意
try:
    from test0320 import (
        get_credentials,
        get_gdrive_service,
        calculate_elapsed_period_simple,
        SERVICE_ACCOUNT_FILE, # 定数もインポート
        SCOPES,             # 定数もインポート
        MAIL_SENDER_NAME    # 定数もインポート
    )
except ImportError as e:
     logging.error(f"test0320.py からのインポートに失敗: {e}")
     logging.error("共通関数/定数が利用できません。処理を続行できません。")
     # 実行を停止するか、エラー処理を行う
     exit(1) # スクリプトの実行を停止

# --- ロギング設定 ---
# ログフォーマットに [CronJob] を含めると分かりやすい
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [CronJob] - %(message)s')

# --- データベース接続設定 ---
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    logging.error("★★★ 致命的エラー: 環境変数 DATABASE_URL が設定されていません ★★★")
    exit(1) # 実行停止

# <<< 修正: DBエンジンとセッションメーカーをここで作成 >>>
# test0320.py と同じ設定で作成する
try:
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
except Exception as e:
    logging.error(f"データベースエンジン/セッションの作成に失敗: {e}")
    exit(1)

# --- 一時フォルダ (メール添付用) ---
TEMP_FOLDER = '/tmp/timecapsule_downloads' # Render環境で書き込み可能なパス
try:
    os.makedirs(TEMP_FOLDER, exist_ok=True)
except OSError as e:
    logging.error(f"一時フォルダの作成に失敗: {TEMP_FOLDER}, エラー: {e}")
    # フォルダがなくても処理を試みるか、停止するか検討
    # exit(1)

# --- メール送信関数 (Driveダウンロード版) ---
def send_reminder_email_with_download(to_email, upload_time, file_details, message_body=''):
    """
    指定されたメールアドレスにリマインドメールを送信する (Gmail API, Driveダウンロード)。
    Args:
        to_email (str): 送信先メールアドレス
        upload_time (datetime): 元のアップロード日時 (timezone aware)
        file_details (list): Google Driveのファイル情報リスト [{'id': '...', 'name': '...'}, ...]
        message_body (str): ユーザーからのメッセージ
    Returns:
        bool: 送信に成功した場合は True, 失敗した場合は False
    """
    logging.info(f"--- [Job Start] リマインドメール送信開始 (Drive Download): 宛先={to_email}, アップロード日時={upload_time} ---")
    downloaded_temp_paths = []
    gdrive_service = None
    email_sent_successfully = False # 送信成否フラグ

    try:
        # --- 0. Google Drive サービス取得 ---
        # <<< 修正: インポートした関数を使用 >>>
        gdrive_service = get_gdrive_service()
        if not gdrive_service:
            # エラーログは get_gdrive_service 内で出力されるはず
            logging.error("[Job] Google Driveサービスへの接続に失敗しました。ファイル添付はスキップされます。")
            # Driveサービスがなくてもメール送信は試みる

        # --- 1. Google Driveからファイルをダウンロード ---
        if gdrive_service and file_details:
            logging.info(f"[Job] Google Driveから {len(file_details)} 個のファイルのダウンロードを開始します...")
            for file_info in file_details:
                file_id = file_info.get('id')
                original_name = file_info.get('name')
                if not file_id or not original_name:
                    logging.warning(f"[Job] 無効なファイル情報です: {file_info}")
                    continue

                # <<< 修正: 一時ファイル名を一意にする >>>
                safe_original_name = "".join(c if c.isalnum() or c in ('-', '_', '.') else '_' for c in original_name) # 安全なファイル名に
                temp_download_path = os.path.join(TEMP_FOLDER, f"downloaded_{datetime.now().timestamp()}_{safe_original_name}")

                try:
                    logging.info(f"[Job] Downloading '{original_name}' (ID: {file_id}) to {temp_download_path}...")
                    request = gdrive_service.files().get_media(fileId=file_id)
                    # <<< 修正: io.BytesIO を使うとメモリ使用量が増える可能性があるため FileIO のまま >>>
                    fh = io.FileIO(temp_download_path, 'wb')
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while done is False:
                        status, done = downloader.next_chunk()
                        logging.debug(f"[Job] Download {original_name}: {int(status.progress() * 100)}%.")
                    fh.close()
                    downloaded_temp_paths.append(temp_download_path)
                    logging.info(f"[Job] Downloaded '{original_name}' successfully.")
                except HttpError as e_download_http:
                     logging.error(f"[Job] DriveダウンロードHTTPエラー (ID: {file_id}, Name: {original_name}): {e_download_http}")
                     # エラーが発生しても、ダウンロードできたファイルだけでメール送信を試みる
                except Exception as e_download:
                    logging.error(f"[Job] Driveダウンロード一般エラー (ID: {file_id}, Name: {original_name}): {e_download}", exc_info=True)

        # --- 2. Gmail API 認証情報取得 ---
        # <<< 修正: インポートした関数を使用 >>>
        creds = get_credentials()
        if not creds:
            # エラーログは get_credentials 内で出力されるはず
            raise Exception("Failed to get credentials for Gmail API")
        gmail_service = build('gmail', 'v1', credentials=creds)

        # --- 3. メールの件名と本文を生成 ---
        subject = "あなたのタイムカプセルの開封日です"
        # upload_time は timezone aware である想定
        elapsed_str = calculate_elapsed_period_simple(upload_time) # <<< 修正: インポートした関数を使用 >>>
        # 必要に応じてローカルタイムゾーンに変換して表示
        try:
            local_tz = datetime.now().astimezone().tzinfo # サーバーのローカルTZを取得
            upload_time_local = upload_time.astimezone(local_tz)
            upload_time_str = upload_time_local.strftime('%Y年%m月%d日 %H時%M分')
        except Exception: # ローカルTZ取得失敗など
             upload_time_str = upload_time.strftime('%Y-%m-%d %H:%M %Z') # UTCまたは元のTZで表示

        attachment_names = [os.path.basename(fp) for fp in downloaded_temp_paths] # ダウンロード成功したファイルのみ
        message_section = ""
        if message_body and message_body.strip():
            message_section = f"""
--- あの日のあなたからのメッセージ ---
{message_body.strip()}
------------------------------------
"""
        body = f"""未来のあなたへ

託したタイムカプセルが、本日、開封予定日を迎えましたことをお知らせいたします。

タイムカプセルに大切な何かを保管してから、{elapsed_str}という時間が流れました。
あの時思い描いた未来は、今、どのように実現しているでしょうか。

もしかしたら、忘れていた夢や目標が、このタイムカプセルの中に眠っているかもしれません。

ぜひ添付ファイルを開いて、未来の自分へのメッセージ、そして{upload_time_str}の自分との再会を果たしてください。
この特別な瞬間が、あなたの未来を輝かせるきっかけの１つとなりますように。

---
カプセルに入っていたもの:
{', '.join([f.get('name', '不明') for f in file_details]) if file_details else '(ファイルなし)'}
{message_section}

From: {MAIL_SENDER_NAME}
""" # <<< 修正: インポートした定数を使用 >>>

        # --- 4. メールメッセージの作成 ---
        message = MIMEMultipart()
        message['to'] = to_email
        message['from'] = Header(MAIL_SENDER_NAME, 'utf-8').encode() # <<< 修正: インポートした定数を使用 >>>
        message['subject'] = Header(subject, 'utf-8')
        message.attach(MIMEText(body, 'plain', 'utf-8'))

        # --- 5. 添付ファイルの処理 ---
        if downloaded_temp_paths:
             logging.info(f"[Job] {len(downloaded_temp_paths)} 個のファイルをメールに添付します...")
        for file_path in downloaded_temp_paths:
            content_type, encoding = mimetypes.guess_type(file_path)
            if content_type is None or encoding is not None:
                content_type = 'application/octet-stream'
            main_type, sub_type = content_type.split('/', 1)
            try:
                with open(file_path, 'rb') as fp:
                    part = MIMEBase(main_type, sub_type)
                    part.set_payload(fp.read())
                encoders.encode_base64(part)
                # <<< 修正: Drive上の元のファイル名を使う >>>
                # ファイルパスから元の名前を復元するのは困難なため、file_details と照合するか、
                # ダウンロード時にファイル名情報を保持する必要がある。
                # ここでは一時ファイル名からプレフィックスを除去して使う（不完全な場合あり）
                filename = os.path.basename(file_path)
                if filename.startswith("downloaded_"):
                     filename = filename.split('_', 2)[-1] # タイムスタンプ部分を除去

                part.add_header('Content-Disposition', 'attachment', filename=Header(filename, 'utf-8').encode())
                message.attach(part)
                logging.info(f"[Job] ファイル '{filename}' をメールに添付しました。")
            except Exception as e_attach:
                 logging.error(f"[Job] ファイル添付エラー ({file_path}): {e_attach}", exc_info=True)

        # --- 6. メールの送信 ---
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': raw_message}
        logging.info(f"[Job] Gmail API を使用してメールを送信します (userId='me')...")
        send_message = gmail_service.users().messages().send(userId='me', body=create_message).execute()
        logging.info(f"[Job] リマインドメールを {to_email} に送信しました。 Message ID: {send_message['id']}")
        email_sent_successfully = True # 送信成功

    except HttpError as error:
        logging.error(f"[Job] Gmail APIエラー: {error}")
        # エラーの詳細を出力
        try:
            error_details = json.loads(error.content.decode())
            logging.error(f"[Job] Gmail APIエラー詳細: {error_details}")
        except:
            logging.error(f"[Job] Gmail APIエラー内容 (raw): {error.content}")
        # 権限エラー(403)の場合のメッセージ
        if error.resp.status == 403:
            logging.error("[Job] 権限エラー(403): サービスアカウントに必要な権限がないか、ドメイン全体の委任が必要な可能性があります。")
    except Exception as e:
        logging.error(f"[Job] メール送信処理中に予期せぬエラー: {e}", exc_info=True)

    finally:
        # --- 7. ダウンロードした一時ファイルを削除 ---
        logging.info(f"[Job] 一時ファイル ({len(downloaded_temp_paths)}個) の削除を試みます...")
        for fp in downloaded_temp_paths:
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                    logging.info(f"[Job] 一時ファイル削除成功: {fp}")
                except Exception as e_rem:
                    logging.error(f"[Job] 一時ファイル削除失敗: {fp}, Error: {e_rem}")
        logging.info(f"--- [Job End] リマインドメール送信処理終了: 宛先={to_email}, 成功={email_sent_successfully} ---")
        return email_sent_successfully # 成否を返す


def process_pending_reminders():
    """保留中のリマインダーを確認し、条件を満たすものについてメール送信処理を行う"""
    logging.info("--- 保留中のリマインダーの確認を開始 ---")
    db = None # finally で close するために外で定義
    processed_count = 0
    error_count = 0
    try:
        db = SessionLocal()
        # 現在時刻 (UTC) を取得
        now_utc = datetime.now(timezone.utc)
        logging.info(f"現在時刻 (UTC): {now_utc}")

        # 送信対象のリマインダーを取得 (status='pending' かつ remind_at が過去または現在)
        # SQLAlchemy Core (text) を使用
        stmt = text("""
            SELECT id, remind_email, remind_at, message_body, gdrive_file_details, upload_time
            FROM reminders
            WHERE status = 'pending' AND remind_at <= :now
            ORDER BY remind_at -- 古いものから処理
            LIMIT 10 -- 一度に処理する数を制限 (負荷軽減のため)
        """)
        result = db.execute(stmt, {'now': now_utc})
        pending_reminders = result.fetchall() # [(id, email, ...), ...] のリスト

        if not pending_reminders:
            logging.info("処理対象の保留中リマインダーはありません。")
            return # 処理対象がなければ終了

        logging.info(f"処理対象のリマインダー数: {len(pending_reminders)}")

        for reminder_data in pending_reminders:
            reminder_id, email, remind_at_db, msg, details_json, upload_time_db = reminder_data
            logging.info(f"--- リマインダー処理開始: ID={reminder_id}, Email={email}, RemindAt={remind_at_db} ---")

            # gdrive_file_details をPythonリスト/辞書に戻す
            file_details = []
            if details_json:
                try:
                    # DBの型がJSONB/JSONなら自動でデシリアライズされる場合がある
                    if isinstance(details_json, (dict, list)):
                         file_details = details_json
                    elif isinstance(details_json, str): # TEXT型の場合は明示的にパース
                        file_details = json.loads(details_json)
                    else:
                         logging.warning(f"リマインダーID {reminder_id}: 予期しない gdrive_file_details の型です: {type(details_json)}")
                         # 型が不明な場合は空リストとして扱うか、エラーにするか検討
                         file_details = []
                except json.JSONDecodeError as e_json:
                    logging.error(f"リマインダーID {reminder_id}: gdrive_file_details のJSONパースに失敗: {e_json}")
                    # パース失敗時はステータスを 'failed' に更新して次へ
                    try:
                        update_stmt = text("UPDATE reminders SET status = 'failed', updated_at = NOW() WHERE id = :id")
                        db.execute(update_stmt, {'id': reminder_id})
                        db.commit() # 個別にコミット
                        error_count += 1
                    except Exception as e_update_fail:
                         logging.error(f"リマインダーID {reminder_id} の failed へのステータス更新に失敗 (JSONパースエラー後): {e_update_fail}")
                    continue # 次のリマインダーへ

            # メール送信関数を呼び出す
            try:
                # upload_time_db と remind_at_db が timezone aware であることを確認
                if upload_time_db.tzinfo is None or remind_at_db.tzinfo is None:
                     logging.warning(f"リマインダーID {reminder_id}: DBから取得した日時にタイムゾーン情報がありません。UTCと仮定します。")
                     upload_time_db = upload_time_db.replace(tzinfo=timezone.utc)
                     remind_at_db = remind_at_db.replace(tzinfo=timezone.utc)

                success = send_reminder_email_with_download(
                    to_email=email,
                    upload_time=upload_time_db,
                    file_details=file_details,
                    message_body=msg
                )

                # 結果に基づいてステータスを更新
                new_status = 'sent' if success else 'failed'
                update_stmt = text("UPDATE reminders SET status = :status, updated_at = NOW() WHERE id = :id")
                db.execute(update_stmt, {'status': new_status, 'id': reminder_id})
                db.commit() # 個別にコミット
                logging.info(f"リマインダーID {reminder_id} のステータスを '{new_status}' に更新しました。")
                if success:
                    processed_count += 1
                else:
                    error_count += 1

            except Exception as e_send:
                logging.error(f"リマインダーID {reminder_id} のメール送信/更新処理中にエラー: {e_send}", exc_info=True)
                error_count += 1
                # エラー発生時も failed に更新 (リトライしない場合)
                try:
                    # ロールバックされている可能性があるので、再接続や再試行が必要な場合があるが、
                    # ここでは単純に更新を試みる
                    update_stmt = text("UPDATE reminders SET status = 'failed', updated_at = NOW() WHERE id = :id")
                    # セッションが有効か確認してから実行する方が安全
                    if db.is_active:
                         db.execute(update_stmt, {'id': reminder_id})
                         db.commit() # 個別にコミット
                    else:
                         logging.warning(f"リマインダーID {reminder_id}: DBセッションが無効なため、failedへの更新をスキップします。")

                except Exception as e_update_fail:
                     logging.error(f"リマインダーID {reminder_id} の failed へのステータス更新に失敗 (送信エラー後): {e_update_fail}")
                # ループは継続して他のリマインダーを試みる

            logging.info(f"--- リマインダー処理終了: ID={reminder_id} ---")

    except Exception as e:
        logging.error(f"リマインダー処理全体でエラーが発生しました: {e}", exc_info=True)
        if db and db.is_active:
            try:
                db.rollback() # エラー発生時はトランザクション全体をロールバック（ただし個別にコミットしている場合は影響範囲が限定的）
                logging.info("データベーストランザクションをロールバックしました。")
            except Exception as e_rollback:
                 logging.error(f"データベースのロールバック中にエラー: {e_rollback}")
    finally:
        if db:
            db.close()
            logging.info("データベース接続をクローズしました。")
        logging.info(f"--- 保留中のリマインダーの確認を終了。処理済み: {processed_count}件, エラー: {error_count}件 ---")

# --- スクリプトとして直接実行された場合の処理 ---
if __name__ == "__main__":
    # このブロックは、`python send_reminders.py` のように直接実行された場合にのみ動作します。
    # Flaskアプリの `/run-cron` エンドポイントから呼び出される場合は、このブロックは実行されません。
    # ローカルでのテストや、Render以外の環境でCronから直接このスクリプトを実行する場合に使用します。
    logging.info("--- Cronジョブスクリプト (send_reminders.py) を直接実行モードで開始します ---")

    # 必要な環境変数やファイルのチェック
    if not DATABASE_URL:
        logging.error("環境変数 DATABASE_URL が未設定のため、処理を実行できません。")
        exit(1)
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
         logging.error(f"サービスアカウントファイルが見つかりません: {SERVICE_ACCOUNT_FILE}")
         exit(1)

    # リマインダー処理関数を実行
    process_pending_reminders()

    logging.info("--- Cronジョブスクリプト (send_reminders.py) を直接実行モードで終了します ---")

