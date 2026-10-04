"""移行ファイル - このツールのデータを、別のツールや別のサーバーへ持ち出す。

1つの zip に、どこへ持っていっても使える形でまとめる:

    manifest.json          形式・スキーマの版・件数・除外したもの・同梱ファイル
    README.md              中身の説明と、取り込み方（日本語）
    rules.json             このツールの判定ルールを決めている定数
    data/<table>.json      全行。取り込み（snsauto migrate import）はこれを使う
    csv/<table>.csv        スプレッドシート用。Excel で文字化けしないよう BOM 付き
    schema/<dialect>.sql   PostgreSQL / MySQL / SQLite の CREATE TABLE 文
    schema/rows.schema.json 各テーブルの行の JSON Schema
    media/...              --with-media のときだけ。動画・絵コンテ画像・レポート

**認証情報は入れない。** SNS のトークンは、それを発行した OAuth アプリ
（このツールの Facebook アプリや Google Cloud プロジェクト）に紐づいており、
別のツールでは使えない。使えないものを持ち出しても漏えいの危険だけが残る。
除外は3段で行う:

1. 認証情報の列（トークン、パスワードハッシュ）を落とす
2. JSON の中を再帰的にたどり、鍵名が token / secret / password などのものを
   落とす。Instagram の連携は長期有効のユーザートークンを
   ``social_accounts.meta.user_token`` に持っており、列だけ落とすとここから漏れる
3. 書き出した全ファイルを、DB と設定にある**実際の秘密の値**と照合する。
   エラー文やジョブの記録に URL ごと紛れ込んだトークンもこれで捕まる。
   1つでも残っていれば移行ファイルを消して失敗させる
"""

from __future__ import annotations

import csv
import enum
import hashlib
import io
import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import Enum as SAEnum
from sqlalchemy import func, select, text
from sqlalchemy.schema import CreateIndex, CreateTable

from .models import Base

FORMAT = "snsauto-migration"
FORMAT_VERSION = 1

# 列ごと落とすもの。取り込み時に必須の列は、使えない値で埋める（下の IMPORT_FILL）。
SECRET_COLUMNS: dict[str, tuple[str, ...]] = {
    "social_accounts": ("access_token", "refresh_token", "token_secret"),
    "users": ("password_hash",),
}

# 取り込み先で NOT NULL の秘密の列を何で埋めるか。どちらも「使えない」値。
# "!" で始まるハッシュは verify_password が形式違いとして必ず拒否する。
IMPORT_FILL = {
    ("social_accounts", "access_token"): "",
    ("users", "password_hash"): "!migrated",
}

# ファイルの場所を持つ列。--with-media で中身を同梱し、取り込み時に張り替える。
FILE_COLUMNS: dict[str, tuple[str, ...]] = {
    "renders": ("path",),
    "shots": ("image_path", "clip_path"),
    "clip_assets": ("path",),
    "reports": ("html_path", "pdf_path"),
}

# JSON の中で落とす鍵。末尾で判定するので user_token / client_secret / api_key は
# 落ち、文字解析の結果に出てくる tokens や token_count は残る。
_SECRET_KEY = re.compile(
    r"(?i)^(?:.*_)?(?:token|secret|password|passwd|api_?key|authorization|cookie)$"
)

# 文字列の中に URL やヘッダーごと紛れ込んだ秘密。値だけ伏せる。
_SECRET_IN_TEXT = (
    (re.compile(r"(?i)\b(access_token|refresh_token|fb_exchange_token|client_secret"
                r"|api_key|key|token)=([^&\s\"'<>]+)"), r"\1=***"),
    (re.compile(r"(?i)\b(Bearer|OAuth)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
)

# これより短い値は照合に使わない（偶然の一致で誤検出するため）。
_CANARY_MIN = 8


class MigrationError(RuntimeError):
    """移行ファイルの書き出し・取り込みができない。"""


@dataclass
class Manifest:
    format: str
    format_version: int
    schema_revision: str | None
    exported_at: str
    scope: str
    tables: dict[str, int]
    excluded: list[str] = field(default_factory=list)
    media: list[dict] = field(default_factory=list)
    detached: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, indent=2)


# ---------------- 値の変換 ----------------


def _to_json_value(value, column):
    if value is None:
        return None
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)   # 保存は常に UTC
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def _from_json_value(value, column):
    if value is None:
        return None
    if isinstance(column.type, SAEnum) and column.type.enum_class is not None:
        return column.type.enum_class(value)
    python_type = None
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        pass
    if python_type is datetime and isinstance(value, str):
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    return value


