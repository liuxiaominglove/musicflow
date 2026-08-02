/**
 * MusicFlow - Preload Script
 * 安全地向渲染进程暴露有限的 API
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // 平台信息
  platform: process.platform,

  // 获取应用信息
  getAppInfo: () => ipcRenderer.invoke('get-app-info'),

  // 监听主进程事件
  onTogglePlay: (callback) => {
    ipcRenderer.on('toggle-play', () => callback());
  },

  // 窗口控制
  bringToFront: () => ipcRenderer.send('bring-to-front'),

  // 托盘歌曲信息更新
  updateTraySong: (name, artist) => ipcRenderer.send('update-tray-song', name, artist),

  // 是否是桌面应用环境
  isDesktop: true,
});
