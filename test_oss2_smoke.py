"""OSS2 适配冒烟测试：验证核心逻辑不依赖真实 OSS 凭据即可工作。"""
import os
import sys
import tempfile

# 确保工作目录正确
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import oss_store

print("=== OSS2 冒烟测试 ===")

# 1. 验证 oss2 库可用
print("1. oss2 library available:", oss_store._OSS2_AVAILABLE)
assert oss_store._OSS2_AVAILABLE, "oss2 must be installed"

# 2. 验证本地存储时 is_oss_enabled 返回 False
cfg_local = {"storage_backend": "local"}
assert not oss_store.is_oss_enabled(7001, cfg_local), "local backend should not enable OSS"
print("2. local backend correctly returns is_oss_enabled=False")

# 3. 验证 OSS2 配置缺少字段时返回 False
cfg_incomplete = {"storage_backend": "oss2", "oss2_authid": "x"}
assert not oss_store.is_oss_enabled(7001, cfg_incomplete), "incomplete config should not enable OSS"
print("3. incomplete OSS config correctly returns is_oss_enabled=False")

# 4. 验证完整 OSS2 配置时返回 True
cfg_oss = {
    "storage_backend": "oss2",
    "oss2_authid": "test-id",
    "oss2_authkey": "test-key",
    "oss2_endpoint": "oss-cn-hangzhou.aliyuncs.com",
    "oss2_bucket": "test-bucket",
}
assert oss_store.is_oss_enabled(7001, cfg_oss), "complete OSS config should enable OSS"
print("4. complete OSS config correctly returns is_oss_enabled=True")

# 5. 验证 Object Key 生成
assert oss_store.object_key("file", "abc") == "file/abc.file"
assert oss_store.object_key("sticker", "def") == "sticker/def.file"
print("5. object_key generation correct: file/abc.file, sticker/def.file")