def _scrub(value, removed: list):
    """JSON の中の秘密の鍵を落とし、文字列中の秘密を伏せる。"""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY.match(key):
                removed.append(item)
                continue
            out[key] = _scrub(item, removed)
        return out
    if isinstance(value, list):
        return [_scrub(item, removed) for item in value]
    if isinstance(value, str):
        for pattern, replacement in _SECRET_IN_TEXT:
            value = pattern.sub(replacement, value)
    return value


def _strings_in(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings_in(item)


# ---------------- 範囲（全体か、1プロジェクトか） ----------------


def _select_rows(session, project_id: int | None):
    """テーブルごとの出力行。1プロジェクトのときは外部キーをたどって範囲を決める。

    - project_id 列を持つ表は、その値が一致する行
    - それ以外は、外部キーの親が出力に含まれる行
    - どちらの手がかりも無い表（利用者、素材ライブラリ）は全体出力のときだけ
    - 親が出力に無い参照は、NULL にできるなら NULL にし（件数を記録）、
      できなければ行ごと落とす

    project_id が NULL の行（複数の顧客で共有しているアカウントなど）は、
    1顧客分の出力には含めない。他社のアカウント情報が混ざるため。
    """
    kept: dict[str, list[dict]] = {}
    kept_ids: dict[str, set] = {}
    detached: dict[str, int] = {}

    for table in Base.metadata.sorted_tables:
        rows = [dict(row._mapping) for row in session.execute(select(table)).all()]
        foreign = [fk for fk in table.foreign_keys if fk.column.table.name != table.name]

        if project_id is not None:
            if table.name == "projects":
                # 起点そのもの。これを落とすと、すべての子が親を失って消える。
                rows = [r for r in rows if r.get("id") == project_id]
            elif "project_id" in table.c:
                rows = [r for r in rows if r.get("project_id") == project_id]
            elif not foreign:
                rows = []

            surviving = []
            for row in rows:
                drop = False
                for fk in foreign:
                    name = fk.parent.name
                    parent = fk.column.table.name
                    if row.get(name) is None or row[name] in kept_ids.get(parent, set()):
                        continue
                    if fk.parent.nullable:
                        row[name] = None
                        key = f"{table.name}.{name}"
                        detached[key] = detached.get(key, 0) + 1
                    else:
                        drop = True
                        break
                if not drop:
                    surviving.append(row)
            rows = surviving

        kept[table.name] = rows
        kept_ids[table.name] = {r["id"] for r in rows if "id" in r}
    return kept, detached


# ---------------- 書き出し ----------------


def _schema_revision(session) -> str | None:
    # 失敗してから rollback する形にすると、呼び出し側の未確定の変更まで
    # 巻き戻してしまう。表があるかを先に確かめる。
    from sqlalchemy import inspect

    if not inspect(session.connection()).has_table("alembic_version"):
        return None
    return session.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _settings_secrets(settings) -> list[str]:
    values = []
    for name in type(settings).model_fields:
        if _SECRET_KEY.match(name) or name.endswith(("_secret", "_token", "_key")):
            value = getattr(settings, name, None)
            if isinstance(value, str):
                values.append(value)
    return values


def _ddl(dialect_name: str) -> str:
    from sqlalchemy.dialects import mysql, postgresql, sqlite

    dialect = {"postgresql": postgresql, "mysql": mysql, "sqlite": sqlite}[dialect_name].dialect()
    parts = [f"-- snsauto のテーブル定義（{dialect_name}）。依存関係の順に並べてある。"]
    for table in Base.metadata.sorted_tables:
        parts.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            parts.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";")
    return "\n\n".join(parts) + "\n"


