# test0320.py
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


# Google Drive API関連
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# スケジューラ関連
from flask_apscheduler import APScheduler

# --- 設定 ---
# Flaskアプリケーションのインスタンスを作成
app = Flask(__name__, static_folder='.', static_url_path='')
app.config['SECRET_KEY'] = 'your_secret_key' # スケジューラ等でセッションを使う場合に必要

# Google Drive API, Gmail API の設定
# 'https://mail.google.com/' は通常不要なので削除しても良い場合があります
SCOPES = ['openid', 'https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/gmail.send', 'https://www.googleapis.com/auth/userinfo.email','https://mail.google.com/']
FOLDER_ID = '1ju1sS1aJxyUXRZxTFXpSO-sN08UKSE0s'  # アップロード先のGoogle DriveフォルダID

# メール設定 (Gmailの例) - セキュリティのため環境変数推奨
MAIL_SERVER = 'smtp.gmail.com'
MAIL_PORT = 587 # TLSの場合
MAIL_USERNAME = os.environ.get('MAIL_USERNAME') # 環境変数から取得 (例: 'your_email@gmail.com')
MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD') # 環境変数から取得 (例: 'your_app_password')
MAIL_SENDER_NAME = 'Time Capsule Keeper' # 送信者名

# スケジューラの設定
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

# --- Google API 認証関数 ---
def get_credentials():
    """Google APIの認証情報を取得または更新する"""
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"トークンのリフレッシュに失敗しました: {e}")
                # リフレッシュ失敗時は再認証へ
                if os.path.exists('token.pickle'):
                    os.remove('token.pickle') # 古いトークンを削除
                creds = None
        if not creds: # credsがNone（初回またはリフレッシュ失敗）の場合
            if not os.path.exists('credentials.json'):
                print("credentials.json が見つかりません。")
                return None
            try:
                from google_auth_oauthlib.flow import InstalledAppFlow
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
                print("認証フローが完了しました。")
            except Exception as e:
                print(f"認証フロー中にエラーが発生しました: {e}")
                return None

        # 新しいクレデンシャルを保存
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return creds

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


# --- メール送信関数 (Gmail API版, 添付ファイル対応) ---
# --- メール送信関数 (Gmail API版, 添付ファイル対応, 本文生成ロジック変更) ---
def send_reminder_email(to_email, upload_time, file_paths=None, message_body=''): # 引数変更: subject, body を削除し upload_time を追加
    """指定されたメールアドレスにリマインドメールを送信する (Gmail APIを使用, 添付ファイル対応)"""
    print(f"--- send_reminder_email ---") # デバッグ用
    print(f"宛先: {to_email}")           # デバッグ用
    print(f"アップロード日時: {upload_time}") # デバッグ用
    print(f"ファイルパス: {file_paths}")   # デバッグ用
    print(f"受け取ったメッセージ: '{message_body}'") # ★★★ これを追加 ★★★

    if file_paths is None:
        file_paths = []

    try:
        creds = get_credentials()
        if not creds:
            print("Gmail APIの認証情報の取得に失敗しました。")
            raise Exception("Failed to get credentials for Gmail API")

        gmail_service = build('gmail', 'v1', credentials=creds)

        # --- メールの件名と本文を生成 ---
        subject = "あなたのタイムカプセルの開封日です"

        # 経過期間を計算
        elapsed_str = calculate_elapsed_period_simple(upload_time)
        # アップロード日時をフォーマット
        upload_time_str = upload_time.strftime('%Y年%m月%d日 %H時%M分')
        # 添付ファイル名リストを作成 (本文表示用)
        attachment_names = [os.path.basename(fp) for fp in file_paths] if file_paths else []
        # ★ メッセージ部分を生成 (空でない場合のみ)
        message_section = ""
        if message_body and message_body.strip(): # 空白のみでないかもチェック
            message_section = f"""
--- あの日のあなたからのメッセージ ---
{message_body.strip()}
------------------------------------
"""
        # 新しいメール本文テンプレート
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
        # --- メールメッセージの作成 (MIMEMultipartを使用) ---
        message = MIMEMultipart() # MIMEMultipartに変更
        message['to'] = to_email
        sender_email = MAIL_USERNAME if MAIL_USERNAME else 'me'
        if sender_email != 'me':
             message['from'] = f"{Header(MAIL_SENDER_NAME, 'utf-8').encode()} <{sender_email}>"
        else:
             # 送信元アドレス取得 (userinfo.email スコープが必要)
             try:
                 profile = gmail_service.users().getProfile(userId='me').execute()
                 sender_email = profile['emailAddress']
                 message['from'] = f"{Header(MAIL_SENDER_NAME, 'utf-8').encode()} <{sender_email}>"
             except HttpError as e_profile:
                 print(f"送信元メールアドレスの取得に失敗: {e_profile}")
                 print("環境変数 MAIL_USERNAME を設定するか、userinfo.email スコープの権限を確認してください。")
                 message['from'] = Header(MAIL_SENDER_NAME, 'utf-8').encode()
             except Exception as e_other_profile:
                 print(f"送信元メールアドレス取得中に予期せぬエラー: {e_other_profile}")
                 message['from'] = Header(MAIL_SENDER_NAME, 'utf-8').encode()

        message['subject'] = Header(subject, 'utf-8')

        # メール本文を追加
        message.attach(MIMEText(body, 'plain', 'utf-8'))

        # --- 添付ファイルの処理 ---
        for file_path in file_paths:
            if not os.path.exists(file_path):
                print(f"添付ファイルが見つかりません: {file_path}")
                continue # 次のファイルへ

            # MIMEタイプを推測
            content_type, encoding = mimetypes.guess_type(file_path)
            if content_type is None or encoding is not None:
                content_type = 'application/octet-stream' # 不明な場合は汎用タイプ
            main_type, sub_type = content_type.split('/', 1)

            try:
                with open(file_path, 'rb') as fp:
                    # MIMEBaseオブジェクトを作成
                    part = MIMEBase(main_type, sub_type)
                    # ファイル内容を読み込み、Base64エンコードして設定
                    part.set_payload(fp.read())
                    encoders.encode_base64(part)

                # Content-Dispositionヘッダーを設定 (ファイル名を指定)
                filename = os.path.basename(file_path)
                # ファイル名はRFC 2047に従ってエンコード (日本語等対応)
                part.add_header('Content-Disposition', 'attachment', filename=filename)

                # メッセージに添付
                message.attach(part)
                print(f"ファイル '{filename}' をメールに添付しました。")

            except Exception as e:
                print(f"ファイル '{os.path.basename(file_path)}' の添付処理中にエラー: {e}")
                # 特定のファイルの添付に失敗しても、他のファイルの処理は続ける

        # --- メールの送信 ---
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': raw_message}

        send_message = gmail_service.users().messages().send(userId='me', body=create_message).execute()
        print(f"リマインドメールを {to_email} に送信しました (Gmail API)。 Message ID: {send_message['id']}")

    except Exception as e:
        print(f"Gmail APIでのメール送信中にエラーが発生しました: {e}")
        if isinstance(e, HttpError): # googleapiclientのエラーの場合
            if e.resp.status == 403:
                 print("権限が不足している可能性があります。必要なスコープが付与されているか、token.pickleを削除して再認証してください。")
            elif e.resp.status == 400:
                 print(f"メール送信リクエストが無効です。宛先({to_email})などを確認してください。")
        elif 'invalid_grant' in str(e).lower():
             print("認証トークンが無効または期限切れの可能性があります。token.pickleを削除して再認証してください。")

    finally:
        # --- 処理完了後（成功・失敗問わず）に一時ファイルを削除 ---
        print("メール送信処理完了。一時ファイルの削除を試みます...")
        for fp in file_paths:
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                    print(f"一時ファイル '{os.path.basename(fp)}' を削除しました。")
                except Exception as e_rem:
                    print(f"一時ファイル '{os.path.basename(fp)}' の削除に失敗: {e_rem}")


