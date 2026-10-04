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
import re
import tempfile
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

import requests
from dotenv import dotenv_values

from secret_store import CredentialError, clear_secret, has_secret, read_secret, save_secret


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
# Installed files can be removed without removing user configuration/history.
BASE_DIR = (Path(os.environ["LOCALAPPDATA"]) / "TranEasy"
            if (APP_DIR / "installed.mode").is_file() else APP_DIR)
CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = BASE_DIR / ".env"
ENV_LOCAL_PATH = BASE_DIR / ".env.local"
PERF_LOG_PATH = BASE_DIR / "data" / "perf.log"
TTS_MAX_WORDS = 50
TTS_MAX_UTF8_BYTES = 2048
TTS_WORD_PATTERN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff]|[^\W_]+(?:['’\-][^\W_]+)*",
    re.UNICODE,
)


def _require_non_empty_string(config, key):
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"配置项 {key} 必须是非空字符串")
    return value.strip()


def load_environment():
    # Only non-sensitive TTS App ID uses dotenv. Secrets never enter os.environ.
    for path in (ENV_PATH, ENV_LOCAL_PATH):
        if path.exists():
            for key, value in dotenv_values(path).items():
                if key == "YOUDAO_TTS_APP_KEY" and value:
                    os.environ[key] = value


