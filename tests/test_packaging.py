"""インストールの定義そのもの。

開発環境で動いていても、手順どおり新しく入れた環境で動くとは限らない。
python-multipart は web の依存関係に書かれておらず、別の経路で入っていた
開発環境では誰も気づかなかった。新しく入れると、ログインを含むすべての
フォーム送信が 500 になる。
"""

import sys
import tomllib
from pathlib import Path

PYPROJECT = tomllib.loads(
    (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
)


def _names(requirements):
    import re

    return {re.split(r"[<>=\[ ;]", r, maxsplit=1)[0].lower() for r in requirements}


def test_the_web_extra_can_receive_forms():
    assert "python-multipart" in _names(PYPROJECT["project"]["optional-dependencies"]["web"])


def test_the_docker_image_installs_the_web_extra():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "web" in dockerfile.split('pip install --no-cache-dir ".[')[1].split("]")[0]


def test_every_third_party_import_in_the_web_app_is_declared():
    """web/app.py が使う外部ライブラリは、本体か web の依存関係に書かれている。"""
    import ast

    declared = _names(PYPROJECT["project"]["dependencies"]) | _names(
        PYPROJECT["project"]["optional-dependencies"]["web"]
    )
    # import 名と配布名が違うもの
    aliases = {"jinja2": "jinja2", "starlette": "fastapi", "multipart": "python-multipart"}
    tree = ast.parse((Path(__file__).resolve().parents[1] / "src/snsauto/web/app.py")
                     .read_text(encoding="utf-8"))
    stdlib = set(sys.stdlib_module_names)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split(".")[0]
        elif isinstance(node, ast.Import):
            top = node.names[0].name.split(".")[0]
        else:
            continue
        if top in stdlib or top == "snsauto":
            continue
        assert aliases.get(top, top).lower() in declared, f"{top} が依存関係に無い"
