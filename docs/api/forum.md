# TouchFish V5 Api 文档（论坛相关部分）

> 因为我懒得写了，所以我不说明就默认成功时返回时间戳加 True，否则返回时间戳加 False。

- `^ POST /forum/create_forum` 创建论坛。

请求体：

```
{
    "forum_name" : <forum_name>,
    "introduction" : <introduction>
}
```

需注意，创建论坛后不是马上出现在论坛页面，是进入审核页面，等待管理员审核。创建成功后，创建者会收到 `forum.review.submitted` 通知，所有管理员会收到 `forum.review.pending` 通知。

- `^ POST /forum/edit_forum` 编辑论坛信息。

**只有论坛创建者有权限操作。** 编辑后需重新进入审核队列，等待管理员审核。

请求体：

```
{
    "fid" : <fid>,
    "forum_name" : <forum_name>,
    "introduction" : <introduction>
}
```

编辑提交后，操作者会收到 `forum.review.submitted` 通知，所有管理员会收到 `forum.review.pending` 通知。

- `^ POST /forum/get_approving_forum_list` 获取所有在审核队列

**只有 ADMIN 和 ROOT 用户有权限访问。**

请求体：

```
{

}
```

返回体：

一个字符串化的 json，返回体例为：

```
{
    "queue_num" : <queue_num>,
    <qid> : {
        "creater" : <creater>,
        "forumname" : <forumname>,
        "introduction" : <introduction>
    }
    ...
}
```

其中 `<queue_num>` 是在队列的待审批论坛数量。

- `^ POST /forum/approve_forum` 批准论坛。

论坛审核通过后，论坛创建者会收到一条通知。

请求体：

```
{
    "qid" : <qid>
}
```

`<qid>` 是队列中的请求 id，获取方法见上。

- `^ POST /forum/reject_forum` 拒绝论坛。

论坛审核拒绝后，论坛创建者会收到一条通知。

请求体：

```
{
    "qid" : <qid>,
    "reason" : <reason>
}
```

其中 `<qid>` 是队列中的请求 id，`<reason>` 是可选的拒绝原因。拒绝通知中会区分是"创建"还是"编辑"的申请。

- `* GET /forum/get_forum_list` 获取所有论坛

返回体：

```
[
    [
        <forum_id>,
        <forumname>,
        <creater_id>,
        <create_time>,
        <introduction>,
        <post_num>
    ],
    ...
]
```

**依照的是论坛帖子数量降序排序**。

- `^ POST /forum/send_post` 发布帖子

请求体：

```
{
    "fid" : <fid>,
    "title" : <title>,
    "content" : <content>,
    "attachment_hashes" : [<file_hash>, ...]
}
```

`attachment_hashes` 可选，也可使用兼容格式 `"attachments": [{"hash": <file_hash>}, ...]`。每个哈希必须：

- 是 64 位十六进制 SHA-256 字符串；
- 对应一个仍然存在的文件；
- 属于发帖用户的有效文件记录；
- 在同一个帖子中不重复。

单个帖子最多可附加 `max_post_attachments` 个文件，默认值为 `20`；超过限制时发布失败。

附件按照请求数组顺序保存。发布成功后帖子会持有文件引用，用户从个人文件列表删除该文件时不会破坏帖子附件。

如果帖子标题和正文中包含 `@用户名`，被提及的用户会收到 `forum.post.mentioned` 通知。

- `* GET /forum/get_post_list/<fid>` 获取论坛帖子（按 `pid` 游标分页）

查询参数（都可省略，省略时行为与旧版一致：一次返回全部帖子）：

| 参数 | 说明 |
| --- | --- |
| `limit` | 本页最多返回多少条，服务端会夹在 `1..100` |
| `before_pid` | 只返回 `pid` 小于它的帖子（上一页最早一条的 `pid`） |

返回体：

```json
{
    "post_rows" : [<post_object>, ...],
    "pinned_pid" : <pinned_post_id_or_null>,
    "has_more" : <bool>,
    "next_before_pid" : <pid_or_null>
}
```

> 破坏性变更：旧版的 `posts`（`SELECT * FROM contents` 的原始行）已移除，
> 它与 `post_rows` 各带一份正文，响应体直接翻倍。请只读 `post_rows`。

