# 两种运行方式

完整版：`python -m uvicorn server.app:app --host 0.0.0.0 --port 8000`，保留 SQLite 和 5/15/30 分钟历史。

Action 版：本地采集端 → GitHub repository_dispatch → Action → 轻量接收端 → 网页。
Action 是一次性转发任务，仍需一个公网可访问的接收端作为网页共享出口。这里提供的接收端只存内存，不创建数据库，也没有历史接口。

## 配置轻量接收端

在服务器安装 `pip install -r requirements-server.txt`。PowerShell 示例：

```powershell
$env:HR_RELAY_TOKEN = "独立的转发写入令牌"
$env:HR_SERVER_TOKEN = "独立的网页查看令牌"
python -m uvicorn relay.app:app --host 0.0.0.0 --port 8000 --workers 1
```

用反向代理提供 HTTPS 和 WebSocket，网页打开接收端域名并输入查看令牌。
接收端每人仅保留一条最新值，按采样时间 180 秒过期，最多 500 人；每 5 秒清理，重启清空。乱序、重复数据不会覆盖较新值，超过 3 分钟的排队数据被拒绝。页面状态每 2 秒更新，不意味着采集上传也是每 2 秒。

## 配置 GitHub

将代码推到自己的私有仓库默认分支，确保 `.github/workflows/heart-rate-relay.yml` 存在且 Actions 已启用。仓库 Actions Secrets 设置：

- `RELAY_TARGET_URL`：例如 `https://你的域名/api/relay`（必须 HTTPS，不跟随重定向）。
- `RELAY_TARGET_TOKEN`：与接收端 `HR_RELAY_TOKEN` 一致。

采集端由仓库拥有者操作，使用自己的、仅限该仓库且具有 Contents: write 的 fine-grained PAT。令牌不打包进客户端，不分发给其他用户：可以在采集端的“GitHub 令牌”栏填写（明文保存在本机用户目录的 `.heart-rate-sharing.json`），或留空并通过环境变量传入：

```powershell
$env:HR_GITHUB_TOKEN = "自己的 GitHub 令牌"
python client/app.py
```

选择“Action 转发”，GitHub 仓库填写 `owner/repository`，填写名字并开始采集。“看板地址”填写接收端地址、“服务端令牌”填写接收端的查看令牌后，窗口右侧可以看到其他人的最新心率。人员 ID 保存到本地配置，重启和改名仍是同一人。

每 60 秒最多提交一次当时的最新采样，无离线补传。提交成功只表示 GitHub 接受了事件，不代表网页已收到。排队时间不可控；此版不是实时传输，也不适合给大量用户分发使用。实际运行次数约为每人每小时 60 次，会消耗 Actions 配额。结束本地采集即可停止提交。

“没有历史”指应用接收端和网页；dispatch 事件数据仍会进入 GitHub 的工作流运行环境，不能承诺 GitHub 不保留。工作流不打印采样，不写仓库文件或 Artifact。私有仓库限制 GitHub 侧访问范围。

如需多人正式使用，请直连服务端。
