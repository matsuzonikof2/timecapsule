# test0320.py (修正後)
from flask import Flask, request, jsonify, redirect, url_for, render_template, session, flash
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
from zoneinfo import ZoneInfo 
# ★★★ パスワードハッシュ化のために werkzeug をインポート ★★★
from werkzeug.security import generate_password_hash, check_password_hash


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
# 本番環境では必ず環境変数で安全なキーを設定してください。
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-should-be-changed')
if app.config['SECRET_KEY'] == 'dev-secret-key-should-be-changed':
    logging.warning("Flask SECRET_KEYが開発用のデフォルト値です。本番環境では必ず安全なキーを環境変数で設定してください。")

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
    # ★★★ ユーザーIDを紐付ける外部キーを追加 (オプションだが推奨) ★★★
    user_id = Column(Integer, index=True) # ForeignKey('users.id') を後で追加も可

    remind_email = Column(String(255), nullable=False)
    remind_at = Column(DateTime(timezone=True), nullable=False)
    message_body = Column(Text)
    gdrive_file_details = Column(JSONB)
    upload_time = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, default='pending')
    created_at = Column(DateTime(timezone=True), server_default=text('CURRENT_TIMESTAMP'))
    updated_at = Column(DateTime(timezone=True), server_default=text('CURRENT_TIMESTAMP'), onupdate=text('CURRENT_TIMESTAMP'))

# ★★★ User モデルを追加 ★★★
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(80), unique=True, nullable=False)
    email = Column(String(120), unique=True, nullable=False) # メールアドレスも必須・ユニークに
    password_hash = Column(String(255), nullable=False) # ハッシュ化されたパスワード
    created_at = Column(DateTime(timezone=True), server_default=text('CURRENT_TIMESTAMP'))
    # (オプション) Flask-Login を使う場合に必要なメソッド
    # is_active = True
    # is_authenticated = True
    # is_anonymous = False
    # def get_id(self): return str(self.id)
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

# ★★★ データベーステーブル作成関数 ★★★
def init_db():
    logging.info("データベーステーブルの初期化を確認・実行します...")
    try:
        # Base.metadata.create_all(bind=engine)
        # テーブルが存在しない場合のみ作成する方が安全
        with engine.connect() as connection:
            if not engine.dialect.has_table(connection, User.__tablename__):
                logging.info(f"テーブル '{User.__tablename__}' を作成します。")
                User.__table__.create(bind=engine)
            else:
                logging.info(f"テーブル '{User.__tablename__}' は既に存在します。")

            if not engine.dialect.has_table(connection, Reminder.__tablename__):
                logging.info(f"テーブル '{Reminder.__tablename__}' を作成します。")
                Reminder.__table__.create(bind=engine)
            else:
                logging.info(f"テーブル '{Reminder.__tablename__}' は既に存在します。")
        logging.info("データベーステーブルの初期化確認完了。")
    except Exception as e:
        logging.error(f"データベーステーブルの作成中にエラーが発生しました: {e}", exc_info=True)
        # エラー発生時はアプリケーションを停止させるか検討
        # sys.exit(1)


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

@app.route('/')
def index():
    """トップページ: ログインしていればアップロードページへ、そうでなければログインページへリダイレクト"""
    # ★★★ セッションに 'user_id' があるか確認 ★★★
    if 'user_id' in session:
        return redirect(url_for('upload'))
    else:
        return redirect(url_for('login'))