def _json_schema() -> dict:
    from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, Numeric

    def kind(column):
        t = column.type
        if isinstance(t, SAEnum):
            return {"type": "string", "enum": [e.value for e in t.enum_class]} \
                if t.enum_class else {"type": "string", "enum": list(t.enums)}
        if isinstance(t, Boolean):
            return {"type": "boolean"}
        if isinstance(t, Integer):
            return {"type": "integer"}
        if isinstance(t, (Float, Numeric)):
            return {"type": "number"}
        if isinstance(t, DateTime):
            return {"type": "string", "format": "date-time"}
        if isinstance(t, Date):
            return {"type": "string", "format": "date"}
        if isinstance(t, JSON):
            return {}
        return {"type": "string"}

    tables = {}
    for table in Base.metadata.sorted_tables:
        secret = set(SECRET_COLUMNS.get(table.name, ()))
        props, required = {}, []
        for column in table.columns:
            if column.name in secret:
                continue
            schema = kind(column)
            if column.nullable:
                schema = {"anyOf": [schema, {"type": "null"}]} if schema else {}
            elif column.name != "id" or not column.autoincrement:
                required.append(column.name)
            if column.comment:
                schema["description"] = column.comment
            fk = next(iter(column.foreign_keys), None)
            if fk is not None:
                schema["description"] = f"{fk.column.table.name}.{fk.column.name} を参照"
            props[column.name] = schema
        tables[table.name] = {
            "type": "array",
            "items": {"type": "object", "properties": props, "required": required},
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "snsauto 移行ファイルの data/<table>.json",
        "type": "object",
        "properties": tables,
    }


def rules() -> dict:
    """このツールの判定を決めている定数。別のツールで同じ動きを再現するための一覧。

    値はコードから直接読むので、ここに書いた数字とツールの動きは必ず一致する。
    """
    from .analytics import playbook, retention, stats
    from .creative import originality
    from .media import qc, trim
    from .platforms import accounts, captions
    from .research import fingerprint

    def entry(value, meaning):
        if isinstance(value, dict):
            value = {getattr(k, "value", k): v for k, v in value.items()}
        return {"value": value, "meaning": meaning}

    return {
        "無音カット": {
            "MIN_GAP_SEC": entry(trim.MIN_GAP_SEC, "これ未満の間は切らない（促音・子音の閉鎖区間）"),
            "HANDLE_SEC": entry(trim.HANDLE_SEC, "切る場合も発話の前後に残す余白（語頭・語尾）"),
            "CONSONANT_MARGIN_DB": entry(
                trim.CONSONANT_MARGIN_DB,
                "削除範囲の高域(4-8kHz)がファイル自身の高域床からこれ以上出ていれば無声子音として残す"),
            "NOISE_FLOOR_DB": entry(trim.NOISE_FLOOR_DB, "無音の候補に挙げる音量（採否は上の3つで決める）"),
        },
        "統計（小さい標本）": {
            "BOOTSTRAP_SEED": entry(stats.BOOTSTRAP_SEED, "ブートストラップの乱数種。結果を再現可能にする"),
            "RELIABILITY_BANDS": entry(
                [list(band) for band in stats.RELIABILITY_BANDS],
                "件数ごとの信頼度（これ未満なら左の区分）"),
        },
        "伸ばす仕組み（自社の実績からの学習）": {
            "MIN_POSTS": entry(playbook.MIN_POSTS, "これ未満の投稿数では傾向を出さない"),
            "MIN_PER_VALUE": entry(playbook.MIN_PER_VALUE, "比較する値ごとに最低これだけの投稿が要る"),
        },
        "視聴維持": {
            "MIN_VIDEOS_FOR_PATTERN": entry(retention.MIN_VIDEOS_FOR_PATTERN, "傾向として言うのに要る動画数"),
            "MATERIAL_DROP": entry(retention.MATERIAL_DROP, "離脱点とみなす残存率の低下幅"),
            "HOOK_WINDOW_SEC": entry(retention.HOOK_WINDOW_SEC, "冒頭として評価する秒数"),
        },
        "出荷前チェック": {
            "TARGET_LUFS": entry(qc.TARGET_LUFS, "音量の目標（ラウドネス）"),
            "SAFE_TOP_MARGIN": entry(qc.SAFE_TOP_MARGIN, "上端の安全領域（px, 1920高）"),
        },
        "台本の独自性": {
            "RUN_THRESHOLD": entry(originality.RUN_THRESHOLD, "競合とこれ以上の連続一致で流用とみなす（文字数）"),
            "MAX_REGENERATIONS": entry(originality.MAX_REGENERATIONS, "流用と判定したときの再生成の上限"),
        },
        "音源の共通性": {
            "SAME_SOUND_THRESHOLD": entry(fingerprint.SAME_SOUND_THRESHOLD, "同じ音源とみなす相関"),
        },
        "投稿": {
            "DAILY_POST_CAP": entry(accounts.DAILY_POST_CAP, "24時間あたりの投稿上限（自前の安全弁）"),
            "MAX_GIVEBACK": entry(captions.MAX_GIVEBACK, "ハッシュタグを守るために本文を削れる割合の上限"),
        },
    }


