/**
 * Electron - 智能音乐播放器桌面应用
 * Electron 主进程
 */

const { app, BrowserWindow, Tray, Menu, dialog, ipcMain, Notification } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const os = require('os');

// ============================================
// 配置
// ============================================
const FLASK_PORT = 18080;
const FLASK_HOST = '127.0.0.1';
const APP_URL = `http://${FLASK_HOST}:${FLASK_PORT}`;

// isPackaged: true = 打包后的 .app, false = 开发模式
const isPackaged = app.isPackaged;

// RESOURCES_DIR: 源代码（server.py, static/）所在目录
//   开发模式: electron/ 的父目录（项目根目录）
//   打包模式: 系统 resources 目录（extraResources 存放处，只读）
const RESOURCES_DIR = isPackaged
  ? process.resourcesPath
  : path.join(__dirname, '..');

// DATA_DIR: 可写数据目录（配置、音乐库、日志等）
//   打包模式: 优先用原项目目录，兜底用用户数据目录
function findDataDir() {
  if (!isPackaged) return path.join(__dirname, '..');
  
  // 检查常见的项目目录位置
  const candidates = [
    path.join(os.homedir(), 'CodeBuddy', '20260728020013', 'smart-music-player'),
  ];
  for (const dir of candidates) {
    if (fs.existsSync(dir)) return dir;
  }
  // 兜底：在 ~/MusicFlowData 创建数据目录
  const fallback = path.join(os.homedir(), 'MusicFlowData');
  if (!fs.existsSync(fallback)) fs.mkdirSync(fallback, { recursive: true });
  return fallback;
}

const DATA_DIR = findDataDir();

let mainWindow = null;
let flaskProcess = null;
let tray = null;
let isQuitting = false;
let traySongName = '';
let traySongArtist = '';
let trayContextMenu = null;

// 安全日志输出 —— 终端关闭后写 stdout 会抛 EIO
function safeLog(...args) {
  try { console.log(...args); } catch (_) { /* 终端已关闭，忽略 */ }
}
function safeError(...args) {
  try { console.error(...args); } catch (_) { /* 终端已关闭，忽略 */ }
}
safeLog(`[Electron] isPackaged=${isPackaged}, RESOURCES=${RESOURCES_DIR}, DATA=${DATA_DIR}`);

// ============================================
// Flask 服务器管理
// ============================================
function startFlaskServer() {
  return new Promise((resolve, reject) => {
    // 源代码脚本从 RESOURCES_DIR 找
    const serverScript = path.join(RESOURCES_DIR, 'server.py');

    // Python 解释器查找：优先使用项目目录的 venv，兜底用系统 python3
    let finalPython;
    const venvPython = process.platform === 'win32'
      ? path.join(DATA_DIR, 'venv', 'Scripts', 'python.exe')
      : path.join(DATA_DIR, 'venv', 'bin', 'python3');
    if (fs.existsSync(venvPython)) {
      finalPython = venvPython;
    } else {
      finalPython = process.platform === 'win32' ? 'python' : 'python3';
    }

    safeLog(`[Electron] 启动 Flask 服务器，Python: ${finalPython}`);
    safeLog(`[Electron] 脚本: ${serverScript}`);
    safeLog(`[Electron] 端口: ${FLASK_PORT}`);

    // 传递 MUSICFLOW_HOME 让 server.py 知道可写数据目录在哪
    const env = {
      ...process.env,
      FLASK_PORT: String(FLASK_PORT),
      MUSICFLOW_HOME: DATA_DIR,
      PYTHONUNBUFFERED: '1',
    };

    flaskProcess = spawn(finalPython, [serverScript, String(FLASK_PORT)], {
      cwd: RESOURCES_DIR,
      env: env,
      stdio: ['pipe', 'pipe', 'pipe'],
      detached: false,
    });

    let started = false;
    let lastError = '';
    let closeHandled = false;

    flaskProcess.stdout.on('data', (data) => {
      const msg = data.toString();
      safeLog(`[Flask] ${msg.trim()}`);

      if (!started && (msg.includes('Running on') || msg.includes('Debugger is active'))) {
        started = true;
        setTimeout(() => resolve(), 500);
      }
    });

    flaskProcess.stderr.on('data', (data) => {
      const msg = data.toString();
      safeLog(`[Flask:err] ${msg.trim()}`);
      if (msg.trim()) lastError = msg.trim();
      if (!started && msg.includes('Running on')) {
        started = true;
        setTimeout(() => resolve(), 500);
      }
    });

    flaskProcess.on('error', (err) => {
      safeError('[Electron] Flask 启动失败:', err);
      if (!closeHandled) { closeHandled = true; reject(err); }
    });

    flaskProcess.on('close', (code, signal) => {
      if (closeHandled) return;
      safeLog(`[Electron] Flask 进程退出，代码: ${code}, 信号: ${signal}`);

      // 启动阶段就退出 → 直接报错，不重启
      if (!started) {
        closeHandled = true;
        const errMsg = lastError
          ? `Flask 启动失败：${lastError.substring(0, 300)}`
          : `Flask 进程意外退出 (code=${code})`;
        safeError(errMsg);
        reject(new Error(errMsg));
        return;
      }

      // 被信号杀死 → 应用退出
      if (signal !== null) {
        safeLog('[Electron] Flask 被信号终止，应用将退出');
        if (!isQuitting) { isQuitting = true; app.quit(); }
        return;
      }

      // 运行中意外退出 → 重启
      if (!isQuitting) {
        safeLog('[Electron] Flask 意外退出，10秒后重启...');
        setTimeout(() => {
          if (!isQuitting) startFlaskServer().catch(safeError);
        }, 10000);
      }
    });

    // 超时保护
    setTimeout(() => {
      if (!started && !closeHandled) {
        closeHandled = true;
        safeError('[Electron] Flask 启动超时');
        reject(new Error('Flask 服务器连接超时'));
      }
    }, 60000);
  });
}

