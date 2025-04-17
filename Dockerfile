# ベースイメージを選択 (Python 3.10 の軽量版)
FROM python:3.10-slim

# 環境変数: Pythonがバッファリングしないように設定 (ログがすぐに見える)
ENV PYTHONUNBUFFERED=1

# 作業ディレクトリを設定
WORKDIR /app

# 依存関係ファイルをコピー
COPY requirements.txt requirements.txt

# 依存関係をインストール
# --no-cache-dir オプションでキャッシュを使わず、イメージサイズを少し削減
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードをコピー
COPY . .

# (Google API 認証情報の処理 - 後述)
# 例: 環境変数から credentials.json や token.pickle を復元するスクリプトを実行する場合
# COPY entrypoint.sh /entrypoint.sh
# RUN chmod +x /entrypoint.sh
# ENTRYPOINT ["/entrypoint.sh"]

# アプリケーションがリッスンするポートを公開 (Render は 10000 を推奨することが多い)
EXPOSE 10000

# アプリケーションの起動コマンド (Gunicorn を使用)
# --bind 0.0.0.0:10000 で全てのインターフェースのポート10000で待機
# test0320:app は test0320.py 内の Flask インスタンス 'app' を指す
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "test0320:app"]
