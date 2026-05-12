# InstaGrab v2 — 公開・収益化対応版

## ファイル構成
```
instagrab/
├── server.py           # FastAPI バックエンド
├── requirements.txt    # Python依存ライブラリ
├── Dockerfile          # VPSデプロイ用
├── render.yaml         # Renderデプロイ用
├── static/
│   ├── index.html      # メインUI（広告枠×3・FAQ付き）
│   ├── privacy.html    # プライバシーポリシー
│   └── terms.html      # 利用規約
```

## ローカル起動
```bash
pip install -r requirements.txt
python server.py
# → http://localhost:8000
```

## Renderデプロイ（無料）
1. render.com でアカウント作成
2. このフォルダをGitHubにpush
3. New Web Service → リポジトリ選択 → Deploy

## VPS（Docker）
```bash
docker build -t instagrab .
docker run -d -p 80:8000 instagrab
```

## AdSense設定
static/index.html 内のコメントアウトされた<ins>タグを有効にして
data-ad-client と data-ad-slot に自分のIDを入れる。

## AdSense審査チェックリスト
- [x] プライバシーポリシーページ
- [x] 利用規約ページ
- [x] FAQコンテンツ
- [ ] お問い合わせメールを実際のアドレスに変更
- [ ] 独自ドメイン取得
- [ ] 月1,000PV以上の実績
- [ ] HTTPS対応
