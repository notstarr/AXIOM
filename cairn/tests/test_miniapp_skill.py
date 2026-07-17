from __future__ import annotations

import json
import subprocess
import sys
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src/axiom/dispatcher/skills/miniapp/scripts/miniapp_decompile_analyze.py"
)


def run_script(*args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(result.stdout)


def make_decompiled(root: Path) -> Path:
    app = root / "wx1234567890abcdef"
    (app / "pages/index").mkdir(parents=True)
    (app / "assets").mkdir()
    (app / "project.config.json").write_text(
        json.dumps({"appid": "wx1234567890abcdef", "projectname": "demo-miniapp", "version": "1.0.0"}),
        encoding="utf-8",
    )
    (app / "app.json").write_text(
        json.dumps({"pages": ["pages/index/index"], "window": {"navigationBarTitleText": "Demo"}}),
        encoding="utf-8",
    )
    (app / "app.js").write_text(
        """
        const base = 'https://api.example.com/client/app?code=UserCenter/User/index';
        const token = 'abcdef1234567890';
        const phone = '13800138000';
        const jdbc = 'jdbc:mysql://127.0.0.1:3306/test';
        wx.setStorageSync('X-TOKEN', token);
        wx.request({ url: base, method: 'POST' });
        """,
        encoding="utf-8",
    )
    (app / "pages/index/index.js").write_text("const logo = '/assets/logo.png';", encoding="utf-8")
    (app / "assets/logo.png").write_bytes(b"fakepng")
    return app


def write_wxapkg(path: Path, files: dict[str, bytes]) -> None:
    entries = []
    index_len = 0
    body_size = 0
    for name, content in files.items():
        raw_name = name.encode("utf-8")
        entries.append((raw_name, body_size, len(content), content))
        index_len += 4 + len(raw_name) + 4 + 4
        body_size += len(content)
    absolute_body_offset = 18 + index_len
    data = bytearray()
    data.extend(struct.pack(">BIIIB", 0xBE, 0, index_len, body_size, 0xED))
    data.extend(struct.pack(">I", len(entries)))
    for raw_name, offset, size, _content in entries:
        data.extend(struct.pack(">I", len(raw_name)))
        data.extend(raw_name)
        data.extend(struct.pack(">II", absolute_body_offset + offset, size))
    for _raw_name, _offset, _size, content in entries:
        data.extend(content)
    path.write_bytes(bytes(data))


def encrypt_wxapkg_data(data: bytes, wxid: str) -> bytes:
    key = __import__("hashlib").pbkdf2_hmac("sha1", wxid.encode(), b"saltiest", 1000, 32)
    cipher = Cipher(algorithms.AES(key), modes.CBC(b"the iv: 16 bytes"))
    encryptor = cipher.encryptor()
    first_plain = data[:1023].ljust(1023, b"\x00") + b"\x00"
    first_encrypted = encryptor.update(first_plain) + encryptor.finalize()
    xor_key = wxid.encode()[-2]
    tail = bytes(byte ^ xor_key for byte in data[1023:])
    return b"V1MMWX" + first_encrypted + tail


def test_all_analyzes_decompiled_dir(tmp_path: Path) -> None:
    source = make_decompiled(tmp_path)
    out = tmp_path / "out"

    result = run_script("all", "--input", str(source), "--out", str(out))

    assert result["api_candidates"] >= 3
    assert result["sensitive_findings"] >= 3
    assert result["media_resources"] == 1
    assert (out / "decompiled" / "app.json").exists()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["appid"] == "wx1234567890abcdef"
    assert manifest["engine"] == "source-copy"
    assert "api.example.com" in (out / "yak_workflow.md").read_text(encoding="utf-8")


def test_list_distinguishes_package_and_decompiled_dir(tmp_path: Path) -> None:
    source = make_decompiled(tmp_path)
    pkg = tmp_path / "wxabcdefabcdefabcd.wxapkg"
    pkg.write_bytes(b"https://pkg.example.com/client/app?code=Cart/List/index")

    result = run_script("list", "--root", str(tmp_path), "--max-depth", "4")

    kinds = {item["kind"] for item in result["miniapps"]}
    paths = {item["source_path"] for item in result["miniapps"]}
    assert "decompiled" in kinds
    assert "package" in kinds
    assert str(source.resolve()) in paths
    assert str(pkg.resolve()) in paths


def test_reuses_existing_output_when_hash_matches(tmp_path: Path) -> None:
    source = make_decompiled(tmp_path)
    out = tmp_path / "out"

    first = run_script("all", "--input", str(source), "--out", str(out))
    second = run_script("all", "--input", str(source), "--out", str(out))

    assert first["output_dir"] == second["output_dir"]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["reused"] is True


def test_wxapkg_fallback_extracts_strings(tmp_path: Path) -> None:
    pkg = tmp_path / "wx1111111111111111.wxapkg"
    pkg.write_bytes(b"binary\x00https://pkg.example.com\x00UserCenter/User/index\x00token='abcdef1234567890'")
    out = tmp_path / "out"

    result = run_script("all", "--input", str(pkg), "--out", str(out))

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["engine"] == "strings-fallback"
    assert (out / "decompiled" / "__strings__" / "wx1111111111111111.strings.txt").exists()
    assert result["api_candidates"] >= 2


def test_wxapkg_unpack_restores_files(tmp_path: Path) -> None:
    pkg = tmp_path / "wx2222222222222222.wxapkg"
    write_wxapkg(
        pkg,
        {
            "/app.json": json.dumps({"pages": ["pages/index/index"]}).encode(),
            "/app.js": b"wx.request({url: 'https://api.unpack.example/client/app?code=Goods/List/index'})",
            "/pages/index/index.wxml": b"<view>hello</view>",
        },
    )
    out = tmp_path / "out"

    result = run_script("all", "--input", str(pkg), "--out", str(out))

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["engine"] == "wxapkg-unpack"
    assert (out / "decompiled" / "app.json").exists()
    assert (out / "decompiled" / "pages/index/index.wxml").exists()
    assert result["api_candidates"] >= 3
    api_items = json.loads((out / "api_candidates.json").read_text(encoding="utf-8"))
    assert any(item["value"] == "Goods/List/index" and item["kind"] in {"api", "api_code"} for item in api_items)
    assert not any(item["value"] == "pages/index/index" and item["kind"] in {"api", "api_code"} for item in api_items)


def test_encrypted_wxapkg_unpack_uses_appid_from_filename(tmp_path: Path) -> None:
    wxid = "wx3333333333333333"
    plain = tmp_path / "plain.wxapkg"
    encrypted = tmp_path / f"{wxid}.wxapkg"
    write_wxapkg(
        plain,
        {
            "/app.json": json.dumps({"pages": ["pages/home/home"]}).encode(),
            "/app.js": b"const api='https://encrypted.example.com/client/app?code=Order/List/index'",
        },
    )
    encrypted.write_bytes(encrypt_wxapkg_data(plain.read_bytes(), wxid))
    out = tmp_path / "out"

    result = run_script("all", "--input", str(encrypted), "--out", str(out))

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["engine"] == "wxapkg-unpack"
    assert manifest["appid"] == wxid
    assert (out / "decompiled" / "app.json").exists()
    assert "encrypted.example.com" in (out / "summary.md").read_text(encoding="utf-8")
    assert result["api_candidates"] >= 2


def test_external_decompiler_option_is_not_exposed() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "all", "--help"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert "--decompiler-cmd" not in result.stdout


def test_identify_groups_package_dir_and_infers_brand_title(tmp_path: Path) -> None:
    appid = "wx5555555555555555"
    version_dir = tmp_path / appid / "1"
    version_dir.mkdir(parents=True)
    write_wxapkg(
        version_dir / "__APP__.wxapkg",
        {
            "/app-config.json": json.dumps(
                {
                    "page": {
                        "pages/user/index.html": {"window": {"navigationBarTitleText": "DEMO"}},
                        "pages/home/index.html": {"window": {"navigationBarTitleText": "首页"}},
                    },
                    "tabBar": {"list": [{"text": "首页"}, {"text": "我的"}]},
                }
            ).encode(),
            "/app-service.js": b"const host='https://api.demo.example.com/client/app?code=UserCenter/User/index';",
        },
    )
    write_wxapkg(
        version_dir / "_pages_user_.wxapkg",
        {"/pages/user/app-service.js": b"request('Order/Create/doCreate')"},
    )

    result = run_script("identify", "--root", str(version_dir), "--only-unpacked")

    assert result["count"] == 1
    item = result["miniapps"][0]
    assert item["kind"] == "package_dir"
    assert item["display_name"] == "DEMO"
    assert "api.demo.example.com" in item["hosts"]
    assert "UserCenter/User/index" in item["business_codes"]
    assert item["developer"] == ""


def test_status_returns_ready_target_for_existing_analyzed_output(tmp_path: Path) -> None:
    source = make_decompiled(tmp_path / "source")
    out_root = tmp_path / "state"
    target_out = out_root / "demo-ready"
    run_script("all", "--input", str(source), "--out", str(target_out))

    result = run_script("status", "--root", str(out_root), "--out-dir", str(out_root), "--max-depth", "4")

    assert result["next_action"] == "use_ready_target"
    ready = result["ready_targets"]
    assert len(ready) == 1
    assert ready[0]["target_id"] == "demo-ready"
    assert ready[0]["is_analyzed"] is True
    assert ready[0]["summary"] == str(target_out / "summary.md")
    assert ready[0]["yak_workflow"] == str(target_out / "yak_workflow.md")
    assert (out_root / "status.json").exists()
    assert (out_root / "status.md").exists()


def test_probe_groups_multi_package_dir_into_one_target(tmp_path: Path) -> None:
    appid = "wx5555555555555555"
    version_dir = tmp_path / appid / "1"
    version_dir.mkdir(parents=True)
    write_wxapkg(
        version_dir / "__APP__.wxapkg",
        {
            "/app-config.json": json.dumps(
                {
                    "page": {
                        "pages/user/index.html": {"window": {"navigationBarTitleText": "DEMO"}},
                        "pages/home/index.html": {"window": {"navigationBarTitleText": "首页"}},
                    }
                }
            ).encode(),
            "/app-service.js": b"const host='https://api.demo.example.com/client/app?code=UserCenter/User/index';",
        },
    )
    write_wxapkg(
        version_dir / "_pages_user_.wxapkg",
        {"/pages/user/app-service.js": b"request('Order/Create/doCreate')"},
    )
    out_root = tmp_path / "state"
    run_script("status", "--root", str(version_dir), "--out-dir", str(out_root))

    result = run_script("probe", "--out-dir", str(out_root))

    assert result["count"] == 1
    target = result["targets"][0]
    assert target["target_id"].startswith("pkg-wx5555555555555555")
    assert target["display_name"] == "DEMO"
    assert target["appid"] == appid
    assert target["package_count"] == 2
    assert target["version_dir"] == str(version_dir.resolve())
    assert "api.demo.example.com" in target["hosts"]
    assert "UserCenter/User/index" in target["business_codes"]
    assert target["developer"] == ""
    assert target["description"] == ""
    assert (out_root / "probe.json").exists()
    assert (out_root / "probe.md").exists()


def test_prepare_uses_target_id_from_state_without_paths(tmp_path: Path) -> None:
    appid = "wx6666666666666666"
    version_dir = tmp_path / appid / "2"
    version_dir.mkdir(parents=True)
    write_wxapkg(
        version_dir / "__APP__.wxapkg",
        {
            "/app-config.json": json.dumps(
                {"page": {"pages/index/index.html": {"window": {"navigationBarTitleText": "PREP"}}}}
            ).encode(),
            "/app-service.js": b"wx.request({url:'https://prep.example.com/client/app?code=Cart/Add/doAdd'})",
        },
    )
    out_root = tmp_path / "state"
    status = run_script("status", "--root", str(version_dir), "--out-dir", str(out_root))
    target_id = status["package_targets"][0]["target_id"]

    result = run_script("prepare", "--target-id", target_id, "--out-dir", str(out_root))

    target_out = out_root / target_id
    assert result["target_id"] == target_id
    assert result["output_dir"] == str(target_out)
    assert result["decompiled_path"] == str(target_out / "decompiled")
    assert result["api_candidates"] >= 2
    assert result["files"]["api_candidates"] == str(target_out / "api_candidates.json")
    assert (target_out / "summary.md").exists()
    assert (target_out / "yak_workflow.md").exists()
    api_items = json.loads((target_out / "api_candidates.json").read_text(encoding="utf-8"))
    assert any(item["value"] == "Cart/Add/doAdd" for item in api_items)
