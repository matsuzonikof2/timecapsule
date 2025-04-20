# test0320.py (サービスアカウント認証版)
from flask import Flask, request, jsonify, redirect, url_for, render_template
import os
import pickle
from datetime import datetime, timedelta
import smtplib
import base64 # Base64エンコードのために追加
from email.mime.text import MIMEText
from email.header import Header
# --- メール添付に必要なモジュールを追加 ---
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import mimetypes # MIMEタイプ判別用
import logging #ログ出力を強化
import time # timeモジュールをインポート
from sqlalchemy import create_engine, text
import redis
import sys
from rq import Queue
from redis import Redis
import io
# Google Drive API関連
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError # エラーハンドリング用
# --- サービスアカウント認証用のモジュール ---
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# スケジューラ関連
from flask_apscheduler import APScheduler
#from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.jobstores.redis import RedisJobStore

# --- 設定 ---
#APSchedulerのデバッグログを有効化して内部動作に関する詳細ログを出力しどこに待機が発生するか把握
logging.getLogger('apscheduler').setLevel(logging.DEBUG)
# Flaskアプリケーションのインスタンスを作成
app = Flask(__name__, static_folder='.', static_url_path='')
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'default-secret-key') # 環境変数から取得推奨
# --- サービスアカウントキーファイルのパス (Render Secret Filesで設定したパス) ---
SERVICE_ACCOUNT_FILE = '/etc/secrets/service_account.json' # ★★★ 要確認 ★★★


# Google Drive API, Gmail API の設定
# 'https://mail.google.com/' は通常不要なので削除しても良い場合があります
SCOPES = ['openid', 'https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/gmail.send', 'https://www.googleapis.com/auth/userinfo.email','https://mail.google.com/']
FOLDER_ID = '1ju1sS1aJxyUXRZxTFXpSO-sN08UKSE0s'  # アップロード先のGoogle DriveフォルダID

# メール設定 (Gmailの例) - セキュリティのため環境変数推奨
#MAIL_SERVER = 'smtp.gmail.com'
#MAIL_PORT = 587 # TLSの場合
# MAIL_USERNAME/PASSWORDはGmail API(サービスアカウント)では直接使わないが、設定は残す
MAIL_USERNAME = os.environ.get('MAIL_USERNAME') # 環境変数から取得 (例: 'your_email@gmail.com')
MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD') # 環境変数から取得 (例: 'your_app_password')
MAIL_SENDER_NAME = 'Time Capsule Keeper' # 送信者名

# --- SQLAlchemyJobStore の設定例 (原因不明エラーで使用中止）---
# Renderの環境変数などからデータベースURLを取得
#DATABASE_URL = os.environ.get('DATABASE_URL') # 例: postgresql://user:password@host:port/database
# --- RedisJobStore の設定 ---
# Renderの環境変数からRedis接続URLを取得
REDIS_URL = os.environ.get('REDIS_URL')
redis_conn_info = {} # 接続情報辞書を初期化
if not REDIS_URL:
    logging.error("★★★ 致命的エラー: 環境変数 REDIS_URL が設定されていません ★★★")
    logging.error("アプリケーションを終了します。Renderの環境変数設定を確認してください。")
    sys.exit(1) # ★★★ REDIS_URLがない場合は起動しない ★★★
else:
    try:
        # REDIS_URLをパースして接続情報を取得
        redis_conn_info = redis.connection.parse_url(REDIS_URL)
        # パスワードがbytes型の場合、デコードする (redis-pyのバージョンによる)
        if 'password' in redis_conn_info and isinstance(redis_conn_info['password'], bytes):
             redis_conn_info['password'] = redis_conn_info['password'].decode('utf-8')
        # db番号が文字列の場合、intに変換
        if 'db' in redis_conn_info:
            redis_conn_info['db'] = int(redis_conn_info['db'])
        logging.info(f"Redis接続情報をパースしました: host={redis_conn_info.get('host')}, port={redis_conn_info.get('port')}, db={redis_conn_info.get('db')}")
    except Exception as e:
        logging.error(f"★★★ 致命的エラー: REDIS_URL のパースに失敗しました: {REDIS_URL} ★★★")
        logging.error(f"エラー詳細: {e}", exc_info=True)
        logging.error("アプリケーションを終了します。")
        sys.exit(1) # ★★★ パース失敗時も起動しない ★★★