def _readme(manifest: Manifest) -> str:
    counts = "\n".join(f"| {name} | {count} |" for name, count in manifest.tables.items() if count)
    excluded = "\n".join(f"- {item}" for item in manifest.excluded) or "- なし"
    media = (f"{len(manifest.media)}件を media/ に同梱" if manifest.media
             else "同梱していません（--with-media で同梱できます）")
    detached = "\n".join(
        f"- {key}: {count}件（範囲外を指していたので空にしました）"
        for key, count in manifest.detached.items()
    )
    return f"""# snsauto 移行ファイル

- 書き出し日時: {manifest.exported_at}
- 範囲: {manifest.scope}
- スキーマの版: {manifest.schema_revision or "不明"}
- 動画・画像: {media}

## 件数

| テーブル | 件数 |
|---|---:|
{counts}

## 入っていないもの

{excluded}

SNS のトークンは、発行した OAuth アプリ（このツールの Facebook アプリや Google Cloud
プロジェクト）に紐づいていて、別のツールでは使えません。使えないものを持ち出すと
漏えいの危険だけが残るので、入れていません。移行先で各アカウントを連携し直してください。
{("" if not detached else chr(10) + "## 範囲外を指していた参照" + chr(10) + chr(10) + detached + chr(10))}
## ファイルの使い方

| 移行先 | 使うファイル |
|---|---|
| 別のサーバーの snsauto | この zip のまま `snsauto migrate import <zip>` |
| PostgreSQL / MySQL を使う別システム | `schema/postgresql.sql` か `schema/mysql.sql` で表を作り、`data/*.json` を流し込む |
| スプレッドシート（Excel / Googleスプレッドシート / Notion / Airtable） | `csv/*.csv`。UTF-8（BOM付き）なので Excel でも文字化けしません |
| 自作のツール・ノーコードツール | `data/*.json` と、項目の型を書いた `schema/rows.schema.json` |
| 判定の基準だけを移したい | `rules.json`（無音カット・統計・出荷前チェックなどの基準値と意味） |

日時はすべて UTC の ISO 8601（例: 2026-09-29T04:13:15+00:00）です。
JSON 型の列（ブランド設定・台本の行など）は、CSV では JSON 文字列のまま入っています。

## snsauto に取り込むとき

```bash
snsauto migrate inspect この.zip     # 中身の確認（取り込まない）
snsauto migrate import  この.zip     # 空のDBにだけ取り込めます
snsauto user password you@example.com  # パスワードは持ち出していないので再設定
```

連携アカウントは「再連携が必要」の状態で入ります。Web UI の /accounts から連携し直してください。
"""