def _write_config_fields(updates, drop_legacy=False):
    current = {}
    if CONFIG_PATH.exists():
        current = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            raise ValueError("配置文件格式错误")
    current.update(updates)
    if drop_legacy:
        for key in ("YOUDAO_APP_KEY", "YOUDAO_APP_SECRET", "youdao_app_secret"):
            current.pop(key, None)
    temporary = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(current, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, CONFIG_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_legacy_fields(path, keys):
    if not path.exists():
        return
    original = path.read_text(encoding="utf-8-sig")
    kept = [line for line in original.splitlines(keepends=True)
            if not any(re.match(rf"^\s*(?:export\s+)?{key}\s*=", line) for key in keys)]
    if len(kept) == len(original.splitlines(keepends=True)):
        return
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            stream.writelines(kept)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def migrate_legacy_credentials():
    """Copy old plaintext credentials, verify DPAPI, then remove only migrated lines."""
    legacy = [(path, dotenv_values(path)) for path in (ENV_LOCAL_PATH, ENV_PATH) if path.exists()]
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    if not isinstance(config, dict):
        raise ValueError("配置文件格式错误")
    legacy_key = next((values.get("YOUDAO_APP_KEY") for _, values in legacy
                       if values.get("YOUDAO_APP_KEY")), None) or config.get("YOUDAO_APP_KEY")
    legacy_secret = next((values.get("YOUDAO_APP_SECRET") for _, values in legacy
                          if values.get("YOUDAO_APP_SECRET")), None) or config.get("youdao_app_secret") or config.get("YOUDAO_APP_SECRET")
    if legacy_secret is not None and not isinstance(legacy_secret, str):
        raise CredentialError("旧密钥格式无效，请重新输入")
    if legacy_secret and not has_secret():
        save_secret(legacy_secret)
    if legacy_secret and not read_secret():
        raise CredentialError("旧密钥迁移验证失败，请检查设置")
    changed = False
    if legacy_key and not config.get("youdao_app_key"):
        config["youdao_app_key"] = legacy_key
        changed = True
    for key in ("YOUDAO_APP_KEY", "YOUDAO_APP_SECRET", "youdao_app_secret"):
        if key in config:
            del config[key]
            changed = True
    if changed:
        _write_config_fields(config, drop_legacy=True)
    for path, values in legacy:
        keys = [key for key in ("YOUDAO_APP_KEY", "YOUDAO_APP_SECRET") if key in values]
        if keys:
            _remove_legacy_fields(path, keys)
    legacy_tts_key = next((values.get("YOUDAO_TTS_APP_KEY") for _, values in legacy
                           if values.get("YOUDAO_TTS_APP_KEY")), None)
    legacy_tts_secret = next((values.get("YOUDAO_TTS_APP_SECRET") for _, values in legacy
                              if values.get("YOUDAO_TTS_APP_SECRET")), None)
    if legacy_tts_secret:
        # An explicitly supplied legacy TTS field is an import/replacement;
        # remove it only after the separate DPAPI file has been verified.
        if not has_secret("tts"):
            save_secret(legacy_tts_secret, "tts")
        if read_secret("tts") != legacy_tts_secret:
            raise CredentialError("旧朗读密钥迁移验证失败")
        for path, values in legacy:
            if "YOUDAO_TTS_APP_SECRET" in values:
                _remove_legacy_fields(path, ("YOUDAO_TTS_APP_SECRET",))
    if legacy_tts_key and not config.get("youdao_tts_app_key"):
        _write_config_fields({"youdao_tts_app_key": legacy_tts_key})


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
        "youdao_tts_api_url": "https://openapi.youdao.com/ttsapi",
        "youdao_tts_voice_name": "youxiaoqin",
        "hotkey": "Alt+T",
        "screenshot_enabled": True,
        "screenshot_auto_translate": True,
        "screenshot_hotkey": "Ctrl+Alt+T",
        "keep_processed_requests": False,
        "auto_read_aloud": False,
        "auto_read_max_chars": TTS_MAX_WORDS,
        "manual_input_hotkey": "Ctrl+I",
        "result_font_size": 14,
        "result_card_width": 378,
        "result_card_height": 360,
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
    default_config["youdao_tts_api_url"] = _require_non_empty_string(
        default_config, "youdao_tts_api_url"
    )
    default_config["youdao_tts_voice_name"] = _require_non_empty_string(
        default_config, "youdao_tts_voice_name"
    )
    if not isinstance(default_config.get("keep_processed_requests"), bool):
        raise ValueError("配置项 keep_processed_requests 必须是 true 或 false")
    if not isinstance(default_config.get("auto_read_aloud"), bool):
        raise ValueError("配置项 auto_read_aloud 必须是 true 或 false")
    if not isinstance(default_config.get("screenshot_enabled"), bool):
        raise ValueError("配置项 screenshot_enabled 必须是 true 或 false")
    if not isinstance(default_config.get("screenshot_auto_translate"), bool):
        raise ValueError("配置项 screenshot_auto_translate 必须是 true 或 false")
    default_config["screenshot_hotkey"] = _require_non_empty_string(
        default_config, "screenshot_hotkey"
    )
    auto_read_max_chars = default_config.get("auto_read_max_chars")
    if (
        isinstance(auto_read_max_chars, bool)
        or not isinstance(auto_read_max_chars, int)
        or not 1 <= auto_read_max_chars <= TTS_MAX_WORDS
    ):
        raise ValueError(
            f"配置项 auto_read_max_chars 必须是 1 到 {TTS_MAX_WORDS} 之间的整数"
        )
    default_config["manual_input_hotkey"] = _require_non_empty_string(
        default_config, "manual_input_hotkey"
    )
    for key, minimum, maximum in (
        ("result_font_size", 10, 24),
        ("result_card_width", 320, 760),
        ("result_card_height", 320, 640),
    ):
        value = default_config[key]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"配置项 {key} 必须是 {minimum} 到 {maximum} 之间的整数")
    return default_config


