"""
服务端配置管理功能
现在被拆出来了
"""

from typing import Optional, Dict, Any
import toml


class ServerConfigManager:
    """服务端配置管理器"""
    
    def __init__(self, config_path: str = "config.toml"):
        self.config_path = config_path
        self._config_cache: Optional[Dict[str, Any]] = None
    
    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return toml.load(f)
        except Exception as e:
            print(f"Failed to load config: {e}")
            return {}
    
    def _save_config(self, config: Dict[str, Any]) -> bool:
        """保存配置文件"""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                toml.dump(config, f)
            self._config_cache = None  # 清除缓存
            return True
        except Exception as e:
            print(f"Failed to save config: {e}")
            return False
    
    def get_auth_settings(self) -> Dict[str, Any]:
        """获取认证相关配置"""
        config = self._load_config()
        auth = config.get('auth', {})
        
        return {
            'max_sessions_per_user': auth.get('max_sessions_per_user', 5),
            'token_ttl': auth.get('token_ttl', 3600),
            'refresh_ttl': auth.get('refresh_ttl', 604800),
            'allow_legacy_auth': auth.get('allow_legacy_auth', True),
        }
    
    def update_max_sessions(self, max_sessions: int) -> bool:
        """更新会话配额"""
        if max_sessions < 1 or max_sessions > 100:
            return False
        
        config = self._load_config()
        if 'auth' not in config:
            config['auth'] = {}
        
        config['auth']['max_sessions_per_user'] = max_sessions
        return self._save_config(config)
    
    def update_token_ttl(self, token_ttl: int, refresh_ttl: int) -> bool:
        """更新 Token 有效期"""
        if token_ttl < 60 or token_ttl > 86400:  # 1分钟到24小时
            return False
        if refresh_ttl < token_ttl or refresh_ttl > 2592000:  # 最多30天
            return False
        
        config = self._load_config()
        if 'auth' not in config:
            config['auth'] = {}
        
        config['auth']['token_ttl'] = token_ttl
        config['auth']['refresh_ttl'] = refresh_ttl
        return self._save_config(config)
    
    def get_session_statistics(self, user_cursor) -> Dict[str, Any]:
        """获取会话统计信息"""
        stats = user_cursor.get_session_stats()
        
        return {
            'total_sessions': stats.get('total_sessions', 0),
            'active_sessions': stats.get('active_sessions', 0),
            'users_at_limit': stats.get('users_at_limit', 0),
            'avg_sessions_per_user': stats.get('avg_sessions_per_user', 0.0),
            'max_sessions_config': self.get_auth_settings()['max_sessions_per_user'],
        }


def add_config_routes(app, user_cursor, jwt_secret):
    """添加配置管理路由"""
    
    config_manager = ServerConfigManager()
    
    @app.route('/admin/config/auth', methods=['POST'])
    def get_auth_config():
        """获取认证配置"""
        content = return_app_route(app)
        if not content:
            return {"error": "invalid_request"}
        
        # 验证管理员权限
        ok, result = resolve_auth_for_admin(content, user_cursor, jwt_secret)
        if not ok:
            return result
        
        settings = config_manager.get_auth_settings()
        stats = config_manager.get_session_statistics(user_cursor)
        
        return {
            'settings': settings,
            'statistics': stats,
        }
    
    @app.route('/admin/config/auth/update', methods=['POST'])
    def update_auth_config():
        """更新认证配置"""
        content = return_app_route(app)
        if not content:
            return {"error": "invalid_request"}
        
        # 验证管理员权限
        ok, result = resolve_auth_for_admin(content, user_cursor, jwt_secret)
        if not ok:
            return result
        
        max_sessions = content.get('max_sessions_per_user')
        token_ttl = content.get('token_ttl')
        refresh_ttl = content.get('refresh_ttl')
        
        success = True
        if max_sessions is not None:
            success = success and config_manager.update_max_sessions(max_sessions)
        
        if token_ttl is not None and refresh_ttl is not None:
            success = success and config_manager.update_token_ttl(token_ttl, refresh_ttl)
        
        if success:
            return {'success': True, 'message': 'Configuration updated successfully'}
        else:
            return {'error': 'invalid_parameters'}
    
    return app


def resolve_auth_for_admin(content, user_cursor, jwt_secret):
    """管理员权限验证"""
    token = content.get("token")
    if not token:
        return False, {"error": "authentication_required"}
    
    import jwt_tool
    payload = jwt_tool.verify_token(jwt_secret, token)
    if payload is None:
        return False, {"error": "token_expired"}
    
    try:
        uid = int(payload.get("sub"))
    except (TypeError, ValueError):
        return False, {"error": "token_expired"}
    
    row = user_cursor.uid_query(uid)
    if not row or row[2] not in {'admin', 'root'}:
        return False, {"error": "permission_denied"}
    
    return True, {'uid': uid, 'auth': row[2]}