def export_bundle(
    session, destination: str | Path, project_id: int | None = None,
    with_media: bool = False, settings=None,
) -> tuple[Path, Manifest]:
    """移行ファイル（zip）を書き出す。書き出した場所と manifest を返す。"""
    from .config import get_settings

    settings = settings or get_settings()
    destination = Path(destination)
    rows_by_table, detached = _select_rows(session, project_id)

    if project_id is not None:
        from .models import Project

        project = session.get(Project, project_id)
        scope = f"プロジェクト「{project.name}」のみ（id={project_id}）" if project else str(project_id)
    else:
        scope = "全体"

    canaries: set[str] = set(v for v in _settings_secrets(settings) if v)
    excluded: list[str] = []
    for table, columns in SECRET_COLUMNS.items():
        for column in columns:
            excluded.append(f"{table}.{column}（認証情報）")

    work = Path(tempfile.mkdtemp(prefix="snsauto-migration-"))
    try:
        (work / "data").mkdir()
        (work / "csv").mkdir()
        (work / "schema").mkdir()
        media: list[dict] = []
        tables_out: dict[str, int] = {}
        scrubbed_keys = 0

        for table in Base.metadata.sorted_tables:
            secret_columns = set(SECRET_COLUMNS.get(table.name, ()))
            out_rows = []
            for row in rows_by_table[table.name]:
                record = {}
                for column in table.columns:
                    value = row.get(column.name)
                    if column.name in secret_columns:
                        if isinstance(value, str):
                            canaries.add(value)
                        continue
                    value = _to_json_value(value, column)
                    removed: list = []
                    value = _scrub(value, removed)
                    for secret in removed:
                        canaries.update(_strings_in(secret))
                    scrubbed_keys += len(removed)
                    record[column.name] = value
                out_rows.append(record)

            if with_media:
                for record in out_rows:
                    for column in FILE_COLUMNS.get(table.name, ()):
                        source = record.get(column)
                        if not source or not Path(source).is_file():
                            continue
                        name = f"media/{table.name}/{record['id']}_{column}{Path(source).suffix}"
                        target = work / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
                        digest = hashlib.sha256(target.read_bytes()).hexdigest()
                        media.append({"table": table.name, "id": record["id"],
                                      "column": column, "original": source,
                                      "file": name, "bytes": target.stat().st_size,
                                      "sha256": digest})

            tables_out[table.name] = len(out_rows)
            (work / "data" / f"{table.name}.json").write_text(
                json.dumps(out_rows, ensure_ascii=False, indent=1), encoding="utf-8")
            _write_csv(work / "csv" / f"{table.name}.csv", table, out_rows, secret_columns)

        if scrubbed_keys:
            excluded.append(f"JSON の中の秘密の項目 {scrubbed_keys}件（user_token など）")

        for dialect in ("postgresql", "mysql", "sqlite"):
            (work / "schema" / f"{dialect}.sql").write_text(_ddl(dialect), encoding="utf-8")
        (work / "schema" / "rows.schema.json").write_text(
            json.dumps(_json_schema(), ensure_ascii=False, indent=1), encoding="utf-8")
        (work / "rules.json").write_text(
            json.dumps(rules(), ensure_ascii=False, indent=1), encoding="utf-8")

        manifest = Manifest(
            format=FORMAT, format_version=FORMAT_VERSION,
            schema_revision=_schema_revision(session),
            exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            scope=scope, tables=tables_out, excluded=excluded, media=media,
            detached=detached,
        )
        (work / "manifest.json").write_text(manifest.to_json(), encoding="utf-8")
        (work / "README.md").write_text(_readme(manifest), encoding="utf-8")

        _assert_no_secrets(work, canaries)

        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.unlink()
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(work.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(work).as_posix())
        # 認証情報は除いているが、顧客の企画・実績は入っている。持ち主だけが読める形で置く。
        destination.chmod(0o600)
        return destination, manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _write_csv(path: Path, table, rows: list[dict], secret_columns: set) -> None:
    columns = [c.name for c in table.columns if c.name not in secret_columns]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([
            json.dumps(row.get(c), ensure_ascii=False)
            if isinstance(row.get(c), (dict, list)) else
            ("" if row.get(c) is None else row.get(c))
            for c in columns
        ])
    # BOM 付き。無いと Excel が Shift_JIS と取り違えて日本語が化ける。
    path.write_text("﻿" + buffer.getvalue(), encoding="utf-8")


def _assert_no_secrets(folder: Path, canaries: set[str]) -> None:
    """書き出した全ファイルに、秘密の値が1つも残っていないことを確かめる。"""
    needles = sorted({c for c in canaries if isinstance(c, str) and len(c) >= _CANARY_MIN},
                     key=len, reverse=True)
    if not needles:
        return
    for path in folder.rglob("*"):
        if not path.is_file() or path.parts[-2:-1] == ("media",):
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for needle in needles:
            if needle in body:
                raise MigrationError(
                    f"移行ファイルに認証情報が残っていたため、書き出しを中止しました"
                    f"（{path.relative_to(folder)}）。値は表示しません。"
                    "この記録の中のトークンを伏せる処理が足りていません。"
                )


# ---------------- 読み込み ----------------


def _open(path: Path) -> tuple[Path, Path | None]:
    """zip なら一時フォルダに展開する。(中身のフォルダ, 後で消すフォルダ)"""
    if path.is_dir():
        return path, None
    if not zipfile.is_zipfile(path):
        raise MigrationError(f"移行ファイルではありません: {path}")
    work = Path(tempfile.mkdtemp(prefix="snsauto-import-"))
    with zipfile.ZipFile(path) as bundle:
        for member in bundle.namelist():
            # zip の中のパスで展開先の外へ書き出させない（../ や絶対パス）
            target = (work / member).resolve()
            if work.resolve() not in target.parents:
                shutil.rmtree(work, ignore_errors=True)
                raise MigrationError(f"不正なパスを含む移行ファイルです: {member}")
        bundle.extractall(work)
    return work, work


