#!/usr/bin/env bash
#
# mac-cleanup.sh — Mac のストレージを調査・分類し、許可制で削除するツール
#
# 使い方:
#   bash mac-cleanup.sh            # スキャンのみ（何も削除しない）= デフォルト
#   bash mac-cleanup.sh --scan     # 同上（明示）
#   bash mac-cleanup.sh --clean    # カテゴリごとに y/N 確認しながら削除
#
# 設計方針:
#   - デフォルトは「見るだけ」。削除は --clean のときだけ。
#   - --clean でも、各カテゴリで明示的に y を押さない限り削除しない。
#   - システム領域(/System 等)やアプリのデータ本体は対象外。
#   - 「安全」「要確認」「情報のみ」の3段階に分類して判断を補助する。

set -u

MODE="scan"
case "${1:-}" in
  --clean) MODE="clean" ;;
  --scan|"") MODE="scan" ;;
  -h|--help)
    echo "Usage: bash mac-cleanup.sh [--scan|--clean]"
    echo "  --scan  (default) 調査して分類を表示するだけ。削除しない。"
    echo "  --clean カテゴリごとに確認しながら削除する。"
    exit 0 ;;
  *) echo "不明なオプション: $1 (--help を参照)"; exit 1 ;;
esac

# ---- 表示ヘルパ ---------------------------------------------------------------
bold() { printf "\033[1m%s\033[0m\n" "$1"; }
green() { printf "\033[32m%s\033[0m\n" "$1"; }
yellow() { printf "\033[33m%s\033[0m\n" "$1"; }
red() { printf "\033[31m%s\033[0m\n" "$1"; }
line() { printf -- "--------------------------------------------------------------\n"; }

# パスの容量を人間可読で返す（存在しなければ空）
size_of() {
  [ -e "$1" ] && du -sh "$1" 2>/dev/null | awk '{print $1}'
}

# あるパス（globでも可）の合計を表示
show_path() {
  local label="$1"; shift
  local total found=0
  for p in "$@"; do
    for match in $p; do
      if [ -e "$match" ]; then
        found=1
        local s
        s=$(du -sh "$match" 2>/dev/null | awk '{print $1}')
        printf "    %-8s %s\n" "${s:-?}" "$match"
      fi
    done
  done
  [ "$found" = 0 ] && printf "    %-8s (該当なし)\n" "-"
}

# --clean 用: 確認してから中身を削除（パス自体は残す）
confirm_delete_contents() {
  local label="$1"; shift
  [ "$MODE" != "clean" ] && return 0
  local exists=0
  for p in "$@"; do for m in $p; do [ -e "$m" ] && exists=1; done; done
  [ "$exists" = 0 ] && return 0
  printf "\n"
  read -r -p "  >>> [$label] を削除しますか? (y/N) " ans
  if [ "${ans:-N}" = "y" ] || [ "${ans:-N}" = "Y" ]; then
    for p in "$@"; do
      for m in $p; do
        [ -e "$m" ] && rm -rf "$m" && echo "      削除: $m"
      done
    done
    green   "  済: [$label] を削除しました。"
  else
    yellow "  スキップ: [$label]"
  fi
}

# ==============================================================================
bold "Mac ストレージ調査ツール  (mode: $MODE)"
line
df -h / | sed 's/^/  /'
line