# APSchedulerの設定ディクショナリ
# scheduler_config = {
#     'apscheduler.jobstores.default': {
#         'type': 'sqlalchemy',
#         'url': DATABASE_URL
#     },
#     'apscheduler.executors.default': {
#         'class': 'apscheduler.executors.pool:ThreadPoolExecutor',
#         'max_workers': '5' # 必要に応じて調整
#     },
#     'apscheduler.job_defaults.coalesce': 'false',
#     'apscheduler.job_defaults.max_instances': '1', # 必要に応じて調整
#     'apscheduler.timezone': 'UTC', # タイムゾーンを明示的に指定 (推奨)
# }

# スケジューラの設定 (設定ディクショナリを渡す)
scheduler = APScheduler()
# scheduler.init_app(app) の代わりに configure を使うか、
# Flaskの設定に APSCHEDULER_ で始まるキーで設定を追加する
app.config['SCHEDULER_JOBSTORES'] = {
   # 'default': SQLAlchemyJobStore(url=DATABASE_URL)
    # --- ★★★ RedisJobStoreの初期化方法を変更 ★★★ ---
    'default': RedisJobStore(**redis_conn_info) # パースした接続情報をキーワード引数として渡す
    # ------------------------------------------------
}
app.config['SCHEDULER_EXECUTORS'] = {
    'default': {'type': 'threadpool', 'max_workers': 20}
}
app.config['SCHEDULER_JOB_DEFAULTS'] = {
    'coalesce': False,
    'max_instances': 3
}
app.config['SCHEDULER_API_ENABLED'] = True # 必要に応じてAPIを有効化
app.config['SCHEDULER_TIMEZONE'] = 'UTC' # タイムゾーン設定

scheduler.init_app(app) # Flaskの設定から読み込ませる
scheduler.start()

# ロギング設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Google API 認証関数 (サービスアカウント版) ---
def get_credentials():
    """サービスアカウントキーファイルを使用してGoogle APIの認証情報を取得する"""
    try:
        if not os.path.exists(SERVICE_ACCOUNT_FILE):
            logging.error(f"サービスアカウントキーファイルが見つかりません: {SERVICE_ACCOUNT_FILE}")
            return None
        # サービスアカウントファイルから認証情報を作成
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        logging.info("サービスアカウント認証情報を正常に読み込みました。")
        return creds
    except Exception as e:
        logging.error(f"サービスアカウント認証情報の読み込み中にエラーが発生しました: {e}", exc_info=True)
        return None

# --- Google Drive 関連関数 ---
def get_gdrive_service():
    creds = get_credentials()
    if not creds:
        print("認証情報の取得に失敗しました。(get_gdrive_service)")
        return None
    try:
        service = build('drive', 'v3', credentials=creds)
        return service
    except Exception as e:
        print(f"Google Driveサービスへの接続中にエラーが発生しました: {e}")
        return None

def upload_to_gdrive(file_path, file_name, folder_id):
    service = get_gdrive_service()
    if service is None:
        print("Google Driveサービスへの接続に失敗しました。認証を確認してください。")
        return False

    file_metadata = {
        'name': file_name,
        'parents': [folder_id]
    }
    media = MediaFileUpload(file_path, resumable=True)
    try:
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        file_id = file.get('id')
        # logging.info(f"File ID: {file_id}") # ログレベルをINFOに変更推奨
        logging.info(f"Google Driveへのアップロード成功: File ID={file_id}, Name='{file_name}'")
        return file_id # ★★★ ファイルIDを返す ★★★
    except Exception as e:
        # print(f"Google Driveへのアップロード中にエラーが発生しました: {e}") # loggingを使う
        logging.error(f"Google Driveへのアップロード中にエラーが発生しました: Name='{file_name}', Path='{file_path}', Error: {e}", exc_info=True)
        return None # ★★★ 失敗時は None を返す ★★★
    # service is None の場合の return False も None に統一すると良いかもしれません
    # if service is None:
    #     logging.error("Google Driveサービスへの接続に失敗しました。認証を確認してください。")
    #     return None

