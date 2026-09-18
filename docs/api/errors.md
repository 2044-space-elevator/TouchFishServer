# TouchFish V5 API 错误码

## 详细错误响应

所有 secret API 都支持在解密后的真实请求体中加入：

```json
{
    "detail_error": true
}
```

请求携带 `detail_error: true` 时，错误响应会返回规范化的大写错误码、原因（English），并使用对应的 HTTP 状态码：

```json
{
    "success": false,
    "error": "AUTH_FAILED",
    "error_message": "Authentication failed"
}
```

错误响应仍会按照 secret API 的 RSA + AES 方式加密。`error_message` 是服务端提供的英文回退文本。

如果请求没有携带 `detail_error`，服务器保持旧行为：错误码使用旧格式，HTTP 状态码保持原有兼容行为。因此旧客户端无需修改即可继续访问 API。


## 错误码列表

### 认证、权限与资源

| 错误码 | `error_message` | HTTP |
| --- | --- | ---: |
| `AUTH_CANNOT_REVOKE_CURRENT` | Cannot revoke the current session token | 400 |
| `AUTH_FAILED` | Authentication failed | 401 |
| `AUTH_INVALID_PASSWORD` | Password is incorrect | 401 |
| `AUTH_NOT_AUTHENTICATED` | Authentication required | 401 |
| `AUTH_TOKEN_EXPIRED` | Token has expired | 401 |
| `AUTH_TOKEN_LIMIT_REACHED` | Maximum number of active sessions reached | 403 |
| `PERMISSION_DENIED` | You do not have permission to perform this action | 403 |
| `PERMISSION_NOT_FRIENDS` | Users are not friends | 403 |
| `PERMISSION_NOT_GROUP_MEMBER` | User is not a member of this group | 403 |
| `RESOURCE_NOT_FOUND` | Requested resource was not found | 404 |
| `RESOURCE_USER_NOT_FOUND` | User does not exist | 404 |
| `RESOURCE_GROUP_NOT_FOUND` | Group does not exist | 404 |
| `RESOURCE_USER_BANNED` | User account is banned | 403 |
| `RESOURCE_UNAVAILABLE` | Resource is unavailable | 503 |

### 请求验证

| 错误码 | `error_message` | HTTP |
| --- | --- | ---: |
| `VALIDATION_INVALID_REQUEST` | Request parameters are invalid | 400 |
| `VALIDATION_INVALID_UID` | User ID is invalid | 400 |
| `VALIDATION_INVALID_FILENAME` | Filename is invalid | 400 |
| `VALIDATION_EXTENSION_NOT_ALLOWED` | File extension is not allowed | 400 |
| `VALIDATION_INVALID_FILE_HASH` | File hash is invalid | 400 |
| `VALIDATION_INVALID_CHUNK_PARAMETERS` | Chunk parameters are invalid | 400 |
| `VALIDATION_INVALID_BASE64` | Base64 data is invalid | 400 |
| `VALIDATION_INVALID_TARGET` | Message target is invalid | 400 |
| `VALIDATION_INVALID_QUOTE` | Quoted message is invalid | 400 |
| `VALIDATION_INVALID_CALL_ID` | Call ID is invalid | 400 |
| `VALIDATION_MESSAGE_TOO_LONG` | Message is too long | 400 |
| `VALIDATION_MISSING_PARAMETER` | A required parameter is missing | 400 |

### 文件

| 错误码 | `error_message` | HTTP |
| --- | --- | ---: |
| `FILE_NOT_OWNED` | You do not own this file | 403 |
| `FILE_UNAVAILABLE` | File is unavailable | 404 |
| `FILE_TOO_LARGE` | File exceeds the maximum size | 413 |
| `FILE_CHUNK_TOO_LARGE` | Chunk exceeds the maximum size | 413 |
| `FILE_STORAGE_QUOTA_EXCEEDED` | Storage quota has been exceeded | 507 |
| `FILE_TOO_MANY_UPLOADS` | Too many concurrent uploads | 429 |
| `FILE_DECODE_FAILED` | File data could not be decoded | 400 |
| `FILE_MISSING_FILE_ID` | file_id is required | 400 |
| `FILE_INVALID_FILE_ID` | File ID is invalid | 400 |
| `FILE_CHUNK_TOTAL_MISMATCH` | Chunk total does not match the upload | 400 |
| `FILE_MISSING_CHUNK` | One or more file chunks are missing | 400 |
| `FILE_WRITE_FAILED` | File could not be written | 500 |
| `FILE_DIRECTORY_CREATION_FAILED` | Upload directory could not be created | 500 |
| `FILE_CHUNK_INFO_FAILED` | Chunk information could not be recorded | 500 |
| `FILE_CHUNK_READ_FAILED` | Chunk information could not be read | 500 |
| `FILE_HASH_VERIFICATION_FAILED` | File hash verification failed | 400 |
| `FILE_FINALIZATION_FAILED` | File upload finalization failed | 500 |
| `FILE_REFERENCE_FAILED` | File reference could not be created | 500 |
| `FILE_UPLOAD_FAILED` | File upload failed | 500 |

### 表情包、消息及其他

| 错误码 | `error_message` | HTTP |
| --- | --- | ---: |
| `STICKER_UNSUPPORTED_TYPE` | Sticker type is not supported | 400 |
| `STICKER_TOO_LARGE` | Sticker exceeds the maximum size | 413 |
| `STICKER_QUOTA_EXCEEDED` | Sticker storage quota has been exceeded | 507 |
| `MESSAGE_CLIENT_MID_CONFLICT` | Message client ID conflicts with existing content | 409 |
| `MESSAGE_ALREADY_RECALLED` | Message has already been recalled | 409 |
| `RATE_LIMITED` | Too many requests | 429 |
| `CONFLICT` | Resource conflict | 409 |
| `SERVER_ERROR` | Internal server error | 500 |

当服务端遇到尚未登记的旧错误文本时，会使用 `SERVER_ERROR` 作为错误码，并将原始文本放入 `error_message`。
