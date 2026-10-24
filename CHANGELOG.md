# 更新日志

## 1.1.1

- 文档：README / metadata / 代码示例里的群号改为 `123456789`，平台 ID 改为占位，不再出现作者本机的群与平台 ID

## 1.1.0

- 新增一句话直发：`filter.regex` 预筛 + 纯规则解析，不发大模型请求也能发图；模型被限流时仍然可用
- 图库改自动发现：不再写死 private_companion / meme_manager 的路径，扫 `plugin_data` 下所有带 `catalog.json` 或 `packs/` 的目录，也可用 `libraries` 手动追加
- 图库解析兼容 `stored_name` / `file` / `filename` / `path` 等多种字段写法
- 图库结果缓存 120 秒，图库变动后不需要重启
- 一句话直发成功后会记录在这次事件上，模型再调用工具不会重复发第二张
- 冷却提示改成「冷却中：N 秒内只发一张」，读起来不像报错

## 1.0.0

- `/群发表情 <群号|群名> [心情]`、`/群列表`
- LLM 工具 `send_meme_to_group`、`send_text_to_group`
- 插件自己拼 `平台ID:GroupMessage:群号`，绕开 invalid session
- 按心情标签选图，最近发过的降权