def save_read_aloud_settings(
    auto_read_aloud, auto_read_max_chars, manual_input_hotkey=None
):
    if not isinstance(auto_read_aloud, bool):
        raise ValueError("自动朗读开关必须是布尔值")
    if (
        isinstance(auto_read_max_chars, bool)
        or not isinstance(auto_read_max_chars, int)
        or not 1 <= auto_read_max_chars <= TTS_MAX_WORDS
    ):
        raise ValueError(f"自动朗读上限必须是 1 到 {TTS_MAX_WORDS} 之间的整数")
    if manual_input_hotkey is not None:
        if not isinstance(manual_input_hotkey, str) or not manual_input_hotkey.strip():
            raise ValueError("手动输入快捷键不能为空")
        manual_input_hotkey = manual_input_hotkey.strip()

    user_config = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_config = json.load(f)
        if not isinstance(user_config, dict):
            raise ValueError(f"配置文件根节点必须是 JSON 对象：{CONFIG_PATH}")

    user_config["auto_read_aloud"] = auto_read_aloud
    user_config["auto_read_max_chars"] = auto_read_max_chars
    if manual_input_hotkey is not None:
        user_config["manual_input_hotkey"] = manual_input_hotkey
    temp_path = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.{os.getpid()}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(user_config, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, CONFIG_PATH)
    finally:
        temp_path.unlink(missing_ok=True)

    return load_config()


def save_result_settings(
    auto_read_aloud,
    auto_read_max_chars,
    manual_input_hotkey,
    screenshot_enabled,
    screenshot_hotkey,
    app_key="",
    app_secret="",
    clear_app_secret=False,
    result_font_size=None,
    result_card_width=None,
    result_card_height=None,
    tts_app_key="",
    tts_app_secret="",
    clear_tts_secret=False,
    screenshot_auto_translate=True,
):
    """Save visible result-window settings without putting credentials in config.json."""
    if not isinstance(screenshot_enabled, bool):
        raise ValueError("截图翻译开关必须是布尔值")
    if not isinstance(screenshot_auto_translate, bool):
        raise ValueError("截图后自动翻译开关必须是布尔值")
    if not isinstance(auto_read_aloud, bool):
        raise ValueError("自动朗读开关必须是布尔值")
    if isinstance(auto_read_max_chars, bool) or not isinstance(auto_read_max_chars, int) or not 1 <= auto_read_max_chars <= TTS_MAX_WORDS:
        raise ValueError(f"自动朗读上限必须是 1 到 {TTS_MAX_WORDS} 之间的整数")
    if not isinstance(manual_input_hotkey, str) or not manual_input_hotkey.strip():
        raise ValueError("手动输入快捷键不能为空")
    if not isinstance(screenshot_hotkey, str) or not screenshot_hotkey.strip():
        raise ValueError("截图快捷键不能为空")
    if not isinstance(app_key, str) or not isinstance(app_secret, str):
        raise ValueError("API 凭据必须是文本")
    if clear_app_secret and app_secret.strip():
        raise ValueError("清除密钥时不能同时填写新密钥")
    if any("\n" in value or "\r" in value for value in (app_key, app_secret)):
        raise ValueError("API 凭据不能包含换行")
    if not isinstance(tts_app_key, str) or not isinstance(tts_app_secret, str):
        raise ValueError("TTS 凭据必须是文本")
    if clear_tts_secret and tts_app_secret.strip():
        raise ValueError("清除 TTS 密钥时不能同时填写新密钥")
    if any("\n" in value or "\r" in value for value in (tts_app_key, tts_app_secret)):
        raise ValueError("TTS 凭据不能包含换行")
    for key, value, minimum, maximum in (
        ("result_font_size", result_font_size, 10, 24),
        ("result_card_width", result_card_width, 320, 760),
        ("result_card_height", result_card_height, 320, 640),
    ):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum):
            raise ValueError(f"{key} 必须是 {minimum} 到 {maximum} 之间的整数")

    replacing_secret = bool(app_secret.strip()) or clear_app_secret
    # A user must be able to replace/clear even a DPAPI blob that no longer
    # decrypts. Normal saves perform the full verified legacy migration.
    if not replacing_secret and not (tts_app_secret.strip() or clear_tts_secret):
        migrate_legacy_credentials()
    user_config = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_config = json.load(f)
        if not isinstance(user_config, dict):
            raise ValueError(f"配置文件根节点必须是 JSON 对象：{CONFIG_PATH}")
    user_config["auto_read_aloud"] = auto_read_aloud
    user_config["auto_read_max_chars"] = auto_read_max_chars
    user_config["manual_input_hotkey"] = manual_input_hotkey.strip()
    user_config["screenshot_enabled"] = screenshot_enabled
    user_config["screenshot_auto_translate"] = screenshot_auto_translate
    user_config["screenshot_hotkey"] = screenshot_hotkey.strip()
    for key, value in (
        ("result_font_size", result_font_size),
        ("result_card_width", result_card_width),
        ("result_card_height", result_card_height),
    ):
        if value is not None:
            user_config[key] = value
    if app_key.strip():
        user_config["youdao_app_key"] = app_key.strip()
    elif replacing_secret and not user_config.get("youdao_app_key"):
        for path in (ENV_LOCAL_PATH, ENV_PATH):
            if path.exists():
                old_key = dotenv_values(path).get("YOUDAO_APP_KEY")
                if old_key:
                    user_config["youdao_app_key"] = old_key
                    break
    if tts_app_key.strip():
        user_config["youdao_tts_app_key"] = tts_app_key.strip()
    if replacing_secret:
        for key in ("YOUDAO_APP_KEY", "YOUDAO_APP_SECRET", "youdao_app_secret"):
            user_config.pop(key, None)
    if app_secret.strip():
        save_secret(app_secret.strip())
    elif clear_app_secret:
        for path in (ENV_LOCAL_PATH, ENV_PATH):
            _remove_legacy_fields(path, ("YOUDAO_APP_SECRET",))
        clear_secret()
    if app_secret.strip():
        for path in (ENV_LOCAL_PATH, ENV_PATH):
            _remove_legacy_fields(path, ("YOUDAO_APP_SECRET",))
    _write_config_fields(user_config, drop_legacy=replacing_secret)
    if tts_app_secret.strip():
        save_secret(tts_app_secret.strip(), "tts")
    elif clear_tts_secret:
        clear_secret("tts")
    if tts_app_secret.strip() or clear_tts_secret:
        for path in (ENV_LOCAL_PATH, ENV_PATH):
            _remove_legacy_fields(path, ("YOUDAO_TTS_APP_SECRET",))
    return load_config()