`post_rows` 内按 `pid` 升序；`has_more` 为 `true` 时，用 `next_before_pid`
作为下一页的 `before_pid` 继续往前翻。`pinned_pid` 指向的帖子可能不在本页
（它可能比本页所有帖子都旧），客户端需要时请单独调用 `/forum/get_post`。

`post_object`：

```json
{
    "fid" : <fid>,
    "pid" : <pid>,
    "title" : <title>,
    "creater" : <author_uid>,
    "author_uid" : <author_uid>,
    "content" : <content>,
    "send_time" : <send_time>,
    "comment_count" : <int>,
    "last_comment" : {
        "time_stamp" : <comment_time_stamp>,
        "uid" : <comment_author_uid>,
        "content" : <comment_content>
    },
    "attachments" : [
        {
            "hash" : <file_hash>,
            "file_name" : <poster_file_name>,
            "filename" : <poster_file_name>,
            "size" : <size>,
            "mime_type" : <mime_type>,
            "extension" : <extension>,
            "download_url" : "/file/get_file/<file_hash>",
            "send_time" : <file_send_time>,
            "position" : <position>
        }
    ]
}
```

`comment_count` / `last_comment` 由列表接口一次批量查出，卡片不需要再为
每篇帖子单独拉评论；`last_comment` 在没有评论时为 `null`。

没有附件的帖子不包含 `attachments` 字段。附件的 `file_name` 是发帖者上传时使用的名称，不会回退到其他上传者为相同内容使用的名称。

- `* GET /forum/get_post/<fid>/<pid>` 获取单个帖子。

返回一个上述 `post_object`；帖子不存在或路径参数非法时返回空对象。

- `^ POST /forum/remove_post` 删除帖子

**只有帖子发布者、论坛版主/创始人、服务器管理员有权限操作**

请求体：

```
{
    "fid" : <fid>,
    "pid" : <pid>
}
```

如果操作者不是帖子发布者（即由论坛创始人或管理员删除），帖子发布者会收到 `forum.post.deleted` 通知。

删除帖子时会同时删除附件关系并减少对应文件引用；只有在文件既无有效拥有者也无其他消息/帖子引用时，物理文件才可被清理。


- `^ POST /forum/remove_forum` 删除论坛

**只有论坛创始人、管理员有权限操作**

请求体：

```
{
    "fid" : <fid>,
}
```

- `^ POST /forum/comment`

评论。

请求体：

```
{
    "fid" : <fid>,
    "pid" : <pid>,
    "comment" : <comment>
}
```

其中 `<fid>` 是论坛编号，`<pid>` 是论坛中的帖子编号。

帖子作者会在收到新评论时得到一条通知；如果评论中包含 `@用户名`，被提及用户也会收到通知。

- `* GET /forum/get_all_comments/<fid>/<pid>` 返回一个帖子的所有评论。

- `^ POST /forum/remove_comment` 删除评论。

只有评论发布者、论坛版主/创始人、服务器管理员有权限操作。

请求体：

```
{
    "fid" : <fid>,
    "pid" : <pid>,
    "send_time" : <send_time>
}
```

其中 `<send_time>` 是评论的时间戳。

如果操作者不是评论发布者（即由他人删除），评论发布者会收到 `forum.comment.deleted` 通知。

## 论坛通知事件类型

论坛相关的事件通知（参见[通知文档](notification.md)）：

- `forum.approved` — 论坛创建/编辑通过审核
- `forum.rejected` — 论坛创建/编辑被拒绝
- `forum.review.submitted` — 论坛创建/编辑已提交审核（发送给操作者）
- `forum.review.pending` — 新的论坛审核等待处理（发送给管理员）
- `forum.comment.created` — 帖子收到新评论
- `forum.comment.mentioned` — 在评论中被 @提及
- `forum.post.mentioned` — 在帖子中被 @提及
- `forum.post.deleted` — 帖子被他人删除
- `forum.comment.deleted` — 评论被他人删除
# 搜索

- `GET /forum/search?query=<文本>&fid=<可选论坛ID>&offset=0&limit=30`：按论坛名、简介、帖子标题和正文进行服务端分页搜索。返回 `forums`、`posts` 和 `total`。
