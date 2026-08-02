# MusicFlow - 智能音乐播放器

基于 Python Flask + Electron 的桌面音乐播放器，支持 YouTube 在线搜索/下载/播放，本地音乐管理。

## 技术栈

- **后端**：Python 3 + Flask（`server.py` + `scheduler.py`）
- **前端**：原生 HTML/JS/CSS（`static/`）
- **桌面壳**：Electron（`main.js` + `preload.js`）
- **视频/音频**：VLC（播放器）、yt-dlp（YouTube 下载）
- **端口**：`18080`

## 目录结构

```
musicflow/
├── server.py          # Flask 后端：搜索、下载、播放 API
├── scheduler.py       # 定时任务调度器
├── main.js            # Electron 主进程
├── preload.js         # Electron 预加载脚本
├── package.json       # Node 项目配置
├── config.yaml        # 应用配置
├── current.yaml.template  # 当前播放配置模板
├── static/            # 前端页面
│   ├── index.html     # 主界面
│   ├── login.html     # 登录页
│   ├── mobile.html    # 移动端
│   ├── sw.js          # Service Worker
│   └── manifest.json  # PWA 配置
├── songs.json         # 已下载歌曲名称清单
└── .gitignore
```

## 运行方式

### 开发模式（直接启动 Flask）

```bash
pip install flask yt-dlp python-vlc PyYAML
python3 server.py 18080
```

访问：`http://localhost:18080`

### 打包模式（Electron App）

```bash
# 安装 Electron
npm install electron

# 启动
npx electron .

# 打包 macOS .app
npx electron-builder --mac
```

## 功能

- 🔍 YouTube 在线搜索音乐
- ⬇️ yt-dlp 下载（需配置代理）
- ▶️ VLC 嵌入播放（音量 / 进度 / 播放列表）
- 📱 移动端浏览器远程控制（手机同 Wi-Fi）
- 🔐 访问密码保护
- ⏰ 定时播放调度