function stopFlaskServer() {
  if (flaskProcess) {
    safeLog('[Electron] 停止 Flask 服务器...');
    // 先移除 close 监听器，避免触发重启逻辑
    flaskProcess.removeAllListeners('close');
    if (process.platform === 'win32') {
      spawn('taskkill', ['/pid', flaskProcess.pid, '/f', '/t']);
    } else {
      try { flaskProcess.kill('SIGTERM'); } catch (_) {}
    }
    flaskProcess = null;
  }
}

function waitForFlask(maxAttempts = 30) {
  return new Promise((resolve, reject) => {
    let attempts = 0;
    const check = () => {
      attempts++;
      const req = http.get(APP_URL + '/api/status', (res) => {
        resolve(true);
      });
      req.on('error', () => {
        if (attempts < maxAttempts) {
          setTimeout(check, 500);
        } else {
          reject(new Error('Flask 服务器连接超时'));
        }
      });
      req.end();
    };
    check();
  });
}

// ============================================
// 窗口管理
// ============================================
function createWindow() {
  const { screen } = require('electron');
  const { width, height } = screen.getPrimaryDisplay().workAreaSize;

  mainWindow = new BrowserWindow({
    width: Math.min(1280, width),
    height: Math.min(820, height),
    minWidth: 880,
    minHeight: 600,
    title: 'MusicFlow',
    icon: process.platform === 'darwin'
      ? path.join(RESOURCES_DIR, 'assets', 'icon.icns')
      : path.join(RESOURCES_DIR, 'assets', 'icon.png'),
    backgroundColor: '#121212',
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    trafficLightPosition: { x: 16, y: 20 },
  });

  mainWindow.loadURL(APP_URL);

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  mainWindow.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault();
      mainWindow.hide(); // 关闭到托盘
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // 设置窗口标题
  mainWindow.webContents.on('page-title-updated', (e) => {
    e.preventDefault();
  });
}