# ★★★ ユーザー登録ルートを追加 ★★★
@app.route('/register', methods=['GET', 'POST'])
def register():
    """ユーザー登録処理"""
    if 'user_id' in session: # ログイン済みならアップロードページへ
        return redirect(url_for('upload'))

    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        password_confirm = request.form.get('password_confirm')

        if not username or not email or not password or not password_confirm:
            flash('すべてのフィールドを入力してください。', 'warning')
            return render_template('register.html')

        if password != password_confirm:
            flash('パスワードが一致しません。', 'danger')
            return render_template('register.html')

        # パスワード強度チェック (任意だが推奨)
        if len(password) < 8:
             flash('パスワードは8文字以上で設定してください。', 'warning')
             return render_template('register.html')

        db = SessionLocal()
        try:
            # ユーザー名とメールアドレスの重複チェック
            existing_user = db.query(User).filter((User.username == username) | (User.email == email)).first()
            if existing_user:
                if existing_user.username == username:
                    flash('そのユーザー名は既に使用されています。', 'danger')
                else:
                    flash('そのメールアドレスは既に使用されています。', 'danger')
                return render_template('register.html')

            # 新規ユーザー作成
            new_user = User(username=username, email=email)
            new_user.set_password(password) # パスワードをハッシュ化して設定
            db.add(new_user)
            db.commit()
            logging.info(f"新規ユーザー登録成功: username={username}, email={email}")
            flash('ユーザー登録が完了しました。ログインしてください。', 'success')
            return redirect(url_for('login'))

        except Exception as e:
            db.rollback()
            logging.error(f"ユーザー登録中にエラーが発生しました: {e}", exc_info=True)
            flash('ユーザー登録中にエラーが発生しました。', 'danger')
            return render_template('register.html')
        finally:
            db.close()

    # GETリクエストの場合
    return render_template('register.html')

