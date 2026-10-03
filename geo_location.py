"""
IP 地理位置解析器

真的有人需要……吗？
"""

import requests
from typing import Optional, Dict
import time
from functools import lru_cache


class GeoLocationService:
    """地理位置解析服务"""
    
    def __init__(self, cache_ttl: int = 86400):
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, tuple] = {}  # {ip: (location, timestamp)}
    
    @lru_cache(maxsize=1000)
    def _resolve_ip_cached(self, ip: str) -> Optional[str]:
        """解析 IP 地址（带缓存）"""
        return self._resolve_ip_internal(ip)
    
    def _resolve_ip_internal(self, ip: str) -> Optional[str]:
        """
        解析 IP 地址的地理位置
        
        优先级：
        1. 本地 GeoIP
        2. ip-api.com 免费 API
        3. 降级返回 IP 本身
        """
        if not ip or ip == "127.0.0.1" or ip.startswith("192.168.") or ip.startswith("10."):
            return "Local Network"
        
        # 尝试使用 ip-api.com
        try:
            response = requests.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,country,regionName,city"},
                timeout=3
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "success":
                    parts = []
                    if data.get("city"):
                        parts.append(data["city"])
                    if data.get("regionName"):
                        parts.append(data["regionName"])
                    if data.get("country"):
                        parts.append(data["country"])
                    
                    if parts:
                        return ", ".join(parts)
        except Exception as e:
            print(f"GeoLocation API error: {e}")
        
        # 降级：返回 IP 地址本身
        return ip
    
    def resolve_ip(self, ip: str) -> str:
        """
        解析 IP 地址（公开接口）
        
        Args:
            ip: IP 地址字符串
        
        Returns:
            地理位置字符串（如 "Beijing, China"）
        """
        if not ip:
            return "Unknown"
        
        # 检查缓存
        now = time.time()
        if ip in self._cache:
            location, timestamp = self._cache[ip]
            if now - timestamp < self.cache_ttl:
                return location
        
        # 解析并缓存
        location = self._resolve_ip_cached(ip)
        if location:
            self._cache[ip] = (location, now)
            return location
        
        return "Unknown"
    
    def clear_cache(self):
        """清除缓存"""
        self._cache.clear()
        self._resolve_ip_cached.cache_clear()


class SuspiciousLoginDetector:
    """可疑登录检测器"""
    
    def __init__(self, geo_service: GeoLocationService):
        self.geo_service = geo_service
    
    def is_suspicious(
        self,
        uid: int,
        new_ip: str,
        new_location: str,
        recent_sessions: list,
        new_device_id: str = None
    ) -> tuple[bool, str]:
        """
        检测登录可疑
        """
        if not recent_sessions:
            # 首次登录，不视为可疑
            return False, ""
        
        # 规则 1：同一设备从不同国家登录
        for session in recent_sessions:
            if new_device_id and session.get('device_id') == new_device_id:
                old_location = session.get('location', '')
                if old_location and new_location:
                    old_country = self._extract_country(old_location)
                    new_country = self._extract_country(new_location)
                    
                    if old_country and new_country and old_country != new_country:
                        time_diff = time.time() - session.get('last_seen', 0)
                        if time_diff < 3600:  # 1小时内
                            return True, f"Same device from different country within 1 hour (was {old_country}, now {new_country})"
        
        # 规则 2：新设备从异地登录
        known_locations = set()
        for session in recent_sessions:
            loc = session.get('location', '')
            if loc:
                known_locations.add(self._extract_country(loc))
        
        if known_locations and new_location:
            new_country = self._extract_country(new_location)
            if new_country and new_country not in known_locations:
                # 新设备检测
                known_devices = {s.get('device_id') for s in recent_sessions if s.get('device_id')}
                if new_device_id and new_device_id not in known_devices:
                    return True, f"New device from new location: {new_location}"
        
        # 规则 3：短时间内多次登录失败后成功
        
        return False, ""
    
    def _extract_country(self, location: str) -> Optional[str]:
        """从地理位置字符串中提取国家"""
        if not location:
            return None
        
        parts = [p.strip() for p in location.split(',')]
        if parts:
            return parts[-1]  # 最后一部分通常是国家
        
        return None


# 全局单例
_geo_service_instance = None


def get_geo_service() -> GeoLocationService:
    """获取地理位置服务单例"""
    global _geo_service_instance
    if _geo_service_instance is None:
        _geo_service_instance = GeoLocationService()
    return _geo_service_instance
