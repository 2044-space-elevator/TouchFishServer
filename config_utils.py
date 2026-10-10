import re


# 服务器功能开关
FEATURE_DEFAULTS = {
    "chat": {
        "private_chat": True,
        "group_chat": True,
        "group_create": True,
        "friend_request": True,
    },
    "forum": True,
    "sticker": True,
    "announcement": True,
}


def get_features(cfg) -> dict:
    """读取功能开关并与默认值深合并（缺键/类型不对回退默认）。
    """
    raw = cfg.get("features") if isinstance(cfg, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    merged = {}
    for key, default in FEATURE_DEFAULTS.items():
        if isinstance(default, dict):
            sub = raw.get(key)
            sub = sub if isinstance(sub, dict) else {}
            merged[key] = {
                leaf: (sub.get(leaf) if isinstance(sub.get(leaf), bool) else leaf_default)
                for leaf, leaf_default in default.items()
            }
        else:
            value = raw.get(key)
            merged[key] = value if isinstance(value, bool) else default
    return merged


def feature_gate(cfg, *path) -> bool:
    """按嵌套路径读取功能开关，缺键类型不对时 1111
    """
    node = get_features(cfg)
    for part in path:
        if not isinstance(node, dict):
            return True
        node = node.get(part)
    return node if isinstance(node, bool) else True


def merge_features(current, incoming) -> dict:
    """把客户端提交的 features 深合并进当前值
    """
    if not isinstance(incoming, dict):
        raise ValueError("features must be an object")
    merged = get_features({"features": current})
    for key, value in incoming.items():
        if key not in FEATURE_DEFAULTS:
            raise ValueError("unknown feature key: {}".format(key))
        default = FEATURE_DEFAULTS[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise ValueError("feature group must be an object: {}".format(key))
            for leaf, leaf_value in value.items():
                if leaf not in default:
                    raise ValueError("unknown feature key: {}.{}".format(key, leaf))
                if not isinstance(leaf_value, bool):
                    raise ValueError("feature value must be boolean: {}.{}".format(key, leaf))
                merged[key][leaf] = leaf_value
        else:
            if not isinstance(value, bool):
                raise ValueError("feature value must be boolean: {}".format(key))
            merged[key] = value
    return merged


# 服务器可配置项
# type: str / int / bool / enum；enum 附带 options。
SETTINGS_SPEC = [
    {"key": "server_name", "type": "str", "default": "TouchFish", "category": "general"},
    {"key": "media_features", "type": "bool", "default": True, "category": "general"},
    {"key": "legacy_auth_enabled", "type": "bool", "default": True, "category": "security"},
    {"key": "jwt_expires_seconds", "type": "int", "min": 60, "default": 3600, "category": "security"},
    {"key": "jwt_refresh_expires_seconds", "type": "int", "min": 60, "default": 604800, "category": "security"},
    {"key": "jwt_max_per_user", "type": "int", "min": 0, "allow_unlimited": True, "default": 5, "category": "security"},
    {"key": "captcha", "type": "bool", "default": False, "category": "security"},
    {"key": "file_last_time", "type": "int", "min": 0, "default": 72, "category": "storage"},
    {"key": "max_file_size", "type": "int", "min": 0, "allow_unlimited": True, "default": -1, "category": "storage"},
    {"key": "max_avatar_size", "type": "int", "min": 0, "allow_unlimited": True, "default": -1, "category": "storage"},
    {"key": "user_storage_quota", "type": "int", "min": 0, "allow_unlimited": True, "default": -1, "category": "storage"},
    {"key": "max_user_storage_quota", "type": "int", "min": 0, "allow_unlimited": True, "default": 73400320, "category": "storage"},
    {"key": "max_sticker_storage_quota", "type": "int", "min": 0, "allow_unlimited": True, "default": 31457280, "category": "storage"},
    {"key": "file_download_mode", "type": "enum", "options": ["redirect", "proxy"], "default": "redirect", "category": "storage"},
    {"key": "max_message_length", "type": "int", "min": 1, "default": 10000, "category": "limits"},
    {"key": "max_request_message_length", "type": "int", "min": 1, "default": 200, "category": "limits"},
    {"key": "min_search_length", "type": "int", "min": 1, "default": 2, "category": "limits"},
    {"key": "min_group_name_length", "type": "int", "min": 1, "default": 1, "category": "limits"},
    {"key": "max_group_name_length", "type": "int", "min": 1, "default": 50, "category": "limits"},
    {"key": "min_username_length", "type": "int", "min": 4, "default": 4, "category": "limits"},
    {"key": "min_password_length", "type": "int", "min": 1, "default": 1, "category": "limits"},
    {"key": "max_sign_length", "type": "int", "min": 1, "allow_unlimited": True, "default": 100, "category": "limits"},
    {"key": "max_introduction_length", "type": "int", "min": 1, "allow_unlimited": True, "default": 500, "category": "limits"},
    {"key": "max_post_content_length", "type": "int", "min": 1, "allow_unlimited": True, "default": 20000, "category": "limits"},
    {"key": "groups_limit", "type": "int", "min": 1, "allow_unlimited": True, "default": 30, "category": "groups"},
    {"key": "single_group_max_people", "type": "int", "min": 1, "allow_unlimited": True, "default": 200, "category": "groups"},
    {"key": "max_sticker_packs_per_user", "type": "int", "min": 1, "allow_unlimited": True, "default": 24, "category": "sticker"},
    {"key": "max_stickers_per_pack", "type": "int", "min": 1, "allow_unlimited": True, "default": 24, "category": "sticker"},
    {"key": "daily_sticker_pack_creation_limit", "type": "int", "min": 1, "allow_unlimited": True, "default": -1, "category": "sticker"},
    {"key": "max_sticker_size", "type": "int", "min": 1, "allow_unlimited": True, "default": 1048576, "category": "sticker"},
    {"key": "smtp_host", "type": "str", "default": "", "category": "smtp"},
    {"key": "smtp_port", "type": "int", "min": 1, "default": 465, "category": "smtp"},
    {"key": "smtp_use_ssl", "type": "bool", "default": True, "category": "smtp"},
    {"key": "reverse_proxy_enabled", "type": "bool", "default": False, "category": "network"},
    {"key": "proxy_count", "type": "int", "min": 0, "default": 1, "category": "network"},
    {"key": "features", "type": "object", "default": FEATURE_DEFAULTS, "category": "features"},
]


def normalize_default_join_targets(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.replace(',', ' ').split()
    if not isinstance(raw, list):
        raise ValueError("default_join_targets must be a list")
    targets = []
    for value in raw:
        target = str(value).strip().upper()
        if not re.fullmatch(r"[UG][1-9][0-9]*", target):
            raise ValueError("invalid default join target")
        if target not in targets:
            targets.append(target)
    return targets
