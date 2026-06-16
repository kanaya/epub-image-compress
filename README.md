# epub-image-compress

EPUB 内の埋め込み画像を JPEG に変換し、指定サイズ以内に縮小する CLI ツールです。

## 機能

- EPUB を展開し、埋め込み画像（PNG / GIF / WebP など）を検出
- 1920×1080 以内にアスペクト比を保ったまま縮小（デフォルト）
- PNG / GIF / WebP / HEIC などを JPEG に変換（透過は白背景で合成）
- 変換後の EPUB を新しいファイルとして保存

## 必要条件

- Python 3.10+
- Pillow

## インストール

```bash
pip install -r requirements.txt
```

## 使い方

```bash
python epub_image_compress.py input.epub
```

出力はデフォルトで `input_compressed.epub` になります。

```bash
python epub_image_compress.py input.epub -o output.epub
python epub_image_compress.py input.epub --max-width 1920 --max-height 1080 --quality 85 -v
```

### オプション

| オプション | 説明 |
|-----------|------|
| `-o`, `--output` | 出力ファイルパス |
| `--max-width` | 最大幅（デフォルト: 1920） |
| `--max-height` | 最大高さ（デフォルト: 1080） |
| `--quality` | JPEG 品質 1–95（デフォルト: 85） |
| `-v`, `--verbose` | 詳細ログを表示 |

## 注意

- 元の EPUB は変更されません
- すでに JPEG で、かつサイズが上限以内の画像はスキップされます
- SVG などベクター画像は対象外です
- 参照パス（HTML / OPF マニフェスト）は、拡張子変更時に自動更新されます
