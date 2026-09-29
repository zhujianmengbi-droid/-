# ZJ HUB · DEV King

面向起床战争的 Windows 工具中心，包含电脑清理、快捷工具、战局数据、排行榜和公共大厅聊天。

## 下载

前往 [GitHub Releases](https://github.com/zhujianmengbi-droid/-/releases) 获取 Windows EXE；当前版本为 `ZJ-HUB-v2026.09.30.13.exe`，源码包为 `source/ZJ-HUB-v2026.09.30.13-source.zip`。

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

支持明亮、玻璃、液态玻璃、暗色和深海蓝主题；需要 Windows 10 或 Windows 11。液态玻璃主题参考 [liquid-glass-webgl](https://github.com/martin65536/liquid-glass-webgl) 的中性灰白玻璃面、白色高光边缘、蓝色点缀和动态光场思路，在 Qt 中保持背板透明，由 Windows DWM 提供模糊玻璃，只绘制动态光场、折射线条和移动高光，保证窗口交互流畅。






- v2026.09.30.13 移除液态主题静态背板底色：背板保持透明，使用 Windows DWM 模糊玻璃承接桌面，只保留低饱和动态光场、折射线条和白色高光。