# --- Flask ルート ---
@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        login_status = request.form.get('login')

        if login_status != 'true' or username != 'ai_academy':
            return jsonify({
                "code": 401,
                "msg": "Unauthorized: Incorrect username or login status"
            }), 401

        return redirect(url_for('mypage', username=username, login=login_status))
    else:
        return app.send_static_file('login.html')

@app.route('/mypage')
def mypage():
    username = request.args.get('username')
    login = request.args.get('login')

    if login != 'true' or username != 'ai_academy':
        return jsonify({
            "code": 401,
            "msg": "Unauthorized: Invalid session"
        }), 401

    return redirect(url_for('upload'))

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        # --- ファイル処理 ---
        if 'file' not in request.files:
            return jsonify({"msg": "No file part"}), 400
        files = request.files.getlist('file')
        if not files or files[0].filename == '':
             return jsonify({"msg": "No selected file"}), 400

        # --- リマインダー情報取得 ---
        remind_datetime_str = request.form.get('remind_datetime')
        remind_email = request.form.get('remind_email')
        # ★★★ メッセージを取得 (デフォルトは空文字) ★★★
        message_body = request.form.get('message', '')

        # ★★★ アップロード日時を取得 ★★★
        upload_time = datetime.now()

        if not remind_datetime_str or not remind_email:
            return jsonify({"msg": "Reminder date/time and email are required"}), 400

        try:
            remind_datetime_obj = datetime.strptime(remind_datetime_str, '%Y-%m-%dT%H:%M')
            if remind_datetime_obj <= datetime.now():
                 return jsonify({"msg": "Reminder date/time must be in the future"}), 400
        except ValueError:
            return jsonify({"msg": "Invalid date/time format"}), 400

        uploaded_filenames = []
        uploaded_file_paths = [] # ★★★ 添付用ファイルパスリスト ★★★
        upload_failed = False
        temp_files_created = [] # ★★★ 作成された一時ファイルのリスト（エラー時削除用） ★★★

        # --- ファイルアップロード処理 ---
        for file in files:
            if file and file.filename:
                # セキュリティのため、ファイル名をサニタイズすることを推奨
                # from werkzeug.utils import secure_filename
                # filename = secure_filename(file.filename)
                filename = file.filename # 今回はそのまま使用
                file_path = os.path.join('.', filename) # カレントディレクトリに保存
                temp_files_created.append(file_path) # 作成リストに追加

                try:
                    file.save(file_path)
                    print(f"一時ファイル '{filename}' を保存しました: {file_path}")

                    # Google Drive にアップロード
                    print(f"'{filename}' を Google Drive にアップロードを試みます...")
                    if upload_to_gdrive(file_path, filename, FOLDER_ID):
                        print(f"'{filename}' の Google Drive へのアップロードに成功しました。")
                        uploaded_filenames.append(filename)
                        uploaded_file_paths.append(file_path) # ★★★ 成功したファイルのパスを保持 ★★★
                        # ここでは削除しない
                    else:
                        print(f"'{filename}' の Google Drive へのアップロードに失敗しました。")
                        upload_failed = True # 失敗フラグ
                        # Driveアップロード失敗時は一時ファイルを削除
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            print(f"Driveアップロード失敗のため一時ファイル '{filename}' を削除しました。")
                            temp_files_created.remove(file_path) # 作成リストからも削除

                except Exception as e:
                    print(f"ファイル処理中にエラーが発生しました ({filename}): {e}")
                    # エラー発生時も、作成された一時ファイルをクリーンアップ
                    for fp in temp_files_created:
                        if os.path.exists(fp):
                            try:
                                os.remove(fp)
                                print(f"エラー発生のため一時ファイル '{os.path.basename(fp)}' を削除しました。")
                            except Exception as e_rem:
                                print(f"一時ファイル '{os.path.basename(fp)}' の削除に失敗: {e_rem}")
                    return jsonify({"msg": f"Error processing file {filename}: {e}"}), 500

        # --- アップロード結果の処理 ---
        if upload_failed:
             # 一部でも失敗した場合、成功したファイル（添付予定だった）も削除
             print("一部のファイルのDriveアップロードに失敗しました。残存する一時ファイルを削除します。")
             for fp in uploaded_file_paths: # uploaded_file_paths には成功したものだけが入っている
                 if os.path.exists(fp):
                     try:
                         os.remove(fp)
                         print(f"一時ファイル '{os.path.basename(fp)}' を削除しました。")
                     except Exception as e_rem:
                         print(f"一時ファイル '{os.path.basename(fp)}' の削除に失敗: {e_rem}")
             return jsonify({"msg": "Some files failed to upload to Google Drive"}), 500

        # --- リマインダーメールのスケジュール ---
        if uploaded_filenames: # 少なくとも1つのファイルが正常にアップロードされた場合
            
            try:
                # スケジューラにジョブを追加
                scheduler.add_job(
                    id=f'reminder_{remind_email}_{remind_datetime_obj.timestamp()}', # ユニークなID
                    func=send_reminder_email,
                    trigger='date',
                    run_date=remind_datetime_obj,
                    args=[remind_email, upload_time, uploaded_file_paths, message_body], # ★★★ ファイルパスリストを渡す ★★★
                    replace_existing=True
                )
                print(f"リマインダーをスケジュールしました: {remind_datetime_obj} に {remind_email} へ送信（添付ファイルパス: {uploaded_file_paths}）")
                # スケジュール成功時は、一時ファイルは send_reminder_email 関数内で削除される

            except Exception as e:
                 print(f"リマインダースケジュール中にエラー: {e}")
                 # スケジュール失敗時は、添付予定だった一時ファイルを削除
                 print("リマインダースケジュール失敗。一時ファイルを削除します。")
                 for fp in uploaded_file_paths:
                     if os.path.exists(fp):
                         try:
                             os.remove(fp)
                             print(f"一時ファイル '{os.path.basename(fp)}' を削除しました。")
                         except Exception as e_rem:
                             print(f"一時ファイル '{os.path.basename(fp)}' の削除に失敗: {e_rem}")
                 return jsonify({"msg": f"File(s) uploaded, but failed to schedule reminder: {e}"}), 500

            return jsonify({"msg": f"File(s) uploaded successfully and reminder set for {remind_datetime_str} with attachments"}), 200
        else:
             # アップロードされたファイルがない場合（通常は発生しないはず）
             print("アップロードに成功したファイルがありません。")
             # この場合 temp_files_created は空のはずだが念のため
             for fp in temp_files_created:
                 if os.path.exists(fp):
                     try:
                         os.remove(fp)
                         print(f"一時ファイル '{os.path.basename(fp)}' を削除しました。")
                     except Exception as e_rem:
                         print(f"一時ファイル '{os.path.basename(fp)}' の削除に失敗: {e_rem}")
             return jsonify({"msg": "No files were successfully uploaded."}), 400

    else:
        # GETリクエストの場合は 'upload.html' を表示
        return app.send_static_file('upload.html')

# --- アプリケーションの実行 ---
if __name__ == "__main__":
    if not MAIL_USERNAME:
        print("警告: 環境変数 MAIL_USERNAME が設定されていません。送信元アドレス特定のために推奨されます。")

    # use_reloader=False は APScheduler との併用時に推奨される
    app.run(port=8000, debug=True, use_reloader=False)