# ★★★ ログインルートを修正 ★★★
@app.route('/login', methods=['GET', 'POST'])
def login():
    """ログイン処理"""
    if 'user_id' in session: # ログイン済みならアップロードページへ
        return redirect(url_for('upload'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if not username or not password:
            flash('ユーザー名とパスワードを入力してください。', 'warning')
            return render_template('login.html')

        db = SessionLocal()
        try:
            # ユーザー名でユーザーを検索
            user = db.query(User).filter_by(username=username).first()

            # ユーザーが存在し、パスワードが正しいかチェック
            if user and user.check_password(password):
                # ログイン成功: セッションにユーザーIDとユーザー名を保存
                session['user_id'] = user.id
                session['username'] = user.username # usernameも保存しておくと便利
                logging.info(f"ログイン成功: user_id={user.id}, username={user.username}")
                flash('ログインしました。', 'success')
                # ログイン後のリダイレクト先 (例: アップロードページ)
                next_url = request.args.get('next') # ログイン前にアクセスしようとしたページがあればそこへ
                return redirect(next_url or url_for('upload'))
            else:
                # ログイン失敗
                logging.warning(f"ログイン失敗試行: username={username}")
                flash('ユーザー名またはパスワードが正しくありません。', 'danger')
                return render_template('login.html')
        except Exception as e:
            logging.error(f"ログイン処理中にエラーが発生しました: {e}", exc_info=True)
            flash('ログイン処理中にエラーが発生しました。', 'danger')
            return render_template('login.html')
        finally:
            db.close()

    # GETリクエストの場合
    return render_template('login.html')

# ★★★ ログアウトルートを修正 ★★★
@app.route('/logout')
def logout():
    """ログアウト処理"""
    # セッションからユーザー情報を削除
    user_id = session.pop('user_id', None)
    username = session.pop('username', None)
    if username:
        logging.info(f"ユーザーログアウト: user_id={user_id}, username={username}")
    else:
        logging.info("ユーザーがログアウトしました (セッション情報なし)。")
    flash('ログアウトしました。', 'info')
    return redirect(url_for('login')) # ログインページへリダイレクト

# ★★★ /upload ルートを修正 ★★★
@app.route('/upload', methods=['GET', 'POST'])
def upload():
    """ファイルアップロードページ (要ログイン)"""
    # --- ログイン状態のチェック ---
    if 'user_id' not in session: # user_id でチェック
        flash('このページにアクセスするにはログインが必要です。', 'warning')
        return redirect(url_for('login', next=request.url)) # ログイン後に戻ってこれるように

    # --- GET リクエスト ---
    if request.method == 'GET':
        username = session.get('username', 'ゲスト') # セッションからユーザー名取得
        return render_template('upload.html', username=username)

    # --- POST リクエスト (ファイルアップロード処理) ---
    if request.method == 'POST':
        user_id = session['user_id'] # ログインユーザーのIDを取得
        username = session['username']
        logging.info(f"--- ファイルアップロード処理開始 (User: {username}, ID: {user_id}) ---")

        # ... (ファイル、日時、メール等の取得処理は変更なし) ...
        if 'file' not in request.files: flash("ファイルが選択されていません。", "warning"); return redirect(url_for('upload'))
        files = request.files.getlist('file')
        if not files or files[0].filename == '': flash("ファイルが選択されていません。", "warning"); return redirect(url_for('upload'))
        remind_datetime_str = request.form.get('remind_datetime')
        remind_email = request.form.get('remind_email')
        message_body = request.form.get('message', '')
        upload_time = datetime.now(timezone.utc)
        if not remind_datetime_str or not remind_email: flash("開封日時と通知先メールアドレスは必須です。", "danger"); return redirect(url_for('upload'))

        # ... (日時処理は変更なし) ...
        try:
            remind_datetime_naive = datetime.strptime(remind_datetime_str, '%Y-%m-%dT%H:%M')
            jst = ZoneInfo("Asia/Tokyo")
            remind_datetime_jst = remind_datetime_naive.replace(tzinfo=jst)
            remind_datetime_utc = remind_datetime_jst.astimezone(timezone.utc)
            if remind_datetime_utc <= datetime.now(timezone.utc): flash("開封日時は未来の日時を指定してください。", "warning"); return redirect(url_for('upload'))
        except ValueError: flash("日時の形式が無効です。", "danger"); return redirect(url_for('upload'))
        except Exception as e_tz: logging.error(f"タイムゾーン処理中にエラー: {e_tz}", exc_info=True); flash("日時の処理中にエラーが発生しました。", "danger"); return redirect(url_for('upload'))

        # ... (ファイルアップロード処理は変更なし) ...
        uploaded_file_details = []
        temp_file_paths = []
        save_error = False
        upload_error = False
        if not os.path.exists(TEMP_UPLOAD_FOLDER):
            try: os.makedirs(TEMP_UPLOAD_FOLDER)
            except OSError as e: logging.error(f"一時アップロードフォルダの作成に失敗: {TEMP_UPLOAD_FOLDER}, Error: {e}"); flash("サーバーエラーが発生しました (Code: UF1)。", "danger"); return redirect(url_for('upload'))
        for file in files:
            if file and file.filename:
                original_filename = file.filename
                safe_filename = f"{datetime.now().timestamp()}_{original_filename}"
                file_path = os.path.join(TEMP_UPLOAD_FOLDER, safe_filename)
                try:
                    file.save(file_path)
                    temp_file_paths.append(file_path)
                    file_id = upload_to_gdrive(file_path, original_filename, FOLDER_ID)
                    if file_id: uploaded_file_details.append({'id': file_id, 'name': original_filename})
                    else: upload_error = True; flash(f"ファイル '{original_filename}' のGoogle Driveへのアップロードに失敗しました。", "warning")
                except Exception as e: logging.error(f"ファイル処理中にエラー ({original_filename}): {e}", exc_info=True); save_error = True; flash(f"ファイル '{original_filename}' の一時保存中にエラー。", "danger"); break
        if save_error or upload_error:
            for fp in temp_file_paths:
                if os.path.exists(fp): 
                    try: 
                        os.remove(fp); 
                    except Exception as e_rem: 
                        logging.error(f"エラー時一時ファイル削除失敗: {fp}, Error: {e_rem}")
            return redirect(url_for('upload'))
        if not uploaded_file_details:
            flash("アップロードに成功したファイルがありませんでした。リマインダーは設定されません。", "warning")
            # 一時ファイル削除 (念のため)
            logging.info("アップロード成功ファイルがないため、一時ファイルを削除します。")
            for fp in temp_file_paths:
                if os.path.exists(fp): 
                    try: 
                        os.remove(fp); 
                    except Exception as e_rem: 
                        logging.error(f"アップロード成功ファイル無の場合の一時ファイル削除失敗: {fp}, Error: {e_rem}")
            return redirect(url_for('upload'))

        # ★★★ DB保存処理で user_id を追加 ★★★
        db = SessionLocal()
        try:
            new_reminder = Reminder(
                user_id=user_id, # ユーザーIDを保存
                remind_email=remind_email,
                remind_at=remind_datetime_utc,
                message_body=message_body,
                gdrive_file_details=uploaded_file_details,
                upload_time=upload_time,
                status='pending'
            )
            db.add(new_reminder)
            db.commit()
            logging.info(f"リマインダー情報をDBに保存 (User ID: {user_id})。ID: {new_reminder.id}, Email: {remind_email}, RemindAt(UTC): {remind_datetime_utc}")
        except Exception as e_db:
            db.rollback()
            logging.error(f"DBへのリマインダー保存中にエラー (User ID: {user_id}): {e_db}", exc_info=True)
            flash("リマインダー情報のデータベース保存に失敗しました。", "danger")
            for fp in temp_file_paths:
                if os.path.exists(fp):
                    try:
                        os.remove(fp);
                    except Exception as e_rem:
                        logging.error(f"DBエラー後の一時ファイル削除失敗: {fp}, Error: {e_rem}")
            return redirect(url_for('upload'))
        finally:
            db.close()

        # ... (正常終了時の一時ファイル削除、完了メッセージは変更なし) ...
        logging.info("処理正常終了のため、一時ファイルを削除します。")
        for fp in temp_file_paths:
            if os.path.exists(fp):
                try: 
                    os.remove(fp); 
                except Exception as e_rem: 
                    logging.error(f"正常終了時の一時ファイル削除失敗: {fp}, Error: {e_rem}")
        try:
            weekdays_jp = ["月", "火", "水", "木", "金", "土", "日"]; weekday_jp = weekdays_jp[remind_datetime_naive.weekday()]
            formatted_remind_date = remind_datetime_naive.strftime(f'%Y年%m月%d日({weekday_jp})')
            success_message = f"あなたのタイムカプセルは土の中深くに埋められました。開封予定日は{formatted_remind_date}です！"
            flash(success_message, 'success')
        except Exception as e_fmt: logging.warning(f"リマインダー日時フォーマットエラー: {e_fmt}"); flash("タイムカプセルは埋められましたが、完了メッセージ表示でエラー。", 'warning')

        return redirect(url_for('upload'))

# --- /run-cron ルート (変更なし) ---
@app.route('/run-cron', methods=['POST'])
def run_cron_job():
    # ... (変更なし) ...
    logging.info("--- /run-cron エンドポイント受信 ---")
    CRON_SECRET_KEY = os.environ.get('CRON_SECRET_KEY')
    request_key = request.headers.get('X-Cron-Secret')
    if CRON_SECRET_KEY and request_key != CRON_SECRET_KEY:
        logging.warning(f"/run-cron: 不正なアクセス試行 (Secret: {request_key})")
        return jsonify({"msg": "Unauthorized"}), 401
    elif not CRON_SECRET_KEY:
        logging.warning("/run-cron: CRON_SECRET_KEY未設定のため認証スキップ")
    else:
        logging.info("/run-cron: Cron実行キー認証成功。")

    if process_pending_reminders is None:
         logging.error("/run-cron: process_pending_reminders 関数がインポートされていません。")
         return jsonify({"msg": "Internal server error: Reminder processing function not available."}), 500
    try:
        # スレッドで実行することで、HTTPリクエストがタイムアウトするのを防ぐ
        thread = threading.Thread(target=process_pending_reminders)
        thread.start()
        logging.info("process_pending_reminders をバックグラウンドで開始しました。")
        return jsonify({"msg": "Reminder check process started in background."}), 202
    except Exception as e:
        logging.error(f"/run-cron でエラーが発生: {e}", exc_info=True)
        return jsonify({"msg": "Error initiating reminder processing."}), 500

# --- アプリケーション実行 ---
if __name__ == "__main__":
    # ★★★ アプリ起動時にDBテーブルを初期化 ★★★
    init_db()

    # ... (起動時のチェックは変更なし、APP_PASSWORD のチェックは不要になる) ...
    if app.config['SECRET_KEY'] == 'dev-secret-key-should-be-changed': logging.warning("Flask SECRET_KEYがデフォルト値のままです。")
    if not os.path.exists(SERVICE_ACCOUNT_FILE): logging.error(f"サービスアカウントファイルが見つかりません: {SERVICE_ACCOUNT_FILE}")
    if not os.environ.get('CRON_SECRET_KEY'): logging.warning("CRON_SECRET_KEYが未設定です。")
    # if not os.environ.get('APP_PASSWORD') or os.environ.get('APP_PASSWORD') == 'your_password': logging.error("★★★ 起動時警告: アプリケーションのパスワード (APP_PASSWORD) が未設定またはデフォルト値です。 ★★★") # 不要

    logging.info("アプリケーションを起動します...")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=False, use_reloader=True)