def count_tts_words(text):
    if not isinstance(text, str):
        return 0
    return sum(1 for _ in TTS_WORD_PATTERN.finditer(text))


def synthesize_speech_to_temp(text, config):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("待朗读文本不能为空")
    text = text.strip()
    word_count = count_tts_words(text)
    if word_count > TTS_MAX_WORDS:
        raise ValueError(
            f"待朗读文本共 {word_count} 个词，超过语音合成上限 {TTS_MAX_WORDS} 个词"
        )
    utf8_size = len(text.encode("utf-8"))
    if utf8_size > TTS_MAX_UTF8_BYTES:
        raise ValueError(
            f"待朗读文本的 UTF-8 编码长度为 {utf8_size} 字节，"
            f"超过接口上限 {TTS_MAX_UTF8_BYTES} 字节"
        )

    migrate_legacy_credentials()
    load_environment()
    app_key = config.get("youdao_tts_app_key", "") or load_config().get("youdao_tts_app_key", "")
    app_key = app_key.strip()
    app_secret = read_secret("tts")
    if not app_key:
        raise CredentialError("没有配置语音合成 App ID，请在设置中填写")
    if not app_secret:
        raise CredentialError("没有配置语音合成密钥，请在设置中填写")

    salt = str(uuid.uuid4())
    curtime = str(int(time.time()))
    sign_input = truncate_for_youdao_sign(text)
    sign = sha256_text(app_key + sign_input + salt + curtime + app_secret)
    del app_secret
    data = {
        "q": text,
        "appKey": app_key,
        "salt": salt,
        "sign": sign,
        "signType": "v3",
        "curtime": curtime,
        "format": "mp3",
        "speed": "1.0",
        "volume": "1.0",
        "voiceName": _require_non_empty_string(config, "youdao_tts_voice_name"),
    }
    try:
        response = requests.post(
            _require_non_empty_string(config, "youdao_tts_api_url"),
            data=data,
            timeout=(5, 30),
        )
    except requests.Timeout as exc:
        raise RuntimeError("语音合成请求超时，请稍后重试") from exc
    except requests.RequestException as exc:
        raise RuntimeError("无法连接有道语音服务，请检查网络") from exc

    if response.status_code != 200:
        raise RuntimeError(f"有道语音服务返回 HTTP {response.status_code}")

    audio_data = response.content
    content_type = response.headers.get("Content-Type", "").lower()
    if not audio_data:
        raise RuntimeError("有道语音服务返回了空音频")
    if len(audio_data) > 20 * 1024 * 1024:
        raise RuntimeError("语音文件超过 20 MB，已停止下载")
    if "json" in content_type or audio_data.lstrip().startswith(b"{"):
        try:
            error_payload = response.json()
        except requests.JSONDecodeError:
            error_payload = {}
        if not isinstance(error_payload, dict):
            raise RuntimeError("有道语音服务返回格式异常")
        error_code = str(error_payload.get("errorCode") or error_payload.get("error") or "")
        if re.fullmatch(r"\d{1,8}", error_code):
            raise RuntimeError(f"有道语音服务返回错误：{error_code}")
        raise RuntimeError("有道语音合成服务没有返回音频")

    if "wav" in content_type or audio_data.startswith(b"RIFF"):
        suffix = ".wav"
    elif "amr" in content_type or audio_data.startswith(b"#!AMR"):
        suffix = ".amr"
    elif "ogg" in content_type or audio_data.startswith(b"OggS"):
        suffix = ".ogg"
    else:
        suffix = ".mp3"
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=suffix, prefix="translate_tool_", delete=False
    ) as audio_file:
        audio_file.write(audio_data)
        return Path(audio_file.name)


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