// ============================================
// 系统托盘
// ============================================
function createTray() {
  // 使用简单的内置图标
  if (process.platform === 'darwin') {
    // macOS 使用 template image
    const { nativeImage } = require('electron');
    const iconPath = path.join(RESOURCES_DIR, 'assets', 'tray-icon.png');

    let trayIcon;
    if (fs.existsSync(iconPath)) {
      trayIcon = nativeImage.createFromPath(iconPath);
      if (process.platform === 'darwin') {
        trayIcon = trayIcon.resize({ width: 16, height: 16 });
        trayIcon.setTemplateImage(true);
      }
    } else {
      // 创建一个简单的 16x16 图标
      trayIcon = nativeImage.createEmpty();
    }

    tray = new Tray(trayIcon);
  } else {
    const iconPath = path.join(RESOURCES_DIR, 'assets', 'tray-icon.png');
    if (fs.existsSync(iconPath)) {
      tray = new Tray(iconPath);
    } else {
      return;
    }
  }

function buildTrayMenu() {
  const songInfo = traySongName ? `${traySongName}${traySongArtist ? ' - ' + traySongArtist : ''}` : '未在播放';
  const menuItems = [
    { label: '显示 MusicFlow', click: () => { mainWindow.show(); mainWindow.focus(); } },
    { type: 'separator' },
    { label: songInfo, enabled: false },
    { type: 'separator' },
    { label: '⏯ 播放/暂停', click: () => { mainWindow.webContents.send('toggle-play'); } },
    { label: '⏹ 停止播放', click: () => {
      mainWindow.webContents.send('toggle-play');  // sends toggle-play, frontend handles stop
      // Also call stop via HTTP
      const http = require('http');
      const req = http.request({ hostname: '127.0.0.1', port: 18080, path: '/api/stop', method: 'POST' }, () => {});
      req.on('error', () => {});
      req.end();
    }},
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        isQuitting = true;
        app.quit();
      }
    },
  ];
  return Menu.buildFromTemplate(menuItems);
}

  trayContextMenu = buildTrayMenu();
  tray.setToolTip('MusicFlow - 智能音乐播放器');
  tray.setContextMenu(trayContextMenu);

  tray.on('double-click', () => {
    mainWindow.show();
    mainWindow.focus();
  });
}

// ============================================
// IPC 通信
// ============================================
ipcMain.handle('get-app-info', () => {
  return {
    version: app.getVersion(),
    platform: process.platform,
    isPackaged: app.isPackaged,
  };
});

// 将窗口带到前台（定时播放触发时调用）
ipcMain.on('bring-to-front', () => {
  if (!mainWindow) return;
  mainWindow.show();
  mainWindow.focus();
  // macOS: 让窗口置顶一瞬再取消，确保可见
  if (process.platform === 'darwin') {
    mainWindow.setAlwaysOnTop(true);
    setTimeout(() => mainWindow.setAlwaysOnTop(false), 500);
  }
  // 发送系统通知（定时播放的歌曲信息）
  const songLabel = traySongName ? `🎵 正在播放: ${traySongName}` : '🎵 定时音乐已开始播放';
  try {
    const notification = new Notification({
      title: 'MusicFlow 定时播放',
      body: songLabel,
      silent: false,
    });
    notification.on('click', () => {
      mainWindow.show();
      mainWindow.focus();
    });
    notification.show();
  } catch (_) {}
});

// 托盘显示当前播放歌曲
ipcMain.on('update-tray-song', (_event, name, artist) => {
  traySongName = name || '';
  traySongArtist = artist || '';

  const tooltip = traySongName
    ? `🎵 ${traySongName}${traySongArtist ? ' - ' + traySongArtist : ''}`
    : 'MusicFlow - 智能音乐播放器';

  if (tray) {
    tray.setToolTip(tooltip);
    trayContextMenu = buildTrayMenu();
    tray.setContextMenu(trayContextMenu);
  }
});

// ============================================
// 全局错误捕获 —— 终端关闭后不再崩溃
// ============================================
process.on('uncaughtException', (err) => {
  if (err.code === 'EIO' || err.code === 'EPIPE' || err.code === 'ERR_STREAM_DESTROYED') {
    // 终端已关闭，静默处理
    return;
  }
  safeError('[Electron] 未捕获异常:', err);
  // 不要 rethrow，让应用继续运行
});

// ============================================
// 应用生命周期
// ============================================
app.whenReady().then(async () => {
  safeLog('[Electron] 应用启动中...');

  try {
    // 启动 Flask 服务器
    await startFlaskServer();
    safeLog('[Electron] Flask 服务器启动完成');

    // 等待 Flask 就绪
    await waitForFlask();
    safeLog('[Electron] Flask 服务器连接就绪');

    // 创建窗口
    createWindow();
    safeLog('[Electron] 窗口创建完成');

    // 创建托盘
    createTray();

  } catch (err) {
    safeError('[Electron] 启动失败:', err);
    dialog.showErrorBox('启动错误', `无法启动播放器服务:\n${err.message}`);
  }
});

app.on('window-all-closed', () => {
  // 不退出，保持托盘运行
});

app.on('activate', () => {
  if (mainWindow === null) {
    createWindow();
  } else {
    mainWindow.show();
    mainWindow.focus();
  }
});

app.on('before-quit', () => {
  isQuitting = true;
  stopFlaskServer();
});
