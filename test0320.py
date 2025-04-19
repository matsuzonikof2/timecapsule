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

# Google Drive API関連
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
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

# --- SQLAlchemyJobStore の設定例 ---
# Renderの環境変数などからデータベースURLを取得
#DATABASE_URL = os.environ.get('DATABASE_URL') # 例: postgresql://user:password@host:port/database
# --- RedisJobStore の設定 ---
# Renderの環境変数からRedis接続URLを取得
REDIS_URL = os.environ.get('REDIS_URL')
if not REDIS_URL:
    logging.error("★★★ 致命的エラー: 環境変数 REDIS_URL が設定されていません ★★★")
    # アプリケーションを終了させるか、適切なエラー処理を行う
    # exit(1)


# APSchedulerの設定ディクショナリ
scheduler_config = {
    'apscheduler.jobstores.default': {
        'type': 'sqlalchemy',
        'url': DATABASE_URL
    },
    'apscheduler.executors.default': {
        'class': 'apscheduler.executors.pool:ThreadPoolExecutor',
        'max_workers': '5' # 必要に応じて調整
    },
    'apscheduler.job_defaults.coalesce': 'false',
    'apscheduler.job_defaults.max_instances': '1', # 必要に応じて調整
    'apscheduler.timezone': 'UTC', # タイムゾーンを明示的に指定 (推奨)
}