def read_manifest(path: str | Path) -> Manifest:
    folder, cleanup = _open(Path(path))
    try:
        raw = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MigrationError("manifest.json がありません。snsauto の移行ファイルではありません。") from exc
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    if raw.get("format") != FORMAT:
        raise MigrationError("snsauto の移行ファイルではありません。")
    return Manifest(**raw)


@dataclass
class ImportResult:
    tables: dict[str, int]
    accounts_to_reconnect: int
    users_to_reset: list[str]
    media_copied: int
    missing_files: int


def import_bundle(session, path: str | Path, settings=None) -> ImportResult:
    """移行ファイルを、空のDBに取り込む。"""
    from alembic.script import ScriptDirectory

    from .config import get_settings
    from .db import _alembic_config

    settings = settings or get_settings()
    folder, cleanup = _open(Path(path))
    try:
        manifest = read_manifest(folder)
        if manifest.format_version > FORMAT_VERSION:
            raise MigrationError(
                f"新しい形式の移行ファイルです（形式 {manifest.format_version}）。"
                "取り込む側の snsauto を更新してください。")

        # alembic.ini の script_location は相対パスなので、Config を直接作ると
        # リポジトリ以外のフォルダから実行したときに失敗する。db 側の設定を使う。
        head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
        current = _schema_revision(session)
        if manifest.schema_revision and manifest.schema_revision != current:
            raise MigrationError(
                f"書き出し元とスキーマの版が違います（移行ファイル {manifest.schema_revision}、"
                f"取り込み先 {current}、最新 {head}）。両方を同じ版の snsauto にそろえてから"
                "書き出し直してください。列の違いで項目が黙って欠けるのを防ぐため、取り込みません。")

        occupied = [t.name for t in Base.metadata.sorted_tables
                    if session.execute(select(func.count()).select_from(t)).scalar()]
        if occupied:
            raise MigrationError(
                "取り込み先のDBが空ではありません（" + "、".join(occupied[:5]) +
                ("など" if len(occupied) > 5 else "") + "）。IDが重なって別の顧客のデータに"
                "つながるのを防ぐため、空のDBにだけ取り込めます。")

        media_map = {(m["table"], m["id"], m["column"]): m["file"] for m in manifest.media}
        media_root = Path(settings.workspace).resolve() / "imported"
        counts, reconnect, reset, copied, missing = {}, 0, [], 0, 0

        for table in Base.metadata.sorted_tables:
            source = folder / "data" / f"{table.name}.json"
            rows = json.loads(source.read_text(encoding="utf-8")) if source.exists() else []
            prepared = []
            for raw in rows:
                unknown = set(raw) - set(table.c.keys())
                if unknown:
                    raise MigrationError(
                        f"{table.name} に取り込み先に無い項目があります: {sorted(unknown)}")
                row = {name: _from_json_value(value, table.c[name]) for name, value in raw.items()}
                for (owner, column), fill in IMPORT_FILL.items():
                    if owner == table.name and row.get(column) is None:
                        row[column] = fill
                if table.name == "social_accounts":
                    row["is_active"] = False
                    row["refresh_error"] = "移行で取り込んだため、再連携が必要です"
                    reconnect += 1
                if table.name == "users":
                    reset.append(row.get("email"))
                for column in FILE_COLUMNS.get(table.name, ()):
                    original = row.get(column)
                    bundled = media_map.get((table.name, raw.get("id"), column))
                    if bundled:
                        target = media_root / bundled.removeprefix("media/")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(folder / bundled, target)
                        row[column] = str(target)
                        copied += 1
                    elif original and not Path(original).exists():
                        missing += 1
                prepared.append(row)
            if prepared:
                session.execute(table.insert(), prepared)
            counts[table.name] = len(prepared)

        _reset_sequences(session)
        session.commit()
        return ImportResult(counts, reconnect, reset, copied, missing)
    except Exception:
        session.rollback()
        raise
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)


def _reset_sequences(session) -> None:
    """ID を指定して入れた後、連番を最大値の次に合わせる（PostgreSQL のみ必要）。

    これをしないと、取り込み後に最初に作る行が「IDが重複」で失敗する。
    SQLite と MySQL は既存の最大値から自動で続くので不要。
    """
    if session.bind.dialect.name != "postgresql":
        return
    for table in Base.metadata.sorted_tables:
        if "id" not in table.c:
            continue
        session.execute(text(
            f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table.name}), 0) + 1, false)"
        ))