class TranslationServiceError(RuntimeError):
    def __init__(self, message, diagnostic):
        super().__init__(message)
        self.diagnostic = diagnostic


def _youdao_error(code):
    if code in {"108", "111", "202"}:
        return "有道应用 ID 或密钥无效，请检查设置"
    if code == "110":
        return "有道应用未开通翻译服务，请检查服务绑定"
    if code == "401":
        return "有道账户额度不足，请检查账户"
    if code in {"411", "412"}:
        return "翻译请求过于频繁，请稍后重试"
    if code in {"102", "103"}:
        return "翻译语言不支持或文本过长"
    return f"有道翻译暂不可用（错误码 {code}）"


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

    try:
        migrate_legacy_credentials()
        app_secret = read_secret()
    except CredentialError as exc:
        raise TranslationServiceError(str(exc), "dpapi_unavailable") from None
    app_key = str(config.get("youdao_app_key") or "").strip()
    if not app_key and CONFIG_PATH.exists():
        # The first migration may have just added the App ID to config.json.
        app_key = str(load_config().get("youdao_app_key") or "").strip()

    if not app_key:
        raise TranslationServiceError("请先在设置中填写有道应用 ID", "missing_app_id")

    if not app_secret:
        raise TranslationServiceError("请先在设置中填写有道应用密钥", "missing_secret")

    url = _require_non_empty_string(config, "youdao_api_url")

    source_language = config.get("source_language", "auto")
    target_language = config.get("target_language", "zh-CHS")

    salt = str(uuid.uuid4())
    curtime = str(int(time.time()))

    input_text = truncate_for_youdao_sign(text)

    sign = sha256_text(app_key + input_text + salt + curtime + app_secret)
    del app_secret

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
        resp = requests.post(url, data=data, timeout=(3, 10))
    except requests.Timeout as exc:
        raise TranslationServiceError("翻译请求超时，请稍后重试", "network_timeout") from None
    except requests.ConnectionError:
        raise TranslationServiceError("无法连接翻译服务，请检查网络", "connection_failed") from None
    except requests.RequestException as exc:
        raise TranslationServiceError("翻译请求失败，请稍后重试", type(exc).__name__) from None

    if resp.status_code != 200:
        if resp.status_code in (401, 403):
            message = "有道身份验证失败，请检查应用 ID 和密钥"
        elif resp.status_code == 429:
            message = "翻译请求过于频繁，请稍后重试"
        elif resp.status_code in (408, 504):
            message = "翻译请求超时，请稍后重试"
        else:
            message = "翻译服务暂不可用，请稍后重试"
        raise TranslationServiceError(message, f"http_{resp.status_code}")

    try:
        result = resp.json()
    except (ValueError, requests.JSONDecodeError):
        raise TranslationServiceError("翻译服务返回数据异常，请稍后重试", "invalid_json") from None

    if not isinstance(result, dict):
        raise TranslationServiceError("翻译服务返回数据异常，请稍后重试", "invalid_shape")

    raw_code = result.get("errorCode")
    error_code = str(raw_code) if isinstance(raw_code, (str, int)) else ""
    if not re.fullmatch(r"\d{1,8}", error_code):
        raise TranslationServiceError("翻译服务返回数据异常，请稍后重试", "invalid_error_code")

    if error_code != "0":
        raise TranslationServiceError(_youdao_error(error_code), f"youdao_{error_code}")

    # 有道主翻译结果，一般在 translation 字段
    translation_list = result.get("translation", [])

    if isinstance(translation_list, list) and translation_list and all(isinstance(item, str) for item in translation_list):
        translation = "\n".join(translation_list).strip()

    # 兜底：有些结果可能在 basic.explains
    else:
        basic = result.get("basic")
        explains = basic.get("explains") if isinstance(basic, dict) else None
        if not isinstance(explains, list) or not explains or not all(isinstance(item, str) for item in explains):
            raise TranslationServiceError("翻译服务返回数据不完整，请稍后重试", "missing_translation")
        translation = "\n".join(explains).strip()

    if not translation:
        raise TranslationServiceError("翻译服务返回了空结果，请稍后重试", "empty_translation")

    return translation


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


