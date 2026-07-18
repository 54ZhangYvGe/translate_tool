"""
    Translate process:
    `translate.py` 的作用就是：__接收 AHK 传来的选中文本文件路径 → 
                                读取文本 → 
                                按配置调用翻译接口 → 
                                把原文和译文落盘保存 → 
                                再把结果写到临时结果文件，供 AHK 弹窗显示__。

"""

import os
import json
import uuid
import time
import hashlib
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = BASE_DIR / ".env"
PERF_LOG_PATH = BASE_DIR / "data" / "perf.log"


def _require_non_empty_string(config, key):
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"配置项 {key} 必须是非空字符串")
    return value.strip()


def append_perf_log(stage, detail):
    PERF_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(PERF_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {stage} | {detail}\n")


def load_config():
    default_config = {
        "save_dir": str(BASE_DIR / "data"),
        "provider": "youdao",
        "target_language": "zh-CHS",
        "source_language": "auto",
        "youdao_api_url": "https://openapi.youdao.com/api",
        "hotkey": "Alt+T",
        "keep_processed_requests": False,
    }

    if not CONFIG_PATH.exists():
        return default_config

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        try:
            user_config = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"配置文件格式错误：{CONFIG_PATH}（第 {exc.lineno} 行，第 {exc.colno} 列）"
            ) from exc

    if not isinstance(user_config, dict):
        raise ValueError(f"配置文件根节点必须是 JSON 对象：{CONFIG_PATH}")

    default_config.update(user_config)
    provider = _require_non_empty_string(default_config, "provider").lower()
    if provider not in {"mock", "youdao"}:
        raise ValueError(f"不支持的 provider：{provider}")
    default_config["provider"] = provider
    save_dir = Path(_require_non_empty_string(default_config, "save_dir")).expanduser()
    if not save_dir.is_absolute():
        save_dir = BASE_DIR / save_dir
    default_config["save_dir"] = str(save_dir)
    default_config["source_language"] = _require_non_empty_string(
        default_config, "source_language"
    )
    default_config["target_language"] = _require_non_empty_string(
        default_config, "target_language"
    )
    default_config["youdao_api_url"] = _require_non_empty_string(
        default_config, "youdao_api_url"
    )
    if not isinstance(default_config.get("keep_processed_requests"), bool):
        raise ValueError("配置项 keep_processed_requests 必须是 true 或 false")
    return default_config


def read_input_text(input_file):
    with open(input_file, "r", encoding="utf-8") as f:
        return f.read().strip()


def truncate_for_youdao_sign(text):
    """
    有道 v3 签名规则：
    如果 q 长度 <= 20，input = q
    如果 q 长度 > 20，input = q前10个字符 + q长度 + q后10个字符
    """
    if text is None:
        return ""

    size = len(text)

    if size <= 20:
        return text

    return text[:10] + str(size) + text[-10:]


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def translate_mock(text):
    """
    测试模式，不请求 API。
    """
    return f"【测试翻译结果】\n{text}"


def translate_by_youdao(text, config):
    """
    有道智云文本翻译 API。
    文档常见参数：
    q: 待翻译文本
    from: 源语言
    to: 目标语言
    appKey: 应用 ID
    salt: UUID
    sign: 签名
    signType: v3
    curtime: 当前秒级时间戳
    """

    load_dotenv(ENV_PATH)

    app_key = os.getenv("YOUDAO_APP_KEY", "").strip()
    app_secret = os.getenv("YOUDAO_APP_SECRET", "").strip()

    if not app_key:
        raise RuntimeError(f"没有配置 YOUDAO_APP_KEY，请检查 {ENV_PATH}")

    if not app_secret:
        raise RuntimeError(f"没有配置 YOUDAO_APP_SECRET，请检查 {ENV_PATH}")

    url = _require_non_empty_string(config, "youdao_api_url")

    source_language = config.get("source_language", "auto")
    target_language = config.get("target_language", "zh-CHS")

    salt = str(uuid.uuid4())
    curtime = str(int(time.time()))

    input_text = truncate_for_youdao_sign(text)

    sign_str = app_key + input_text + salt + curtime + app_secret
    sign = sha256_text(sign_str)

    data = {
        "q": text,
        "from": source_language,
        "to": target_language,
        "appKey": app_key,
        "salt": salt,
        "sign": sign,
        "signType": "v3",
        "curtime": curtime
    }

    try:
        resp = requests.post(url, data=data, timeout=(5, 30))
    except requests.Timeout as exc:
        raise RuntimeError("翻译服务请求超时，请稍后重试") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"无法连接翻译服务：{exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"有道 API 请求失败。\n"
            f"HTTP {resp.status_code}\n"
            "请稍后重试，或检查翻译服务配置。"
        )

    try:
        result = resp.json()
    except requests.JSONDecodeError as exc:
        raise RuntimeError("翻译服务返回了无法解析的数据") from exc

    error_code = str(result.get("errorCode", ""))

    if error_code != "0":
        error_message = result.get("msg") or result.get("message") or "请检查账号额度和语言配置"
        raise RuntimeError(f"有道 API 返回错误：{error_code}\n{error_message}")

    # 有道主翻译结果，一般在 translation 字段
    translation_list = result.get("translation", [])

    if translation_list:
        return "\n".join(translation_list).strip()

    # 兜底：有些结果可能在 basic.explains
    basic = result.get("basic", {})
    explains = basic.get("explains", [])

    if explains:
        return "\n".join(explains).strip()

    raise RuntimeError(
        "有道 API 没有返回 translation 字段：\n"
        + json.dumps(result, ensure_ascii=False, indent=2)
    )


def translate_text(text, config):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("待翻译文本不能为空")

    provider = _require_non_empty_string(config, "provider").lower()

    if provider == "mock":
        return translate_mock(text)

    if provider == "youdao":
        return translate_by_youdao(text, config)

    raise RuntimeError(f"不支持的 provider：{provider}")


def save_record(source, translation, config):
    save_dir = Path(config.get("save_dir") or BASE_DIR / "data").expanduser()
    save_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now().strftime("%Y-%m-%d")
    time_str = datetime.now().strftime("%H:%M:%S")

    file_path = save_dir / f"{date_str}.txt"

    with open(file_path, "a", encoding="utf-8") as f:
        f.write("=" * 70 + "\n")
        f.write(f"时间：{time_str}\n\n")
        f.write("原文：\n")
        f.write(source.strip())
        f.write("\n\n")
        f.write("译文：\n")
        f.write(translation.strip())
        f.write("\n\n")

    return file_path



