# ---- 0. ホーム配下の大きいフォルダ TOP15 ------------------------------------
bold "ホーム配下で容量の大きいフォルダ TOP15"
du -sh "$HOME"/* "$HOME"/.* 2>/dev/null | sort -rh | head -15 | sed 's/^/    /'
line

# ==============================================================================
# 安全に削除できるもの（再生成される / 不要物）
# ==============================================================================
green "■ 安全に削除できる（キャッシュ・ログ・ゴミ箱など。再生成されます）"

bold "ゴミ箱"
show_path "Trash" "$HOME/.Trash/*"
confirm_delete_contents "ゴミ箱" "$HOME/.Trash/*"

bold "ユーザーキャッシュ"
show_path "Caches" "$HOME/Library/Caches/*"
confirm_delete_contents "ユーザーキャッシュ" "$HOME/Library/Caches/*"

bold "ユーザーログ"
show_path "Logs" "$HOME/Library/Logs/*"
confirm_delete_contents "ユーザーログ" "$HOME/Library/Logs/*"
line

# ==============================================================================
# 開発者向けキャッシュ（あれば大物。基本安全に消せる）
# ==============================================================================
green "■ 開発者向けキャッシュ（あれば大容量。再生成されます）"

bold "Xcode DerivedData / Archives / iOS DeviceSupport"
show_path "Xcode" \
  "$HOME/Library/Developer/Xcode/DerivedData/*" \
  "$HOME/Library/Developer/Xcode/Archives/*" \
  "$HOME/Library/Developer/Xcode/iOS DeviceSupport/*"
confirm_delete_contents "Xcode キャッシュ" \
  "$HOME/Library/Developer/Xcode/DerivedData/*" \
  "$HOME/Library/Developer/Xcode/Archives/*"

bold "パッケージマネージャのキャッシュ (npm/yarn/pnpm/pip)"
show_path "pkg-cache" \
  "$HOME/.npm/_cacache" \
  "$HOME/Library/Caches/Yarn" \
  "$HOME/Library/pnpm/store" \
  "$HOME/Library/Caches/pip"
confirm_delete_contents "パッケージキャッシュ" \
  "$HOME/.npm/_cacache" \
  "$HOME/Library/Caches/Yarn" \
  "$HOME/Library/Caches/pip"

if command -v brew >/dev/null 2>&1; then
  bold "Homebrew キャッシュ"
  show_path "brew" "$(brew --cache 2>/dev/null)"
  if [ "$MODE" = "clean" ]; then
    read -r -p "  >>> [Homebrew] brew cleanup -s を実行しますか? (y/N) " a
    { [ "${a:-N}" = "y" ] || [ "${a:-N}" = "Y" ]; } && brew cleanup -s && green "  済: brew cleanup" || yellow "  スキップ"
  fi
fi

if command -v xcrun >/dev/null 2>&1; then
  bold "iOS シミュレータ（未使用デバイス）"
  echo "    'xcrun simctl delete unavailable' で未使用分を削除できます。"
  if [ "$MODE" = "clean" ]; then
    read -r -p "  >>> [Simulator] 未使用デバイスを削除しますか? (y/N) " a
    { [ "${a:-N}" = "y" ] || [ "${a:-N}" = "Y" ]; } && xcrun simctl delete unavailable && green "  済: simctl" || yellow "  スキップ"
  fi
fi
line

# ==============================================================================
# 要確認（中身を見て自分で判断すべきもの。自動削除しない）
# ==============================================================================
yellow "■ 要確認（大事なものが混ざる可能性あり。自動削除はしません）"

bold "ダウンロードフォルダ（大きい順 TOP10）"
ls -lahS "$HOME/Downloads" 2>/dev/null | head -11 | sed 's/^/    /'
echo "    → 不要なものを手動で削除してください。"

bold "node_modules（プロジェクトの依存。場所のみ表示）"
find "$HOME" -type d -name node_modules -prune 2>/dev/null | head -20 | while read -r d; do
  printf "    %-8s %s\n" "$(size_of "$d")" "$d"
done
echo "    → 使っていないプロジェクトのものだけ削除を検討。"

bold "iOS デバイスのバックアップ"
show_path "iOS-backup" "$HOME/Library/Application Support/MobileSync/Backup/*"
echo "    → 古い端末のバックアップなら削除候補。中身を確認のうえ手動で。"

if command -v docker >/dev/null 2>&1; then
  bold "Docker 使用量"
  docker system df 2>/dev/null | sed 's/^/    /'
  echo "    → 'docker system prune -a' で未使用分を削除できます（内容を理解のうえ）。"
fi
line

# ==============================================================================
red "■ 触らない方がよい領域"
echo "    /System, /Library(システム), アプリのデータ本体"
echo "    (~/Library/Application Support 配下など) はこのツールの対象外です。"
line

bold "完了しました。"
if [ "$MODE" = "scan" ]; then
  echo "削除するには:  bash mac-cleanup.sh --clean"
  echo "（--clean でもカテゴリごとに y/N を確認します。許可したものだけ削除されます）"
fi