# スケジューラの設定 (設定ディクショナリを渡す)
scheduler = APScheduler()
# scheduler.init_app(app) の代わりに configure を使うか、
# Flaskの設定に APSCHEDULER_ で始まるキーで設定を追加する
app.config['SCHEDULER_JOBSTORES'] = {
   # 'default': SQLAlchemyJobStore(url=DATABASE_URL)
    'default': RedisJobStore(url=REDIS_URL) # RedisJobStoreを使用するように変更

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
        print(f"File ID: {file_id}")
        return True
    except Exception as e:
        print(f"Google Driveへのアップロード中にエラーが発生しました: {e}")
        return False

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


# --- メール送信関数 (Gmail API版, サービスアカウント認証) ---
def send_reminder_email(to_email, upload_time, file_paths=None, message_body=''):
    """指定されたメールアドレスにリマインドメールを送信する (Gmail API, サービスアカウント認証)"""
    logging.info(f"--- リマインドメール送信開始: 宛先={to_email}, アップロード日時={upload_time} ---")
    if file_paths is None:
        file_paths = []

    try:
        creds = get_credentials()
        if not creds:
            logging.error("Gmail APIの認証情報の取得に失敗しました。")
            raise Exception("Failed to get credentials for Gmail API")

        # ★★★ ドメイン全体の委任が必要な場合がある ★★★
        # Workspace環境で、サービスアカウントに代理送信権限を与える必要があるかもしれません。
        # もし権限がない場合、`delegated_credentials` を作成する必要があります。
        # 例: creds = creds.with_subject(MAIL_USERNAME) # MAIL_USERNAMEは代理送信元のWorkspaceユーザーメール
        # これが必要かは環境によります。まずは委任なしで試します。

        gmail_service = build('gmail', 'v1', credentials=creds)

        # --- メールの件名と本文を生成 ---
        subject = "あなたのタイムカプセルの開封日です"
        elapsed_str = calculate_elapsed_period_simple(upload_time)
        upload_time_str = upload_time.strftime('%Y年%m月%d日 %H時%M分')
        attachment_names = [os.path.basename(fp) for fp in file_paths] if file_paths else []
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
{', '.join(attachment_names) if attachment_names else '(ファイルなし)'}
{message_section}

From: {MAIL_SENDER_NAME}
"""
        # --- メールメッセージの作成 ---
        message = MIMEMultipart()
        message['to'] = to_email
        # サービスアカウントで送信する場合、Fromはサービスアカウント自身か、
        # ドメイン全体の委任で指定したユーザーになります。
        # ここでは固定の送信者名を表示します。
        # 実際の送信元アドレスはGmail側で設定されます。
        message['from'] = Header(MAIL_SENDER_NAME, 'utf-8').encode()
        message['subject'] = Header(subject, 'utf-8')
        message.attach(MIMEText(body, 'plain', 'utf-8'))

        # --- 添付ファイルの処理 ---
        for file_path in file_paths:
            if not os.path.exists(file_path):
                logging.warning(f"添付ファイルが見つかりません: {file_path}")
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
                filename = os.path.basename(file_path)
                part.add_header('Content-Disposition', 'attachment', filename=filename)
                message.attach(part)
                logging.info(f"ファイル '{filename}' をメールに添付しました。")
            except Exception as e:
                logging.error(f"ファイル '{os.path.basename(file_path)}' の添付処理中にエラー: {e}", exc_info=True)

        # --- メールの送信 ---
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': raw_message}

        # ★★★ userId='me' はサービスアカウント自身を指します ★★★
        # ドメイン全体の委任を使用しない場合、これで動作するはずです。
        # 委任を使用する場合は、委任先のユーザーID ('user@example.com') を指定するか、
        # `creds.with_subject()` で作成した認証情報を使う必要があります。
        send_message = gmail_service.users().messages().send(userId='me', body=create_message).execute()
        logging.info(f"リマインドメールを {to_email} に送信しました (Gmail API)。 Message ID: {send_message['id']}")

    except HttpError as error:
        logging.error(f"Gmail APIでのメール送信中にAPIエラーが発生しました: {error}")
        logging.error(f"エラー詳細: {error.content}")
        # 権限エラー(403)の場合、ドメイン全体の委任が必要か確認
        if error.resp.status == 403:
            logging.error("権限エラー(403): サービスアカウントに必要な権限が付与されていないか、ドメイン全体の委任が必要な可能性があります。")
        elif error.resp.status == 400:
            logging.error(f"メール送信リクエストが無効です(400)。宛先({to_email})などを確認してください。")
    except Exception as e:
        logging.error(f"Gmail APIでのメール送信中に予期せぬエラーが発生しました: {e}", exc_info=True)

    finally:
        # --- 処理完了後（成功・失敗問わず）に一時ファイルを削除 ---
        logging.info("メール送信処理完了。一時ファイルの削除を試みます...")
        for fp in file_paths:
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                    logging.info(f"一時ファイル '{os.path.basename(fp)}' を削除しました。")
                except Exception as e_rem:
                    logging.error(f"一時ファイル '{os.path.basename(fp)}' の削除に失敗: {e_rem}", exc_info=True)

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

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        logging.info("--- ファイルアップロード処理開始 ---")
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

        uploaded_filenames = []
        uploaded_file_paths = []
        upload_failed = False
        temp_files_created = []

        # --- ファイルアップロード処理 ---
        for file in files:
            if file and file.filename:
                filename = file.filename # 本番環境では secure_filename を推奨
                # 一時ファイルの保存場所を /tmp など一時ディレクトリにする方が良い場合がある
                file_path = os.path.join('.', filename) # カレントディレクトリに保存
                temp_files_created.append(file_path)

                try:
                    file.save(file_path)
                    logging.info(f"一時ファイル '{filename}' を保存しました: {file_path}")

                    logging.info(f"[{time.time()}] Calling upload_to_gdrive for {filename}...")
                    # upload_to_gdrive の結果を変数に格納
                    upload_successful = upload_to_gdrive(file_path, filename, FOLDER_ID)

                    # 変数を使って条件分岐とログ出力を行う
                    if upload_successful:
                        logging.info(f"[{time.time()}] upload_to_gdrive for {filename} finished. Success: {upload_successful}") # 変数を使用
                        uploaded_filenames.append(filename)
                        uploaded_file_paths.append(file_path) # 成功したファイルのパスを保持
                    else:
                        logging.warning(f"'{filename}' の Google Drive へのアップロードに失敗しました。")
                        upload_failed = True
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            logging.info(f"Driveアップロード失敗のため一時ファイル '{filename}' を削除しました。")
                            temp_files_created.remove(file_path)

                except Exception as e:
                    logging.error(f"ファイル処理中にエラーが発生しました ({filename}): {e}", exc_info=True)
                    # エラー発生時も、作成された一時ファイルをクリーンアップ
                    for fp in temp_files_created:
                        if os.path.exists(fp):
                            try: os.remove(fp)
                            except: pass # 削除失敗は無視
                    return jsonify({"msg": f"Error processing file {filename}: {e}"}), 500

        # --- アップロード結果の処理 ---
        if upload_failed:
             logging.warning("一部のファイルのDriveアップロードに失敗しました。残存する一時ファイルを削除します。")
             for fp in uploaded_file_paths: # 成功したもの（添付予定だった）も削除
                 if os.path.exists(fp):
                     try:
                         os.remove(fp)
                         logging.info(f"一時ファイル '{os.path.basename(fp)}' を削除しました。")
                     except Exception as e_rem:
                         logging.error(f"一時ファイル '{os.path.basename(fp)}' の削除に失敗: {e_rem}", exc_info=True)
             return jsonify({"msg": "Some files failed to upload to Google Drive"}), 500

        # --- リマインダーメールのスケジュール ---
        if uploaded_filenames:
            try:
                job_id = f'reminder_{remind_email}_{remind_datetime_obj.timestamp()}'
                logging.info(f"[{time.time()}] Calling scheduler.add_job for {remind_email}...")
                scheduler.add_job(
                    id=job_id,
                    func=send_reminder_email,
                    trigger='date',
                    run_date=remind_datetime_obj,
                    args=[remind_email, upload_time, uploaded_file_paths, message_body],
                    replace_existing=True
                )
                logging.info(f"[{time.time()}] scheduler.add_job finished.")
                # スケジュール成功時は一時ファイルは send_reminder_email 内で削除される

            except Exception as e:
                 logging.error(f"リマインダースケジュール中にエラー: {e}", exc_info=True)
                 logging.warning("リマインダースケジュール失敗。一時ファイルを削除します。")
                 for fp in uploaded_file_paths:
                     if os.path.exists(fp):
                         try: os.remove(fp)
                         except Exception as e_rem: logging.error(f"一時ファイル '{os.path.basename(fp)}' の削除に失敗: {e_rem}", exc_info=True)
                 return jsonify({"msg": f"File(s) uploaded, but failed to schedule reminder: {e}"}), 500

            logging.info("--- ファイルアップロード処理正常終了 ---")
            return jsonify({"msg": f"File(s) uploaded successfully and reminder set for {remind_datetime_str} with attachments"}), 200
        else:
             logging.warning("アップロードに成功したファイルがありません。")
             # この場合 temp_files_created は空のはず
             for fp in temp_files_created:
                 if os.path.exists(fp):
                     try: os.remove(fp)
                     except Exception as e_rem: logging.error(f"一時ファイル '{os.path.basename(fp)}' の削除に失敗: {e_rem}", exc_info=True)
             return jsonify({"msg": "No files were successfully uploaded."}), 400

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
