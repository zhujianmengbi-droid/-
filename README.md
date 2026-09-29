# ZJ HUB · DEV King

面向起床战争的 Windows 工具中心，包含电脑清理、快捷工具、战局数据、排行榜和公共大厅聊天。

## 下载

前往 [GitHub Releases](https://github.com/zhujianmengbi-droid/-/releases) 获取 Windows EXE；当前版本为 `ZJ-HUB-v2026.09.30.15.exe`，源码包为 `source/ZJ-HUB-v2026.09.30.15-source.zip`。

## 游戏 ID 登录

首次启动先调用布吉岛 `POST /v2/player` 验证游戏 ID。验证成功后 ID 会保存到当前 Windows 账户，并同步到战局查询、最近查询和大厅聊天。设置 → 大厅身份可以重新验证和换绑。

## 大厅聊天服务

仓库根目录包含可部署到 Render 的公共大厅服务：

- `lobby_server.py`：公共消息、在线人数和会话接口；不提供私聊，免费实例只保留内存中的短消息窗口。
- `requirements-chat.txt`：Flask + Gunicorn。
- `render.yaml`：免费 Python Web Service，区域选择 Singapore（距离中国大陆较近的可用选项）。中国大陆网络可达性受运营商线路影响。

在 Render 选择 **New → Blueprint**，连接本仓库并应用 `render.yaml`。服务名为 `dev-king-lobby`，默认地址为 `https://dev-king-lobby.onrender.com`。服务创建后，客户端大厅聊天会自动使用该地址；也可在软件“设置 → 大厅身份”中输入并保存服务器地址。

## 自动更新

程序启动后会查询本仓库最新 Release。发现更高版本时，窗口顶部会显示“可更新”，点击后打开 GitHub 发布页。

## 主题与系统

支持明亮、玻璃、暗色和深海蓝主题；需要 Windows 10 或 Windows 11。四套主题共用统一圆角、透明层级和控件状态，按钮采用短时可中断的透明度反馈，保持界面清晰和交互流畅。






- v2026.09.30.15 删除液态玻璃主题及其动态背景层；保留明亮、玻璃、暗色、深海蓝四套主题，按钮交互采用 125ms 可中断反馈，减少重绘并提升流畅度。