# --- 経過期間計算関数 (簡易版) ---
def calculate_elapsed_period_simple(start_time):
    """開始時刻から現在までの経過期間を文字列で返す (簡易版)"""
    now = datetime.now()
    delta = now - start_time
    days = delta.days

    if days < 0: # 未来の日付が渡された場合など (通常はないはず)
        return "未来"

    if days >= 365:
        years = days // 365
        months = (days % 365) // 30 # 簡易計算
        period_str = f"約{years}年"
        if months > 0:
            period_str += f"{months}ヶ月"
    elif days >= 30:
        months = days // 30
        remaining_days = days % 30
        period_str = f"約{months}ヶ月"
        if remaining_days > 0:
            period_str += f"{remaining_days}日"
    elif days > 0:
        period_str = f"{days}日"
    else:
        # 1日未満の場合
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        if hours > 0:
            period_str = f"約{hours}時間"
        elif minutes > 0:
            period_str = f"約{minutes}分"
        else:
            period_str = "ほんの少し" # 1分未満
    return period_str


# --- メール送信関数 (Gmail API版, サービスアカウント認証, Driveダウンロード対応版) ---
def send_reminder_email(to_email, upload_time, file_details, message_body=''):
    """
    指定されたメールアドレスにリマインドメールを送信する (Gmail API, サービスアカウント認証)。
    Google Driveからファイルをダウンロードして添付する。
    Args:
        to_email (str): 送信先メールアドレス
        upload_time (datetime): 元のアップロード日時
        file_details (list): Google Driveのファイル情報リスト [{'id': '...', 'name': '...'}, ...]
        message_body (str): ユーザーからのメッセージ
    """
    logging.info(f"--- [Job Start] リマインドメール送信開始 (Drive Download): 宛先={to_email}, アップロード日時={upload_time} ---")
    if not file_details:
        logging.warning("[Job] 添付すべきファイル情報がありません。メール本文のみ送信します。")
        # ファイルがない場合でもメールは送信する（メッセージはあるかもしれないため）

    downloaded_temp_paths = [] # ダウンロードした一時ファイルのパスを格納
    gdrive_service = None # Driveサービスを初期化

    try:
        # --- 0. Google Drive サービス取得 ---
        # この関数はスケジューラから直接実行されるため、再度認証情報を取得する必要がある
        gdrive_service = get_gdrive_service()
        if not gdrive_service:
            logging.error("[Job] Google Driveサービスへの接続に失敗しました。ファイル添付はスキップされます。")
            # Driveに接続できなくてもメール本文は送る試みをする

        # --- 1. Google Driveからファイルをダウンロード ---
        if gdrive_service and file_details: # Driveサービスがあり、ファイル詳細情報もある場合のみダウンロード
            logging.info("[Job] Google Driveからファイルのダウンロードを開始します...")
            for file_info in file_details:
                file_id = file_info.get('id')
                original_name = file_info.get('name')
                if not file_id or not original_name:
                    logging.warning(f"[Job] 無効なファイル情報です: {file_info}。スキップします。")
                    continue

                # 一時ファイルのパスを生成 (衝突を避けるためIDと元の名前を使う)
                # TEMP_FOLDER はグローバル変数または設定から取得
                # ★★★ TEMP_FOLDER が定義されていることを確認 ★★★
                if 'TEMP_FOLDER' not in globals() or not os.path.exists(TEMP_FOLDER):
                     logging.error(f"[Job] 一時フォルダ TEMP_FOLDER が無効です。ダウンロードできません。")
                     # TEMP_FOLDER がないとダウンロードできないので、ループを抜けるか、代替パスを使う
                     # ここではスキップする
                     continue

                temp_download_path = os.path.join(TEMP_FOLDER, f"downloaded_{file_id}_{original_name}")

                try:
                    logging.info(f"[Job] Downloading '{original_name}' (ID: {file_id}) to {temp_download_path}...")
                    request = gdrive_service.files().get_media(fileId=file_id)
                    # io.BytesIO を使うか、直接ファイルに書き込むか選択
                    # ここでは直接ファイルに書き込む
                    fh = io.FileIO(temp_download_path, 'wb')
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while done is False:
                        status, done = downloader.next_chunk()
                        if status:
                            logging.info(f"[Job] Download {int(status.progress() * 100)}%.")
                    fh.close() # ファイルハンドルを閉じる
                    logging.info(f"[Job] Successfully downloaded '{original_name}' to {temp_download_path}")
                    downloaded_temp_paths.append(temp_download_path) # 成功したパスをリストに追加
                except HttpError as e_download_http:
                     logging.error(f"[Job] Google Driveからのファイルダウンロード中にHTTPエラーが発生しました (ID: {file_id}, Name: {original_name}): {e_download_http}", exc_info=True)
                     if e_download_http.resp.status == 404:
                         logging.error(f"[Job] ファイルが見つかりません(404)。削除された可能性があります。")
                     # ダウンロードに失敗したファイルは添付しない
                except Exception as e_download:
                    logging.error(f"[Job] Google Driveからのファイルダウンロード中に予期せぬエラーが発生しました (ID: {file_id}, Name: {original_name}): {e_download}", exc_info=True)
                    # ダウンロードに失敗したファイルは添付しない

        # --- 2. Gmail API 認証情報取得 ---
        creds = get_credentials()
        if not creds:
            logging.error("[Job] Gmail APIの認証情報の取得に失敗しました。")
            raise Exception("Failed to get credentials for Gmail API")

        # ドメイン全体の委任が必要な場合のコメントはそのまま残す
        # creds = creds.with_subject(MAIL_USERNAME)

        gmail_service = build('gmail', 'v1', credentials=creds)

        # --- 3. メールの件名と本文を生成 ---
        subject = "あなたのタイムカプセルの開封日です"
        # upload_time は datetime オブジェクトとして渡されているはず
        elapsed_str = calculate_elapsed_period_simple(upload_time)
        upload_time_str = upload_time.strftime('%Y年%m月%d日 %H時%M分')
        # 添付ファイル名は file_details から取得 (ダウンロード成功有無に関わらず元のリストを表示)
        attachment_names = [f.get('name', '不明なファイル') for f in file_details] if file_details else []
        message_section = ""
        if message_body and message_body.strip():
            message_section = f"""
--- あの日のあなたからのメッセージ ---
{message_body.strip()}
------------------------------------
"""
        # ダウンロードに失敗したファイルがあるかどうかの情報も追加すると親切かもしれない
        download_status_message = ""
        if file_details and len(downloaded_temp_paths) < len(file_details):
            download_status_message = "\n\n注意: いくつかのファイルの取得に問題があり、添付されていない可能性があります。"


        body = f"""未来のあなたへ

託したタイムカプセルが、本日、開封予定日を迎えましたことをお知らせいたします。

タイムカプセルに大切な何かを保管してから、{elapsed_str}という時間が流れました。
あの時思い描いた未来は、今、どのように実現しているでしょうか。

もしかしたら、忘れていた夢や目標が、このタイムカプセルの中に眠っているかもしれません。

{f'添付ファイルを開いて、' if downloaded_temp_paths else ''}未来の自分へのメッセージ、そして{upload_time_str}の自分との再会を果たしてください。
この特別な瞬間が、あなたの未来を輝かせるきっかけの１つとなりますように。

---
カプセルに入っていたもの:
{', '.join(attachment_names) if attachment_names else '(ファイルなし)'}
{message_section}
{download_status_message}

From: {MAIL_SENDER_NAME}
"""
        # --- 4. メールメッセージの作成 ---
        message = MIMEMultipart()
        message['to'] = to_email
        message['from'] = Header(MAIL_SENDER_NAME, 'utf-8').encode()
        message['subject'] = Header(subject, 'utf-8')
        message.attach(MIMEText(body, 'plain', 'utf-8'))

        # --- 5. 添付ファイルの処理 (ダウンロード成功したもののみ) ---
        logging.info(f"[Job] メールに添付するファイル: {downloaded_temp_paths}")
        for file_path in downloaded_temp_paths:
            if not os.path.exists(file_path):
                logging.warning(f"[Job] 添付予定だった一時ファイルが見つかりません: {file_path}")
                continue

            content_type, encoding = mimetypes.guess_type(file_path)
            if content_type is None or encoding is not None:
                content_type = 'application/octet-stream'
            main_type, sub_type = content_type.split('/', 1)

            try:
                with open(file_path, 'rb') as fp:
                    part = MIMEBase(main_type, sub_type)
                    part.set_payload(fp.read())
                    encoders.encode_base64(part)
                # ファイル名は元の名前を使う (パスから抽出するか、file_detailsと突き合わせる)
                # パス "downloaded_{id}_{original_name}" から original_name を抽出
                filename = os.path.basename(file_path).split('_', 2)[-1]
                part.add_header('Content-Disposition', 'attachment', filename=Header(filename, 'utf-8').encode()) # ファイル名をUTF-8でエンコード
                message.attach(part)
                logging.info(f"[Job] 一時ファイル '{filename}' ({file_path}) をメールに添付しました。")
            except Exception as e_attach:
                logging.error(f"[Job] 一時ファイル '{os.path.basename(file_path)}' の添付処理中にエラー: {e_attach}", exc_info=True)

        # --- 6. メールの送信 ---
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': raw_message}

        send_message = gmail_service.users().messages().send(userId='me', body=create_message).execute()
        logging.info(f"[Job] リマインドメールを {to_email} に送信しました (Gmail API)。 Message ID: {send_message['id']}")

    except HttpError as error: # googleapiclient.errors.HttpError をインポートしておく
        logging.error(f"[Job] Gmail APIでのメール送信中にAPIエラーが発生しました: {error}")
        logging.error(f"[Job] エラー詳細: {error.content}")
        if error.resp.status == 403:
            logging.error("[Job] 権限エラー(403): サービスアカウントに必要な権限が付与されていないか、ドメイン全体の委任が必要な可能性があります。")
        elif error.resp.status == 400:
            logging.error(f"[Job] メール送信リクエストが無効です(400)。宛先({to_email})などを確認してください。")
    except Exception as e:
        logging.error(f"[Job] メール送信処理中に予期せぬエラーが発生しました: {e}", exc_info=True)

    finally:
        # --- 7. 処理完了後（成功・失敗問わず）にダウンロードした一時ファイルを削除 ---
        logging.info("[Job] メール送信処理完了。ダウンロードした一時ファイルの削除を試みます...")
        for fp in downloaded_temp_paths:
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                    logging.info(f"[Job] ダウンロードした一時ファイル '{os.path.basename(fp)}' を削除しました。")
                except Exception as e_rem:
                    logging.error(f"[Job] ダウンロードした一時ファイル '{os.path.basename(fp)}' の削除に失敗: {e_rem}", exc_info=True)
            else:
                logging.warning(f"[Job] 削除対象の一時ファイルが見つかりません: {fp}")
        logging.info(f"--- [Job End] リマインドメール送信処理終了: 宛先={to_email} ---")
