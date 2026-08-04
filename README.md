# 三角洲行动国服 → Discord 单频道推送

自动检查《三角洲行动》国服官网，把新的更新、活动、补偿、版本和重要公告发送到一个 Discord 文字频道。

## 特性

- 不需要 Discord Bot Token，只使用绑定到目标频道的 Webhook。
- 不需要电脑或本地脚本长期在线，由 GitHub Actions 定时运行。
- 首次启动只建立基线，不会一次性发送历史公告。
- 通过 `state.json` 去重；同一公告不会重复发送。
- 默认过滤纯赛事、战队和赛程资讯。
- 直接读取官网当前使用的腾讯 Milo 动态新闻接口，并自动发现当前流程参数。
- 每次最多扫描 60 条，自动分页；接口异常、空数据或陈旧数据会让任务失败，不会把旧静态快照当成成功。
- GitHub Actions 会先运行 20 项单元测试，再接触 Webhook Secret。
- Actions 固定到完整提交，Ubuntu 依赖使用 SHA-256 哈希锁定；Windows 本地依赖也固定版本。

## 1. Discord：只创建一个频道和一个 Webhook

1. 创建文字频道，例如 `#三角洲公告`。
2. 可选：将 `@everyone` 的“发送消息”关闭，保留“查看频道”和“读取消息历史”。
3. 打开频道设置 → **整合 / Integrations** → **Webhook** → **新建 Webhook**。
4. 名称可填 `三角洲国服情报站`，频道选择刚才的唯一文字频道。
5. 复制 Webhook URL。此地址等同于发消息凭据，不要发到聊天、截图或代码仓库。

## 2. GitHub：创建仓库并上传本项目

建议创建 **Public** 仓库，例如 `delta-force-cn-discord-push`。项目代码没有账号信息；Webhook URL 会放在 GitHub Secret 中，不会出现在公开代码里。

将本目录中的全部文件上传到仓库根目录，必须保留 `.github/workflows/push.yml` 路径。

## 3. 添加 Secret

仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**：

- Name：`DISCORD_WEBHOOK_URL`
- Secret：粘贴 Discord Webhook URL

## 4. state.json 写入权限

工作流已在 `.github/workflows/push.yml` 内只申请 `contents: write`，用于提交 `state.json`。通常不需要把整个仓库的默认工作流权限改为 Read and write。

如果日志明确出现 `git push` 403，再检查仓库或组织的 Actions 策略、Ruleset/分支保护是否禁止 GitHub Actions 写默认分支。

## 5. 第一次测试

1. 打开仓库的 **Actions**。
2. 选择 **Delta Force CN Discord Push**。
3. 点击 **Run workflow**。
4. `mode` 选择 `test`，再点击绿色的 **Run workflow**。
5. 工作流变成绿色后，唯一 Discord 频道应出现“推送测试成功”。测试模式不会修改去重状态。

## 6. 正式初始化

再次手动运行，`mode` 改为 `normal`。

频道会收到“监控已启用”。程序会把当前官网条目写入 `state.json`，但不发送旧公告。以后工作流大约每 15 分钟检查一次，只发新内容。GitHub 定时任务可能偶尔延迟。

从旧版静态源升级时，`source_version` 变化会自动重新建立一次基线，避免把几十条历史文章当成新内容刷屏。

## 手动重发最新一条

Actions → Run workflow → `mode: resend_latest`。

该模式只用于检查排版，不修改去重状态。

## 默认会推送什么

会推送：更新、维护、修复、平衡调整、补偿、版本、新赛季、福利、免费领取、联动/联名、返场、签到、三角券、兑换、预告、招募、测试服、共创活动和重要公告等。

默认忽略：纯职业联赛、主播巅峰赛、赛程、战队、俱乐部等赛事新闻。可在 `config.json` 修改关键词。

## 故障处理

- **Actions 报缺少 `DISCORD_WEBHOOK_URL`**：Secret 名称必须完全一致。
- **Discord 返回 401/404**：Webhook 被删除或重置；重新复制并覆盖 Secret。
- **工作流成功但没有新消息**：正常情况；没有新公告时不会发“无更新”消息。
- **state.json 无法推送**：检查 Actions 策略、Ruleset 和分支保护；工作流本身已声明最小写权限。
- **动态接口结构无效或数据陈旧**：任务会主动失败。不要关闭保护；先检查官网是否改版，再更新 Milo 流程解析。
- **偶发 Discord 429**：程序会按 Discord 返回的等待时间重试；无需手动处理。
- **想完全重新建立基线**：将 `state.json` 恢复为初始内容后，手动运行 `normal`。

## 安全

不要把 Discord Webhook URL 写入 `config.json`、README、Issue、Actions 日志或任何公开提交。程序会主动隐藏异常中的 Webhook URL，但 Secret 仍应只在 GitHub 设置页粘贴。若泄露，在 Discord 的 Webhook 设置中删除旧 Webhook，再创建新的并更新 Secret。

Discord Webhook 与 Git 提交无法组成真正的事务。极少数情况下，如果 Discord 已收消息但 `state.json` 最终提交失败，下一轮可能重复一次；Actions 会明确标红，需及时处理失败记录。
