# Awalia

ふたりのあわいに、絆を育む。

Awalia は、AI のパートナーと会話を重ねて、関係を育てていく Windows 向けのアプリです。パートナーは、あなたが質問に答えながら迎えます。話した日のことは日記やノートに少しずつ残り、パートナーはそれを覚えたまま、次の日もあなたと話します。

## 動く環境

- Windows 10 / 11（64 ビット）
- AI のサービスのアカウント（OpenRouter、Google Gemini、Anthropic Claude、OpenAI のどれか）。返事を作るたびに、そのサービスの利用料金がかかります。料金はサービスへ直接支払います。

## 入手と更新

[Releases](https://github.com/whitelabel1940/awalia/releases) からインストーラーを取得してください。管理者権限は要りません。新しい版が出ると、アプリの「お知らせ」から更新できます。

コード署名は準備中です。インストール時に Windows やウイルス対策ソフトの警告が出ることがあります。

## データとプライバシー

- 会話、設定、API キー、パートナーは、この PC の `%LOCALAPPDATA%\Awalia` に保存されます。アンインストールしても消えません。
- 会話は、返事を作るために、あなたが選んだ AI のサービスへ送られます。
- お知らせと新しい版の確認のため、このリポジトリの公開ファイルを起動後と一日一回読みに行きます。
- 利用状況の送信は、最初の画面か設定であなたがオンにした時だけ行います。会話や設定の中身は送りません。

詳しくは [PRIVACY.md](PRIVACY.md) を読んでください。

## ソースから動かす

Python 3.14 が必要です。

```
python -m venv .venv
.venv\Scripts\pip install --require-hashes -r requirements.lock
.venv\Scripts\python -m cbs.web
```

ブラウザで `http://127.0.0.1:8765` を開きます。ソースから動かした時は、データをこのフォルダの `data/` と `characters/` に置きます。

デスクトップ版の殻（`desktop/`）は Tauri 2 で作っています。Node.js と Rust が必要です。インストーラーの作り方は `release/build.py` にあります。

## フィードバック

不具合や要望は、アプリの「お知らせ」にあるフィードバックのフォームから送ってください。

プルリクエストは受け付けていません。

## ライセンス

[GNU Affero General Public License v3.0](LICENSE)。同梱している第三者のソフトウェアのライセンスは、インストール先の `app/THIRD_PARTY_NOTICES.txt` にあります。