# --- RQの設定 ---
# Redis接続 (RQ用) - Flaskアプリとワーカーの両方から参照される
redis_conn_rq = Redis.from_url(REDIS_URL) # 環境変数から直接接続
# デフォルトキューを作成
q = Queue(connection=redis_conn_rq)

# send_reminder_email の修正 (ファイルIDを受け取り、Driveからダウンロードする実装に変更が必要)
# 例: def send_reminder_email(to_email, upload_time, file_details, message_body=''):
#        # file_details は [{'id': '...', 'name': '...'}, ...] のような辞書リスト
#        # ... Google Driveから file_id を使ってダウンロード ...
#        # ... ダウンロードしたファイルを添付 ...
#        # ... 送信後、ダウンロードした一時ファイルを削除 ...

# process_upload_and_schedule の修正

# --- バックグラウンドタスクとして実行される関数 ---
def process_upload_and_schedule(temp_file_paths, filenames, folder_id, remind_email, remind_datetime_obj, message_body, upload_time_iso):
    """
    バックグラウンドでファイルのGoogle Driveアップロードとリマインダースケジュールを行うタスク。
    Args:
        temp_file_paths (list): 一時ファイルのパスのリスト
        filenames (list): 元のファイル名のリスト
        folder_id (str): Google DriveのフォルダID
        remind_email (str): リマインダー送信先メールアドレス
        remind_datetime_obj (datetime): リマインダー送信日時 (datetimeオブジェクト)
        message_body (str): メッセージ本文
        upload_time_iso (str): アップロード時刻 (ISOフォーマット文字列)
    """
    logging.info(f"--- [RQ Task Start] Processing upload for {remind_email} ---")
    uploaded_file_details = [] # 成功したファイルのIDと名前を格納するリスト
    
    # ISOフォーマットからdatetimeオブジェクトに戻す
    try:
        remind_datetime_obj = datetime.fromisoformat(remind_datetime_iso)
        upload_time = datetime.fromisoformat(upload_time_iso)
    except ValueError as e:
        logging.error(f"[RQ Task] Invalid ISO datetime format received: remind='{remind_datetime_iso}', upload='{upload_time_iso}'. Error: {e}", exc_info=True)
        # タスクを失敗させるか、適切なエラー処理を行う
        # ここでは早期リターン
        logging.error("[RQ Task] Datetime conversion failed. Aborting task.")
        # ★★★ finallyブロックは実行されるので一時ファイルは削除される ★★★
        return

    try:
        # 1. Google DriveへのアップロードとファイルIDの取得
        for i, temp_path in enumerate(temp_file_paths):
            original_filename = original_filenames[i] # 元のファイル名
            logging.info(f"[RQ Task] Uploading '{original_filename}' from {temp_path} to Google Drive...")
            if os.path.exists(temp_path):
                # ★★★ upload_to_gdrive の戻り値(file_id)を受け取る ★★★
                file_id = upload_to_gdrive(temp_path, original_filename, folder_id)
                if file_id: # ★★★ file_id が None でないかチェック ★★★
                    logging.info(f"[RQ Task] Successfully uploaded '{original_filename}' to Google Drive. File ID: {file_id}")
                    uploaded_file_details.append({'id': file_id, 'name': original_filename}) # IDと元の名前を保存
                else:
                    logging.warning(f"[RQ Task] Failed to upload '{original_filename}' to Google Drive.")
            else:
                logging.error(f"[RQ Task] Temporary file not found by worker: {temp_path}. Skipping upload.")

        # 2. リマインダーメールのスケジュール (少なくとも1つ成功した場合)
        if uploaded_file_details:
            logging.info(f"[RQ Task] Scheduling reminder email for {remind_email} at {remind_datetime_obj}...")
            try:
                job_id = f'reminder_{remind_email}_{remind_datetime_obj.timestamp()}'
                # ★★★ 以下の scheduler.add_job の呼び出しを追加 ★★★
                scheduler.add_job(
                    id=job_id,
                    func=send_reminder_email, # send_reminder_email はファイルIDリストを受け取るように修正済みのはず
                    trigger='date',
                    run_date=remind_datetime_obj,
                    # ★重要★ ファイルIDと名前のリストを渡す
                    args=[remind_email, upload_time, uploaded_file_details, message_body],
                    replace_existing=True,
                    misfire_grace_time=3600 # 例: 1時間以内なら実行
                )
                logging.info(f"[RQ Task] Successfully scheduled job {job_id}")
            except Exception as e_sched:
                logging.error(f"[RQ Task] Failed to schedule reminder: {e_sched}", exc_info=True)
                # スケジュール失敗時のエラーハンドリング (必要であれば)
        else:
             logging.warning("[RQ Task] No files were successfully uploaded to Google Drive. Skipping reminder schedule.")

        
    except Exception as e_task:
        logging.error(f"[RQ Task] Error during background task execution: {e_task}", exc_info=True)

    finally:
        # 3. 一時ファイルの削除 (常に実行)
        logging.info("[RQ Task] Cleaning up temporary files...")
        for temp_path in temp_file_paths:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                    logging.info(f"[RQ Task] Removed temporary file: {temp_path}")
                except Exception as e_rem:
                    logging.error(f"[RQ Task] Failed to remove temporary file {temp_path}: {e_rem}", exc_info=True)
            else:
                 logging.warning(f"[RQ Task] Temporary file already removed or not found: {temp_path}")
        logging.info(f"--- [RQ Task End] Finished processing for {remind_email} ---")

