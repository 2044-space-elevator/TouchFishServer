"""
ERROR！！！！！
错误都在这了！
"""

ERRORS = {
    "token_expired": ("AUTH_TOKEN_EXPIRED", "Token has expired", 401),
    "auth_failed": ("AUTH_FAILED", "Authentication failed", 401),
    "token_limit_reached": ("AUTH_TOKEN_LIMIT_REACHED", "Maximum number of active sessions reached", 403),
    "not_authenticated": ("AUTH_NOT_AUTHENTICATED", "Authentication required", 401),
    "current_token": ("AUTH_CANNOT_REVOKE_CURRENT", "Cannot revoke the current session token", 400),
    "forbidden": ("PERMISSION_DENIED", "You do not have permission to perform this action", 403),
    "not_friends": ("PERMISSION_NOT_FRIENDS", "Users are not friends", 403),
    "not_group_member": ("PERMISSION_NOT_GROUP_MEMBER", "User is not a member of this group", 403),
    "invalid_request": ("VALIDATION_INVALID_REQUEST", "Request parameters are invalid", 400),
    "Invalid uid": ("VALIDATION_INVALID_UID", "User ID is invalid", 400),
    "invalid_uid": ("VALIDATION_INVALID_UID", "User ID is invalid", 400),
    "Invalid filename": ("VALIDATION_INVALID_FILENAME", "Filename is invalid", 400),
    "invalid_filename": ("VALIDATION_INVALID_FILENAME", "Filename is invalid", 400),
    "Extension not allowed": ("VALIDATION_EXTENSION_NOT_ALLOWED", "File extension is not allowed", 400),
    "Invalid file hash": ("VALIDATION_INVALID_FILE_HASH", "File hash is invalid", 400),
    "invalid_file_hash": ("VALIDATION_INVALID_FILE_HASH", "File hash is invalid", 400),
    "Invalid chunk parameters": ("VALIDATION_INVALID_CHUNK_PARAMETERS", "Chunk parameters are invalid", 400),
    "invalid_base64": ("VALIDATION_INVALID_BASE64", "Base64 data is invalid", 400),
    "invalid_target": ("VALIDATION_INVALID_TARGET", "Message target is invalid", 400),
    "invalid_quote": ("VALIDATION_INVALID_QUOTE", "Quoted message is invalid", 400),
    "invalid_call_id": ("VALIDATION_INVALID_CALL_ID", "Call ID is invalid", 400),
    "message_too_long": ("VALIDATION_MESSAGE_TOO_LONG", "Message is too long", 400),
    "not_found": ("RESOURCE_NOT_FOUND", "Requested resource was not found", 404),
    "user_not_found": ("RESOURCE_USER_NOT_FOUND", "User does not exist", 404),
    "User not found": ("RESOURCE_USER_NOT_FOUND", "User does not exist", 404),
    "group_not_found": ("RESOURCE_GROUP_NOT_FOUND", "Group does not exist", 404),
    "unavailable": ("RESOURCE_UNAVAILABLE", "Resource is unavailable", 503),
    "User banned": ("RESOURCE_USER_BANNED", "User account is banned", 403),
    "user_banned": ("RESOURCE_USER_BANNED", "User account is banned", 403),
    "banned": ("RESOURCE_USER_BANNED", "User account is banned", 403),
    "file_not_owned": ("FILE_NOT_OWNED", "You do not own this file", 403),
    "file_unavailable": ("FILE_UNAVAILABLE", "File is unavailable", 404),
    "File too large": ("FILE_TOO_LARGE", "File exceeds the maximum size", 413),
    "Chunk too large": ("FILE_CHUNK_TOO_LARGE", "Chunk exceeds the maximum size", 413),
    "Storage quota exceeded": ("FILE_STORAGE_QUOTA_EXCEEDED", "Storage quota has been exceeded", 507),
    "sticker_storage_quota_exceeded": ("STICKER_QUOTA_EXCEEDED", "Sticker storage quota has been exceeded", 507),
    "Too many concurrent uploads": ("FILE_TOO_MANY_UPLOADS", "Too many concurrent uploads", 429),
    "Missing parameter": ("VALIDATION_MISSING_PARAMETER", "A required parameter is missing", 400),
    "Decode failed": ("FILE_DECODE_FAILED", "File data could not be decoded", 400),
    "Missing file_id": ("FILE_MISSING_FILE_ID", "file_id is required", 400),
    "Invalid file_id": ("FILE_INVALID_FILE_ID", "File ID is invalid", 400),
    "chunk_total mismatch": ("FILE_CHUNK_TOTAL_MISMATCH", "Chunk total does not match the upload", 400),
    "Write failed": ("FILE_WRITE_FAILED", "File could not be written", 500),
    "Directory creation failed": ("FILE_DIRECTORY_CREATION_FAILED", "Upload directory could not be created", 500),
    "Failed to record chunk info": ("FILE_CHUNK_INFO_FAILED", "Chunk information could not be recorded", 500),
    "Failed to read chunk info": ("FILE_CHUNK_READ_FAILED", "Chunk information could not be read", 500),
    "Hash verification failed": ("FILE_HASH_VERIFICATION_FAILED", "File hash verification failed", 400),
    "Finalization failed": ("FILE_FINALIZATION_FAILED", "File upload finalization failed", 500),
    "file_reference_failed": ("FILE_REFERENCE_FAILED", "File reference could not be created", 500),
    "upload_failed": ("FILE_UPLOAD_FAILED", "File upload failed", 500),
    "unsupported_sticker_type": ("STICKER_UNSUPPORTED_TYPE", "Sticker type is not supported", 400),
    "sticker_too_large": ("STICKER_TOO_LARGE", "Sticker exceeds the maximum size", 413),
    "client_mid_conflict": ("MESSAGE_CLIENT_MID_CONFLICT", "Message client ID conflicts with existing content", 409),
    "already_recalled": ("MESSAGE_ALREADY_RECALLED", "Message has already been recalled", 409),
    "rate_limited": ("RATE_LIMITED", "Too many requests", 429),
    "conflict": ("CONFLICT", "Resource conflict", 409),
    "Password incorrect": ("AUTH_INVALID_PASSWORD", "Password is incorrect", 401),
    "Server error": ("SERVER_ERROR", "Internal server error", 500),
}


def normalize_error(response, detailed=False):
    if not isinstance(response, dict) or "error" not in response or not detailed:
        return response, 200
    old_code = str(response["error"])
    metadata = ERRORS.get(old_code)
    if metadata is None and old_code.startswith("Missing chunk "):
        metadata = ("FILE_MISSING_CHUNK", "One or more file chunks are missing", 400)
    code, message, status = metadata or ("SERVER_ERROR", old_code, 500)
    normalized = dict(response)
    normalized["error"] = code
    normalized["error_message"] = normalized.get("error_message", message)
    return normalized, status