def load_recent_records(config, limit=30):
    """Read only the latest history entries when the history panel is opened."""
    save_dir = Path(config.get("save_dir") or BASE_DIR / "data").expanduser()
    if not save_dir.is_dir():
        return []
    records = []
    for file_path in sorted(save_dir.glob("????-??-??.txt"), reverse=True):
        recent = deque(maxlen=limit)
        current = []
        try:
            with open(file_path, "r", encoding="utf-8") as stream:
                for line in stream:
                    if line.rstrip("\r\n") == "=" * 70:
                        record = _parse_record_block("".join(current), file_path)
                        if record:
                            recent.append(record)
                        current = []
                    else:
                        current.append(line)
                record = _parse_record_block("".join(current), file_path)
                if record:
                    recent.append(record)
        except OSError:
            continue
        records.extend(reversed(recent))
        if len(records) >= limit:
            break
    return records[:limit]


def _parse_record_block(block, file_path):
    if "原文：\n" not in block or "译文：\n" not in block:
        return None
    header, _, content = block.partition("原文：\n")
    source, _, translation = content.partition("\n\n译文：\n")
    if not translation.strip():
        return None
    time_match = re.search(r"时间：([^\n]+)", header)
    return {
        "source": source.strip(),
        "translation": translation.strip(),
        "time": f"{file_path.stem} {time_match.group(1).strip()}" if time_match else file_path.stem,
        "file": str(file_path),
    }



