# --- Flask ルート (変更なし、ただしエラーハンドリングやログを強化推奨) ---
@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        login_status = request.form.get('login')

        if login_status != 'true' or username != 'ai_academy':
            logging.warning(f"ログイン失敗: username={username}, login_status={login_status}")
            return jsonify({
                "code": 401,
                "msg": "Unauthorized: Incorrect username or login status"
            }), 401
        logging.info(f"ログイン成功: username={username}")
        return redirect(url_for('mypage', username=username, login=login_status))
    else:
        return app.send_static_file('login.html')

@app.route('/mypage')
def mypage():
    username = request.args.get('username')
    login = request.args.get('login')

    if login != 'true' or username != 'ai_academy':
        logging.warning(f"マイページアクセス拒否: username={username}, login={login}")
        return jsonify({
            "code": 401,
            "msg": "Unauthorized: Invalid session"
        }), 401

    return redirect(url_for('upload'))

# test0320.py 内の /upload ルートを修正

# --- ★★★ 一時ファイルの保存場所を変更 ★★★ ---
# Render環境では /tmp ディレクトリが利用可能な場合が多い
# 環境変数などで指定できるようにするとより良い
TEMP_FOLDER = '/tmp/timecapsule_uploads'
# 起動時に一時フォルダを作成 (存在しない場合)
if not os.path.exists(TEMP_FOLDER):
    try:
        os.makedirs(TEMP_FOLDER)
        logging.info(f"一時ディレクトリを作成しました: {TEMP_FOLDER}")
    except OSError as e:
        logging.error(f"一時ディレクトリの作成に失敗しました: {TEMP_FOLDER}, Error: {e}")
        # エラー発生時の処理 (例: カレントディレクトリを使うなど)
        TEMP_FOLDER = '.' # フォールバック

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        logging.info("--- ファイルアップロード処理開始 (RQ版) ---")
        # --- ファイルとリマインダー情報の取得 (変更なし) ---
        if 'file' not in request.files:
            logging.warning("アップロードリクエストにファイルパートがありません。")
            return jsonify({"msg": "No file part"}), 400
        files = request.files.getlist('file')
        if not files or files[0].filename == '':
             logging.warning("アップロードファイルが選択されていません。")
             return jsonify({"msg": "No selected file"}), 400

        remind_datetime_str = request.form.get('remind_datetime')
        remind_email = request.form.get('remind_email')
        message_body = request.form.get('message', '')
        upload_time = datetime.now() # アップロード時刻を記録

        logging.info(f"リマインダー情報: 日時={remind_datetime_str}, メール={remind_email}, メッセージ='{message_body[:20]}...'")

        if not remind_datetime_str or not remind_email:
            logging.warning("リマインダー日時またはメールアドレスが指定されていません。")
            return jsonify({"msg": "Reminder date/time and email are required"}), 400

        try:
            remind_datetime_obj = datetime.strptime(remind_datetime_str, '%Y-%m-%dT%H:%M')
            if remind_datetime_obj <= datetime.now():
                 logging.warning(f"リマインダー日時が過去です: {remind_datetime_str}")
                 return jsonify({"msg": "Reminder date/time must be in the future"}), 400
        except ValueError:
            logging.error(f"無効な日時フォーマットです: {remind_datetime_str}")
            return jsonify({"msg": "Invalid date/time format"}), 400

        temp_file_paths = []
        original_filenames = []
        save_error = False

        # --- ファイルを一時ディレクトリに保存 ---
        for file in files:
            if file and file.filename:
                original_filename = file.filename # 元の名前を保存
                # filename = secure_filename(file.filename) # サニタイズする場合
                filename = original_filename # ここではサニタイズしない場合
                if not filename: continue # スキップ処理

                file_path = os.path.join(TEMP_FOLDER, f"{upload_time.timestamp()}_{filename}") # 一意なパス

                try:
                    file.save(file_path)
                    logging.info(f"一時ファイル '{original_filename}' を保存しました: {file_path}")
                    temp_file_paths.append(file_path)
                    original_filenames.append(original_filename) # 元のファイル名をリストに追加
                except Exception as e:
                    # ... (エラー処理、中断) ...
                    break

        # --- 保存失敗時の処理 ---
        if save_error:
            # ... (エラー処理、一時ファイル削除) ...
            return jsonify({"msg": "Failed to save uploaded file temporarily."}), 500

        if temp_file_paths:
            try:
                logging.info(f"[{time.time()}] Enqueuing background task...")
                upload_time_iso = upload_time.isoformat()
                remind_datetime_iso = remind_datetime_obj.isoformat() # ★ datetime を ISO 文字列に変換 ★

                job = q.enqueue(
                    process_upload_and_schedule,
                    args=(temp_file_paths, original_filenames, FOLDER_ID, remind_email, remind_datetime_iso, message_body, upload_time_iso), # ★ 修正した引数 ★
                    job_timeout='10m'
                )
                logging.info(f"[{time.time()}] Task enqueued with job ID: {job.id}")
                return jsonify({"msg": f"File(s) received. Processing and reminder scheduling will run in the background."}), 202
            except Exception as e_enqueue:
                # ... (エラー処理、一時ファイル削除) ...
                return jsonify({"msg": "Failed to enqueue background task."}), 500
        else:
            # ... (ファイルがない場合のエラー処理) ...
            return jsonify({"msg": "No files were saved temporarily."}), 400

    else: # GETリクエスト
        return app.send_static_file('upload.html')

# --- アプリケーションの実行 ---
if __name__ == "__main__":
    # 環境変数 SECRET_KEY のチェック
    if app.config['SECRET_KEY'] == 'default-secret-key':
        logging.warning("警告: FlaskのSECRET_KEYがデフォルト値です。本番環境では必ず環境変数 FLASK_SECRET_KEY を設定してください。")
    # サービスアカウントファイルの存在チェック
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
         logging.error(f"★★★ 致命的エラー: サービスアカウントファイルが見つかりません: {SERVICE_ACCOUNT_FILE} ★★★")
         logging.error("RenderのSecret Files設定を確認してください。")
         # ここでアプリケーションを終了させることも検討
         # exit(1)

    # Gunicornから実行される場合は __name__ == "__main__" は通らない
    # ローカルでのデバッグ実行用
    # use_reloader=False は APScheduler との併用時に推奨
    logging.info("ローカルデバッグモードでアプリケーションを起動します...")
    app.run(host='0.0.0.0', port=8000, debug=True, use_reloader=False)
