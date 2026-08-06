#!/usr/bin/env python3
"""
MusicFlow — Web 服务端
提供搜索、下载、播放、评分等 API
"""

import os
import sys
import re
import json
import time
import secrets
import hashlib
import socket
import threading
import subprocess
import functools
from pathlib import Path
from datetime import datetime

# ── 修复 macOS Electron 子进程 PATH 缺少 Homebrew 目录的问题 ──
# Electron 启动的 Python 子进程 PATH 可能只有 /usr/bin:/bin:/usr/sbin:/sbin，
# 导致 subprocess 找不到 yt-dlp（FileNotFoundError 被静默吞掉，搜索永远 0 条）。
for _bin in ("/usr/local/bin", "/opt/homebrew/bin"):
    if os.path.isdir(_bin) and _bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")


def _log_debug(msg):
    """写调试日志到 /tmp/mf_search.log（仅排查用，不影响功能）"""
    try:
        with open("/tmp/mf_search.log", "a") as _f:
            _f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass

try:
    from pypinyin import lazy_pinyin, Style
    _HAS_PYPINYIN = True
except ImportError:
    _HAS_PYPINYIN = False

# ── Upnet VPN 代理配置 ──
_UPNET_HTTP_PORT = "29758"


def _get_proxy():
    """智能代理检测：Upnet 在线时走代理，否则直连（国内源不需要代理）。"""
    try:
        s = socket.create_connection(('127.0.0.1', int(_UPNET_HTTP_PORT)), timeout=1.5)
        s.close()
        return (["--proxy", f"http://127.0.0.1:{_UPNET_HTTP_PORT}"], {
            **os.environ,
            "http_proxy": f"http://127.0.0.1:{_UPNET_HTTP_PORT}",
            "https_proxy": f"http://127.0.0.1:{_UPNET_HTTP_PORT}",
            "no_proxy": "localhost,127.0.0.1,.local",
        })
    except Exception:
        return ([], os.environ)


def _to_pinyin(text):
    """将中文文本转为拼音（小写无空格），非中文原样保留"""
    if not text:
        return ""
    if not _HAS_PYPINYIN:
        return text.lower().replace(" ", "")
    return "".join(lazy_pinyin(text, style=Style.NORMAL)).lower().replace(" ", "")


def _parse_artist_from_title(title):
    """从视频标题中智能提取歌手名。
    支持格式：歌手 - 歌名 | 【歌手】歌名 | 歌手《歌名》 | 歌手 | 歌名 等
    返回 (artist, clean_title) 元组，解析失败则 artist 为空字符串。
    """
    if not title:
        return "", title

    t = title.strip()

    # 按优先级依次尝试各种分隔格式（左侧 = 歌手，右侧 = 歌名）
    patterns = [
        # 1. "歌手 - 歌名" 或 "歌手 – 歌名"（最常见的格式）
        (r'^(.+?)\s*[-–—]\s*(.+)$', True),
        # 2. "【歌手】歌名"（B站/中文常见）
        (r'^【(.+?)】\s*(.+)$', True),
        # 3. "歌手《歌名》"（中文音乐常见）
        (r'^(.+?)《(.+?)》\s*$', True),
        # 4. "歌手「歌名」"
        (r'^(.+?)「(.+?)」\s*$', True),
        # 5. "[歌手] 歌名"
        (r'^\[(.+?)\]\s*(.+)$', True),
        # 6. "歌手 | 歌名"
        (r'^(.+?)\s*\|\s*(.+)$', True),
    ]

    for pattern, left_is_artist in patterns:
        m = re.match(pattern, t)
        if m:
            if left_is_artist:
                artist = m.group(1).strip()
                song = m.group(2).strip()
            else:
                artist = m.group(2).strip()
                song = m.group(1).strip()
            # 歌手名不能太长（排除误匹配，比如一句话被当成歌手）
            if len(artist) <= 80:
                return artist, song

    return "", title

from flask import Flask, request, jsonify, send_from_directory, redirect, make_response

# 尝试导入 mutagen（MP3 ID3 标签读取）
try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False

# 尝试导入 VLC
try:
    import vlc as _vlc
    VLC_AVAILABLE = True
except ImportError:
    VLC_AVAILABLE = False
    print("⚠️  python-vlc 未安装，播放功能不可用")

app = Flask(__name__, static_folder="static", static_url_path="")

# ============================================
# 数据目录：打包模式下由 Electron 通过 MUSICFLOW_HOME 指定
# 开发模式下优先检测 ~/MusicFlowData，兜底用源码目录
# ============================================
def _resolve_data_dir():
    env = os.environ.get("MUSICFLOW_HOME")
    if env:
        return Path(env)
    # 自动检测 ~/MusicFlowData
    legacy = Path(os.path.expanduser("~/MusicFlowData"))
    if legacy.is_dir():
        return legacy
    return Path(__file__).parent

BASE_DIR = _resolve_data_dir()

# ============================================
# 安全配置 — 访问密码
# ============================================
# 生成随机密钥（首次启动自动生成，存到文件保持稳定）
SECRET_FILE = BASE_DIR / "data" / ".secret_key"
SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)

def _get_or_create_secret():
    """读取或创建密钥（长度 32 字符，足够安全）"""
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    key = secrets.token_urlsafe(24)  # 32 chars
    SECRET_FILE.write_text(key)
    SECRET_FILE.chmod(0o600)  # 仅文件拥有者可读写
    return key

ACCESS_TOKEN = _get_or_create_secret()

def _is_remote():
    """判断请求是否来自公网（Cloudflare Tunnel 代理）。
    远程请求 = 有 Cloudflare 代理头 或 非本地 IP 且非 127.0.0.1
    """
    return (
        bool(request.headers.get("Cf-Connecting-Ip")) or
        bool(request.headers.get("Cdn-Loop")) or
        bool(request.headers.get("X-Forwarded-For")) or
        request.remote_addr not in ("127.0.0.1", "::1")
    )


def _check_auth():
    """验证请求是否携带正确 token。
    
    支持两种方式：
    1. URL 参数: ?token=xxx
    2. Cookie: auth_token=xxx（登录后自动记住）
    
    真正的本地浏览器访问（127.0.0.1 且无代理头）直接放行。
    Cloudflare Tunnel 会设置 X-Forwarded-For，此时需要验证。
    """
    # 真正的本地直接访问 — 放行
    is_real_local = (
        request.remote_addr in ("127.0.0.1", "::1") and
        not request.headers.get("X-Forwarded-For") and
        not request.headers.get("Cf-Connecting-Ip") and
        not request.headers.get("Cdn-Loop")
    )
    if is_real_local:
        return True
    
    # 检查 URL 参数
    token = request.args.get("token", "")
    if token == ACCESS_TOKEN:
        return True
    
    # 检查 Cookie
    token = request.cookies.get("auth_token", "")
    if token == ACCESS_TOKEN:
        return True
    
    return False


def _check_admin():
    """验证请求是否来自管理员（本地直接访问）。
    远程用户（Cloudflare Tunnel）永远不是管理员，即使有密码。
    """
    return not _is_remote()


def require_auth(f):
    """装饰器：需要密码才能访问的 API（只读 + 管理都可用）"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not _check_auth():
            return jsonify({"ok": False, "error": "需要密码验证", "need_auth": True}), 401
        return f(*args, **kwargs)
    return wrapper


def require_admin(f):
    """装饰器：管理操作（删除、下载、关机等），仅本地访问可用。
    远程用户（手机端）即使有密码也不允许执行这些操作。"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not _check_auth():
            return jsonify({"ok": False, "error": "需要密码验证", "need_auth": True}), 401
        if not _check_admin():
            return jsonify({"ok": False, "error": "此操作仅限电脑端本地执行，手机端仅支持播放和浏览", "forbidden": True}), 403
        return f(*args, **kwargs)
    return wrapper


def require_auth_html(f):
    """装饰器：需要密码才能访问的 HTML 页面"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not _check_auth():
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper

# ============================================
# 配置
# ============================================
MUSIC_DIR = BASE_DIR / "music_library"
RATINGS_FILE = BASE_DIR / "data" / "ratings.json"
TAGS_ORDER_FILE = BASE_DIR / "data" / "tags_order.json"
MUSIC_DIR.mkdir(parents=True, exist_ok=True)
RATINGS_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load_tag_order() -> list:
    """读取用户自定义的标签排序（未保存过则返回空列表）"""
    try:
        if TAGS_ORDER_FILE.exists():
            data = json.loads(TAGS_ORDER_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [t for t in data if isinstance(t, str)]
    except Exception:
        pass
    return []


def _save_tag_order(order: list):
    """保存标签排序到配置文件"""
    TAGS_ORDER_FILE.parent.mkdir(parents=True, exist_ok=True)
    TAGS_ORDER_FILE.write_text(
        json.dumps([t for t in order if isinstance(t, str)], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _all_tags_with_order() -> list:
    """返回音乐库全部标签（含空标签），优先应用用户自定义顺序，未排序的标签追加在末尾"""
    dir_tags = [d.name for d in MUSIC_DIR.iterdir()
                if d.is_dir() and not d.name.startswith(".")]
    order = _load_tag_order()
    # 保留用户在 order 里的有效顺序（过滤掉已不存在的目录）
    ordered = [t for t in order if t in dir_tags]
    # 补充 order 里没有但实际存在的目录
    for t in dir_tags:
        if t not in ordered:
            ordered.append(t)
    return ordered

# ============================================
# VLC 播放器管理
# ============================================
class Player:
    def __init__(self):
        self._vlc_instance = None
        self._player = None
        self._current_file = None
        self._is_paused = False
        self._volume = 70
        self._loop_mode = "all"  # "off" / "all" / "single"
        self._on_song_end_callback = None
        self._song_ended_pending = False  # VLC 回调线程安全的标志位
        self._schedule_manually_paused = False  # 用户手动暂停了定时播放歌曲
        self._lock = threading.Lock()  # 线程锁保护 VLC 操作
        self._init_vlc()

    def _init_vlc(self):
        if not VLC_AVAILABLE:
            return
        try:
            self._vlc_instance = _vlc.Instance("--quiet")
            self._player = self._vlc_instance.media_player_new()
            event_manager = self._player.event_manager()
            event_manager.event_attach(
                _vlc.EventType.MediaPlayerEndReached,
                self._on_end_reached,
            )
        except Exception as e:
            print(f"VLC 初始化失败: {e}")

    def _on_end_reached(self, event):
        """歌曲播放完毕回调（运行在 VLC 内部线程，仅设置标志位，不做播放操作）"""
        self._song_ended_pending = True

    def _handle_song_end_if_needed(self):
        """在主线程中安全处理歌曲结束逻辑（由 status() 触发）"""
        if not self._song_ended_pending:
            return
        self._song_ended_pending = False

        old_file = self._current_file
        self._current_file = None

        if self._loop_mode == "single":
            if old_file and os.path.exists(old_file):
                time.sleep(0.3)
                self.play(old_file, volume=self._volume)
                # 单曲循环不需要走回调
                return

        if self._on_song_end_callback:
            try:
                self._on_song_end_callback(self._loop_mode)
            except Exception as e:
                print(f"歌曲结束回调出错: {e}")

    def play(self, filepath, volume=None):
        if not self._player:
            return {"ok": False, "error": "播放器未初始化"}
        with self._lock:
            try:
                if not os.path.exists(filepath):
                    return {"ok": False, "error": "文件不存在"}
                media = self._vlc_instance.media_new(filepath)
                self._player.set_media(media)
                if volume is not None:
                    self._player.audio_set_volume(int(volume))
                else:
                    self._player.audio_set_volume(self._volume)
                self._player.play()
                self._current_file = filepath
                self._is_paused = False
                return {"ok": True, "file": filepath}
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def pause_resume(self):
        global _schedule_song_active
        if not self._player:
            return {"ok": False, "error": "播放器未初始化"}
        with self._lock:
            try:
                self._player.pause()
                self._is_paused = not self._is_paused
                # 如果当前是定时播放歌曲，记录用户手动暂停状态
                if _schedule_song_active:
                    self._schedule_manually_paused = self._is_paused
                    if self._is_paused:
                        print("[定时播放] 用户手动暂停，阻止自动恢复")
                    else:
                        print("[定时播放] 用户手动恢复播放")
                return {"ok": True, "paused": self._is_paused}
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def stop(self):
        """停止播放"""
        if not self._player:
            return {"ok": False, "error": "播放器未初始化"}
        with self._lock:
            try:
                self._player.stop()
                self._current_file = None
                self._is_paused = False
                self._song_ended_pending = False
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def force_stop(self):
        """强制停止播放——仅停止当前媒体，不重建 VLC 实例（避免线程崩溃 segfault）"""
        global _schedule_song_active
        if not self._player:
            return {"ok": False, "error": "播放器未初始化"}
        with self._lock:
            try:
                self._player.stop()
            except:
                pass
            # 释放当前媒体（不重建 VLC 实例，防止多线程 segfault）
            try:
                self._player.set_media(None)
            except:
                pass
            self._current_file = None
            self._is_paused = False
            self._song_ended_pending = False
            self._schedule_manually_paused = False  # 强制停止时清除手动暂停标记
            _schedule_song_active = False  # 清除定时播放活跃标记
            return {"ok": True}

    def set_volume(self, volume):
        self._volume = int(volume)
        if self._player:
            with self._lock:
                self._player.audio_set_volume(self._volume)
        return {"ok": True, "volume": self._volume}

    def set_loop_mode(self, mode):
        """设置循环模式: off / all / single"""
        if mode in ("off", "all", "single"):
            self._loop_mode = mode
            return {"ok": True, "loop_mode": mode}
        return {"ok": False, "error": f"无效模式: {mode}"}

    def get_loop_mode(self):
        return self._loop_mode

    def set_on_end_callback(self, cb):
        """设置歌曲结束回调函数"""
        self._on_song_end_callback = cb

    def seek(self, position):
        """跳转到指定位置（秒）"""
        if not self._player:
            return {"ok": False, "error": "播放器未初始化"}
        with self._lock:
            try:
                # VLC set_time 单位是毫秒
                self._player.set_time(int(position * 1000))
                return {"ok": True, "position": position}
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def status(self):
        # 在主线程安全处理歌曲结束 → 切下一首
        self._handle_song_end_if_needed()

        is_playing = False
        position = 0
        duration = 0
        if self._player:
            with self._lock:
                is_playing = self._player.is_playing()
                position = self._player.get_time() / 1000  # 秒
                duration = self._player.get_length() / 1000  # 秒
        # 暂停时 VLC is_playing() 可能返回 False，用 _is_paused 修正
        # active = 有媒体在播放或暂停中（区别于完全停止）
        active = is_playing or (self._is_paused and self._current_file is not None)
        s = {
            "playing": is_playing,
            "paused": self._is_paused,
            "active": active,  # 是否处于活跃状态（播放或暂停）
            "current_file": self._current_file,
            "position": round(position, 1),
            "duration": round(duration, 1),
            "volume": self._volume,
            "loop_mode": self._loop_mode,
            "schedule_active": _schedule_song_active,  # 是否为定时播放歌曲
            "schedule_paused": getattr(self, '_schedule_manually_paused', False),
        }
        # 解析当前文件名
        if self._current_file:
            stem = Path(self._current_file).stem
            song_name, artist = parse_song_name(stem)
            s["current_name"] = song_name
            s["current_artist"] = artist
        return s


player = Player()

# 播放列表状态（用于循环播放）
playlist_songs = []  # list of {rel_path, path, ...}
playlist_index = -1

def _on_song_end(loop_mode):
    """歌曲结束回调：处理列表循环"""
    global playlist_songs, playlist_index
    if loop_mode == "all" and playlist_songs and playlist_index >= 0:
        # 播放下一个
        next_idx = playlist_index + 1
        if next_idx >= len(playlist_songs):
            next_idx = 0  # 列表循环回到第一首
        playlist_index = next_idx
        song = playlist_songs[next_idx]
        abs_path = song.get("path") or str(MUSIC_DIR / song["rel_path"])
        time.sleep(0.3)
        player.play(abs_path)
    elif loop_mode == "off" and playlist_songs and playlist_index >= 0:
        playlist_index = -1  # 停止

player.set_on_end_callback(_on_song_end)

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ============================================
# 评分管理
# ============================================
def load_ratings() -> dict:
    if RATINGS_FILE.exists():
        try:
            return json.loads(RATINGS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            return {}
    return {}

def save_ratings(ratings: dict):
    RATINGS_FILE.write_text(
        json.dumps(ratings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

# ============================================
# 自定义歌曲元数据（用户手动修正的歌名/歌手）
# ============================================
SONG_META_FILE = DATA_DIR / "song_meta.json"

def load_song_meta() -> dict:
    """加载用户手动修正的歌名/歌手配置。
    格式: { "rel_path": { "name": "...", "artist": "..." } }
    """
    if SONG_META_FILE.exists():
        try:
            return json.loads(SONG_META_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            return {}
    return {}

def save_song_meta(meta: dict):
    SONG_META_FILE.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

# ============================================
# 定时播放调度管理
# ============================================
SCHEDULE_FILE = DATA_DIR / "schedule.json"

def load_schedules() -> dict:
    """加载定时播放配置"""
    if SCHEDULE_FILE.exists():
        try:
            return json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            return {"items": []}
    return {"items": []}


def save_schedules(data: dict):
    """保存定时播放配置"""
    SCHEDULE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# 记录每个 schedule 最后触发的时间，避免同一分钟重复触发
schedule_last_triggered = {}
schedule_thread = None
schedule_check_running = False
_schedule_song_active = False  # 标记：当前是否为定时播放触发的歌曲（含暂停状态）
_restore_stop_event = threading.Event()  # 通知旧的 _restore_loop 线程停止


def schedule_checker_loop():
    """后台线程：每 30 秒检查一次定时播放"""
    global schedule_check_running, _schedule_song_active
    while schedule_check_running:
        try:
            now = datetime.now()
            current_time = now.strftime("%H:%M")
            schedules = load_schedules()

            for item in schedules.get("items", []):
                if not item.get("enabled", True):
                    continue
                if item.get("time", "") != current_time:
                    continue

                sid = item.get("id", "")
                # 检查是否已经在当前分钟触发过
                last = schedule_last_triggered.get(sid, "")
                if last == current_time:
                    continue

                # 触发播放
                song_path = item.get("song_path", "")
                if song_path:
                    full_path = MUSIC_DIR / song_path
                    if full_path.exists():
                        vol = item.get("volume", 70)
                        print(f"[定时播放] {current_time} - 播放: {item.get('song_name', song_path)}")
                        try:
                            # 保存当前循环模式
                            prev_loop = player.get_loop_mode()
                            # 强制停止当前正在播放的音乐
                            player.force_stop()
                            time.sleep(0.2)  # 等待 VLC 完全停止
                            # 设为不循环（定时任务只播一遍）
                            player.set_loop_mode("off")
                            player.play(str(full_path), volume=vol)
                            _schedule_song_active = True
                            schedule_last_triggered[sid] = current_time
                        except Exception as e:
                            print(f"[定时播放] VLC 操作异常: {e}")
                            continue
                        # 通知旧的恢复线程停止，再启动新的
                        _restore_stop_event.set()
                        time.sleep(0.2)
                        _restore_stop_event.clear()

                        def _restore_loop():
                            global _schedule_song_active
                            while not _restore_stop_event.is_set():
                                time.sleep(2)
                                try:
                                    st = player.status()
                                    if not st.get("active") and not st.get("paused"):
                                        player.set_loop_mode(prev_loop)
                                        _schedule_song_active = False
                                        print(f"[定时播放] 歌曲已结束，恢复循环模式: {prev_loop}")
                                        break
                                except Exception as e:
                                    print(f"[定时播放] 恢复循环检查异常: {e}")
                        threading.Thread(target=_restore_loop, daemon=True).start()
                    else:
                        print(f"[定时播放] {current_time} - 文件不存在: {full_path}")

        except Exception as e:
            print(f"[定时播放] 检查出错: {e}")

        # 每 30 秒检查一次
        for _ in range(30):
            if not schedule_check_running:
                break
            time.sleep(1)


def start_schedule_checker():
    """启动定时播放检查线程"""
    global schedule_thread, schedule_check_running
    if schedule_check_running:
        return
    schedule_check_running = True
    schedule_thread = threading.Thread(target=schedule_checker_loop, daemon=True)
    schedule_thread.start()
    print("[定时播放] 调度器已启动，每 30 秒检查一次")


def stop_schedule_checker():
    """停止定时播放检查线程"""
    global schedule_check_running
    schedule_check_running = False


# ============================================
# 音乐库管理
# ============================================
def get_id3_metadata(filepath):
    """尝试从 MP3 文件的 ID3 标签中读取歌名和歌手。
    返回 (title, artist) 元组，失败返回 (None, None)。
    ID3 标签来自音乐平台下载/购买时的原始数据，比文件名更可信。
    """
    if not MUTAGEN_AVAILABLE:
        return None, None
    try:
        audio = MP3(filepath)
        if not audio.tags:
            return None, None
        title_tag = audio.tags.get('TIT2')   # 歌名
        artist_tag = audio.tags.get('TPE1')  # 歌手
        title = str(title_tag).strip() if title_tag else None
        artist = str(artist_tag).strip() if artist_tag else None
        if title and len(title) > 1:
            return title, artist or ''
        return None, None
    except Exception:
        return None, None


def parse_song_name(filename_stem):
    """
    从文件名解析歌曲名和歌手。
    支持格式:
      - "周杰伦 - 晴天" → name="晴天", artist="周杰伦"
      - "Adele - Someone Like You" → name="Someone Like You", artist="Adele"
      - "Adele_-_Someone_Like_You" → name="Someone Like You", artist="Adele" (yt-dlp格式)
      - "简单歌名" → name="简单歌名", artist=""
    """
    # 先尝试 " - " 分隔符（中英文短横线）
    m = re.match(r'^(.+?)\s*[-–—]\s*(.+)$', filename_stem)
    if m:
        artist = _clean_artist(m.group(1))
        song_name = m.group(2).strip()
        if len(artist) <= 50 and not artist.isdigit():
            song_name = _clean_song_title(song_name)
            if _is_meaningful_title(song_name):
                return song_name, artist

    # 再尝试 yt-dlp 的 "_-_" 格式（下划线包裹的短横）
    m = re.match(r'^(.+?)_[-–—]+_(.+)$', filename_stem)
    if m:
        artist = _clean_artist(m.group(1))
        song_name = m.group(2).strip()
        if len(artist) <= 50 and len(artist) >= 2 and not artist.isdigit():
            song_name = _clean_song_title(song_name)
            if _is_meaningful_title(song_name):
                return song_name, artist

    # 没有分隔符，整体作为歌名（也做清洗）
    clean_name = filename_stem.replace('_', ' ').strip()
    clean_name = _clean_song_title(clean_name)
    return clean_name, ''


def _is_meaningful_title(title):
    """检查歌名是否有实际含义（非纯平台标记/单字符/空）"""
    if not title:
        return False
    junk_keywords = {
        'mv', 'dj', 'hd', 'hq', '4k', 'ktv', '1080p', 'lyrics',
        'official', 'video', 'audio', 'music', 'live', 'remix', 'lrc',
        '1080p ktv', 'mv hd', 'dj mv', '4k mv', 'hd mv',
    }
    if len(title) <= 1 or title.lower().strip(' .,-_[]()') in junk_keywords:
        return False
    return True


def _clean_song_title(title):
    """清理歌名中的无关节信息"""
    original = title
    # 去掉括号内的内容 (Official, MV, HD 等)
    title = re.sub(r'\s*[\(（\[].*?[\)）\]]\s*$', '', title)
    # 去掉后缀扩展名 (xxx.mp3, xxx.flv 等）
    title = re.sub(r'\s*\.[a-z0-9]{2,4}$', '', title, flags=re.IGNORECASE)
    # 去掉常见视频/下载平台标记
    title = re.sub(r'\s*(Official|MV|Music Video|Video|Lyric|Audio|HD|4K|HQ|'
                   r'Remastered|Live|1080p|LIVE|OFFICIAL|VIDEO|KTV|M\/V).*$',
                   '', title, flags=re.IGNORECASE)
    # 去掉前导音质标记 "128kbps", "320kbps" 等
    title = re.sub(r'^\s*\d{2,4}\s*kbps\s*', '', title, flags=re.IGNORECASE)
    # 去掉后置音质标记
    title = re.sub(r'\s*[\[\(]?\d{2,4}\s*kbps[\]\)]?\s*$', '', title, flags=re.IGNORECASE)
    # 去掉 y2mate / 网址前缀
    title = re.sub(r'^.*?\.(com|net|org|cc)\s*[-–—]?\s*', '', title, flags=re.IGNORECASE)
    # 去掉前导数字编号 "01 - ", "01.", "01_", "01 "
    title = re.sub(r'^\d{1,3}[\.\s_\-–—]+\s*', '', title)
    # 下划线、连字符、逗号转空格
    for ch in ('_', '-', ','):
        title = title.replace(ch, ' ')
    # 合并多余空格，清理首尾标点
    title = re.sub(r'\s+', ' ', title).strip().strip(',.').strip()
    # 如果全部洗掉了，回退到原始名称（至少用户能看到信息）
    if not title:
        title = original.replace('_', ' ').strip()
    return title


def _clean_artist(artist):
    """清理歌手名"""
    artist = artist.strip().strip('_').strip('-').strip()
    # 去掉 URL
    artist = re.sub(r'https?://\S+', '', artist)
    # 去掉域名形式的来源 (y2mate.com, www.xxx.com 等)
    artist = re.sub(r'\b[\w-]+\.(com|net|org|cc|io|tv)\b', '', artist, flags=re.IGNORECASE)
    # 去掉平台后缀 (VEVO, Official, Topic 等)
    artist = re.sub(r'\s*[\(（\[].*?(VEVO|Official|Music|Topic|Channel).*?[\)）\]]',
                    '', artist, flags=re.IGNORECASE)
    artist = artist.replace('_', ' ')
    artist = re.sub(r'\s+', ' ', artist).strip()
    return artist


def _is_name_confident(name, artist, name_source):
    """判断歌名是否可信（无需人工审核）。
    可信条件（满足任一即可）：
      1. ID3 标签来源 → 完全可信
      2. 包含中文字符 → 中文歌名几乎总是正确的
      3. 有歌手+歌名格式（artist 非空）且歌名有意义 → 可信
      4. 英文歌名 4+ 个单词 → 很可能是正确标题（如电影配乐名）
    """
    if name_source in ('id3', 'custom'):
        return True
    if not name:
        return False

    # 中文歌名 → 可信
    if any('\u4e00' <= c <= '\u9fff' for c in name):
        return True

    # 有歌手信息且歌名有意义 → 可信
    if artist and len(name) > 2:
        return True

    # 英文多单词标题（4+ 词）且非纯数字 → 可信
    words = name.strip().split()
    if len(words) >= 4 and not all(w.replace('-', '').replace("'", '').isnumeric() for w in words):
        return True

    return False


def get_library():
    """获取本地音乐库列表"""
    songs = []
    ratings = load_ratings()
    custom_meta = load_song_meta()

    for song_dir in MUSIC_DIR.iterdir():
        if not song_dir.is_dir() or song_dir.name.startswith("."):
            continue
        for f in song_dir.glob("*.mp3"):
            # 排除 macOS 资源分支文件 (._*)
            if f.name.startswith("._"):
                continue
            rel_path = str(f.relative_to(MUSIC_DIR))

            # 检查是否有用户手动修正的元数据
            custom = custom_meta.get(rel_path, {})

            # 优先尝试 ID3 标签（可信来源）
            id3_name, id3_artist = get_id3_metadata(str(f))
            if id3_name:
                song_name = id3_name
                artist = id3_artist or ''
                name_source = 'id3'
            elif custom.get('name'):
                # 用户手动修正优先于文件名解析
                song_name = custom['name']
                artist = custom.get('artist', '')
                name_source = 'custom'
            else:
                # 回退到文件名解析
                song_name, artist = parse_song_name(f.stem)
                name_source = 'filename'

            songs.append({
                "id": rel_path,
                "name": song_name,
                "artist": artist,
                "name_pinyin": _to_pinyin(song_name),
                "artist_pinyin": _to_pinyin(artist or ""),
                "filename": f.name,
                "path": str(f),
                "rel_path": rel_path,
                "tag": song_dir.name,
                "size": f.stat().st_size,
                "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
                "rating": ratings.get(rel_path, 0),
                "name_source": name_source,
                "name_confident": _is_name_confident(song_name, artist, name_source),
                "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })

    # 按修改时间降序（最新添加的在最前面）
    songs.sort(key=lambda s: s.get("mtime", ""), reverse=True)

    return songs

# ============================================
# API 路由
# ============================================

@app.route("/login")
def login_page():
    """登录页面"""
    return send_from_directory("static", "login.html")


@app.route("/api/auth", methods=["POST"])
def api_auth():
    """验证密码，设置 Cookie"""
    data = request.get_json() or {}
    token = data.get("token", "")
    if token == ACCESS_TOKEN:
        resp = make_response(jsonify({"ok": True}))
        # Cookie 有效期 365 天
        resp.set_cookie("auth_token", ACCESS_TOKEN, max_age=365*24*3600, httponly=True, samesite="Lax")
        return resp
    return jsonify({"ok": False, "error": "密码错误"}), 403


@app.route("/")
@require_auth_html
def index():
    """首页，移动端自动跳转到移动页面"""
    ua = request.headers.get("User-Agent", "").lower()
    is_mobile = any(k in ua for k in ["iphone", "ipad", "android", "mobile", "webos"])
    if is_mobile:
        return send_from_directory("static", "mobile.html")
    return send_from_directory("static", "index.html")


@app.route("/mobile")
@require_auth_html
def mobile():
    """移动端独立播放器页面"""
    return send_from_directory("static", "mobile.html")

@app.route("/api/library")
@require_auth
def api_library():
    """获取本地音乐库"""
    songs = get_library()
    tag = request.args.get("tag", "")
    rating = request.args.get("rating", "")

    if tag:
        songs = [s for s in songs if s["tag"] == tag]
    if rating:
        try:
            r = int(rating)
            songs = [s for s in songs if s["rating"] == r]
        except ValueError:
            pass

    # 收集标签：遍历音乐库所有子目录（含空标签），应用用户自定义排序
    tags = _all_tags_with_order()

    return jsonify({
        "ok": True,
        "songs": songs,
        "tags": sorted(tags),
        "total": len(songs),
    })

@app.route("/api/search")
@require_auth
def api_search():
    """搜索歌曲（在线）"""
    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify({"ok": False, "error": "请输入搜索关键词"})

    max_results = int(request.args.get("max", 5))
    results = []

    try:
        proxy_args, proxy_env = _get_proxy()
        cmd = [
            "yt-dlp",
            f"ytsearch{max_results}:{keyword}",
            "--dump-json",
            "--no-playlist",
            "--no-warnings",
        ] + proxy_args
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=proxy_env)

        if result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                try:
                    info = json.loads(line)
                    results.append({
                        "id": info.get("id", ""),
                        "title": info.get("title", ""),
                        "url": info.get("webpage_url", ""),
                        "duration": info.get("duration", 0),
                        "duration_str": f"{info.get('duration', 0)//60}:{info.get('duration', 0)%60:02d}",
                        "uploader": info.get("uploader", ""),
                        "thumbnail": info.get("thumbnail", ""),
                    })
                except json.JSONDecodeError:
                    pass
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "搜索超时，请重试"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    return jsonify({"ok": True, "results": results, "keyword": keyword})


@app.route("/api/search-all")
@require_auth
def api_search_all():
    """统一搜索：先搜本地音乐库，再搜在线，返回合并结果"""
    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify({"ok": False, "error": "请输入搜索关键词"})

    max_online = int(request.args.get("max", 5))
    source = request.args.get("source", "youtube")  # youtube 或 bilibili

    # ---- 第一步：搜索本地音乐库 ----
    local_results = []
    keyword_lower = keyword.lower()
    all_songs = get_library()

    for song in all_songs:
        name_lower = song["name"].lower()
        name_pinyin = song.get("name_pinyin", "")
        artist_lower = (song.get("artist") or "").lower()
        artist_pinyin = song.get("artist_pinyin", "")
        tag_lower = (song.get("tag") or "").lower()
        # 支持逗号分隔的关键词，同时转为拼音用于匹配英文歌手名
        kws = [k.strip().lower() for k in keyword.replace("，", ",").split(",") if k.strip()]
        kw_pinyins = [_to_pinyin(kw) for kw in kws]

        # 模糊匹配：歌名/歌手/标签 原文 或 拼音 包含任一关键词（含关键词拼音双向匹配）
        matched = False
        for i, kw in enumerate(kws):
            kp = kw_pinyins[i] if i < len(kw_pinyins) else ""
            if (kw in name_lower or kw in name_pinyin or kp in name_lower or kp in name_pinyin or
                kw in artist_lower or kw in artist_pinyin or kp in artist_lower or kp in artist_pinyin or
                kw in tag_lower):
                matched = True
                break

        if matched:
            local_results.append({
                "source": "local",
                "id": song["rel_path"],
                "title": song["name"],
                "tag": song.get("tag", ""),
                "size_mb": song.get("size_mb", 0),
                "rel_path": song["rel_path"],
                "rating": song.get("rating", 0),
                "thumbnail": "",
            })

    # 按评分排序（高评分在前），然后按名称排序
    local_results.sort(key=lambda x: (-x["rating"], x["title"]))

    # ---- 第二步：搜索在线 ----
    online_results = []
    try:
        if source == "bilibili":
            search_query = f"bilisearch{max_online}:{keyword}"
        else:
            search_query = f"ytsearch{max_online}:{keyword}"
        proxy_args, proxy_env = _get_proxy()
        cmd = [
            "yt-dlp",
            search_query,
            "--dump-json",
            "--no-playlist",
            "--no-warnings",
            "--socket-timeout", "15",
            "--retries", "1",
        ]
        # bilibili 需要模拟浏览器请求头绕过反爬
        if source == "bilibili":
            cmd.extend([
                "--user-agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "--add-header", "Referer:https://www.bilibili.com/",
                "--no-check-certificates",
            ])
        cmd.extend(proxy_args)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45, env=proxy_env)
        _log_debug(
            f"search source={source} keyword={keyword} rc={proc.returncode} "
            f"stdout_lines={len([l for l in proc.stdout.splitlines() if l.strip()])} "
            f"stderr={proc.stderr[:500]!r}"
        )

        if proc.stdout.strip():
            for line in proc.stdout.strip().split("\n"):
                try:
                    info = json.loads(line)
                    raw_title = info.get("title", "")
                    is_already_local = False
                    raw_title_lower = raw_title.lower()
                    for s in all_songs:
                        if s["name"].lower() in raw_title_lower or raw_title_lower in s["name"].lower():
                            is_already_local = True
                            break
                    duration_sec = int(info.get("duration") or 0)
                    online_results.append({
                        "source": "online",
                        "platform": info.get("extractor_key", ""),
                        "id": info.get("id", ""),
                        "title": raw_title,
                        "url": info.get("webpage_url", ""),
                        "duration": duration_sec,
                        "duration_str": f"{duration_sec//60}:{duration_sec%60:02d}",
                        "uploader": info.get("uploader", ""),
                        "thumbnail": info.get("thumbnail", ""),
                        "already_local": is_already_local,
                    })
                except json.JSONDecodeError:
                    pass
    except subprocess.TimeoutExpired:
        _log_debug(f"search timeout keyword={keyword} source={source}")
    except Exception as e:
        _log_debug(f"search exception keyword={keyword} source={source} err={e!r}")

    return jsonify({
        "ok": True,
        "keyword": keyword,
        "local_count": len(local_results),
        "online_count": len(online_results),
        "local": local_results,
        "online": online_results,
    })



@app.route("/api/download", methods=["POST"])
@require_admin
def api_download():
    """下载歌曲（自动提取歌手/上传者，重命名文件 + 写入 song_meta.json）"""
    data = request.get_json()
    video_id = data.get("video_id", "")
    title = data.get("title", "")
    tag = data.get("tag", "default")
    quality = data.get("quality", "192")  # 128/192/256/320 kbps

    if not video_id:
        return jsonify({"ok": False, "error": "缺少 video_id"})

    # 安全检查：标签名不能包含路径穿越字符
    if "/" in tag or "\\" in tag or ".." in tag:
        return jsonify({"ok": False, "error": "标签名称包含非法字符"})
    tag_dir = MUSIC_DIR / tag
    try:
        tag_dir.resolve().relative_to(MUSIC_DIR.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "非法的标签路径"})
    tag_dir.mkdir(parents=True, exist_ok=True)

    # 检查是否已存在
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-（）()").strip()
    existing = list(tag_dir.glob(f"*{safe_title[:20]}*.mp3"))
    if existing:
        return jsonify({
            "ok": True,
            "existed": True,
            "path": str(existing[0]),
            "message": "歌曲已存在",
        })

    proxy_args, proxy_env = _get_proxy()

    # 判断平台：bilibili 的 ID 是 BV 号或 av 号
    is_bilibili = video_id.startswith("BV") or video_id.startswith("av")
    if is_bilibili:
        video_url = f"https://www.bilibili.com/video/{video_id}"
    else:
        video_url = f"https://www.youtube.com/watch?v={video_id}"

    # bilibili 需要模拟浏览器请求头绕过反爬
    bili_headers = []
    if is_bilibili:
        bili_headers = [
            "--user-agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "--add-header", "Referer:https://www.bilibili.com/",
            "--no-check-certificates",
        ]

    # ── 第一步：用 --dump-json 获取视频标题，再从中解析歌手 ──
    artist = ""
    video_title = title
    try:
        info_cmd = [
            "yt-dlp",
            video_url,
            "--dump-json",
            "--no-playlist",
            "--no-warnings",
            "--socket-timeout", "15",
        ] + bili_headers + proxy_args
        info_result = subprocess.run(info_cmd, capture_output=True, text=True, timeout=30, env=proxy_env)
        if info_result.stdout.strip():
            info = json.loads(info_result.stdout.strip().split("\n")[0])
            video_title = info.get("title", video_title)
            # 从标题中智能解析歌手（不再用 uploader 兜底）
            artist, clean_title = _parse_artist_from_title(video_title)
            if artist:
                video_title = clean_title  # 用纯歌名替换原标题，避免重复
    except Exception:
        pass  # 获取元数据失败不影响下载

    # ── 第二步：下载 ──
    try:
        cmd = [
            "yt-dlp",
            video_url,
            "--extract-audio",
            "--audio-format", "mp3",
        ] + bili_headers
        if quality and quality != "best":
            cmd.extend(["--audio-quality", f"{quality}K"])
        cmd.extend([
            "--output", str(tag_dir / "%(title)s.%(ext)s"),
            "--no-playlist",
            "--no-warnings",
            "--restrict-filenames",
        ])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=proxy_env)

        # 查找下载的文件
        mp3_files = sorted(tag_dir.glob("*.mp3"), key=lambda f: f.stat().st_mtime, reverse=True)
        if mp3_files:
            newest = mp3_files[0]

            # ── 第三步：有歌手则重命名为「歌手 - 歌名.mp3」 ──
            if artist:
                safe_artist = "".join(c for c in artist if c.isalnum() or c in " _-（）()").strip()
                safe_name = "".join(c for c in video_title if c.isalnum() or c in " _-（）()").strip()
                artist_filename = f"{safe_artist} - {safe_name}.mp3"
                new_path = newest.parent / artist_filename
                if new_path != newest and not new_path.exists():
                    try:
                        newest.rename(new_path)
                        newest = new_path
                    except OSError:
                        pass  # 重命名失败不阻塞

                # ── 写入 song_meta.json ──
                meta = load_song_meta()
                rel_path = str(newest.relative_to(MUSIC_DIR))
                meta[rel_path] = {"name": video_title, "artist": artist}
                save_song_meta(meta)

            return jsonify({
                "ok": True,
                "path": str(newest),
                "name": newest.stem,
                "size_mb": round(newest.stat().st_size / (1024 * 1024), 1),
                "artist": artist,
            })

        return jsonify({"ok": False, "error": "下载失败，请检查网络"})

    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "下载超时"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/play", methods=["POST"])
@require_auth
def api_play():
    """播放歌曲"""
    global playlist_songs, playlist_index
    data = request.get_json()
    path = data.get("path", "")
    volume = data.get("volume")
    # 可选：传入完整播放列表，用于循环模式
    songs_list = data.get("playlist")
    start_index = data.get("index", 0)

    if not path:
        return jsonify({"ok": False, "error": "缺少文件路径"})

    if not os.path.isabs(path):
        path = str(MUSIC_DIR / path)

    result = player.play(path, volume=volume)

    # 更新播放列表状态
    if songs_list:
        playlist_songs = songs_list
        playlist_index = start_index
    else:
        playlist_index = -1

    time.sleep(0.3)
    status = player.status()
    result.update(status)
    return jsonify(result)

@app.route("/api/pause", methods=["POST"])
@require_auth
def api_pause():
    """暂停/继续"""
    result = player.pause_resume()
    return jsonify(result)

@app.route("/api/stop", methods=["POST"])
@require_auth
def api_stop():
    """停止播放"""
    global playlist_index
    playlist_index = -1
    result = player.force_stop()
    return jsonify(result)

@app.route("/api/volume", methods=["POST"])
@require_auth
def api_volume():
    """设置音量"""
    data = request.get_json()
    volume = data.get("volume", 70)
    result = player.set_volume(volume)
    return jsonify(result)

@app.route("/api/rename", methods=["POST"])
@require_admin
def api_rename():
    """重命名歌曲文件 + 可选更新歌手"""
    data = request.get_json()
    rel_path = data.get("rel_path", "")
    new_name = data.get("new_name", "").strip()
    artist = data.get("artist")  # 新增：可选歌手字段
    
    if not rel_path or not new_name:
        return jsonify({"ok": False, "error": "缺少参数"})
    
    old_path = MUSIC_DIR / rel_path
    # 安全检查：确保文件在音乐目录内
    try:
        old_path.resolve().relative_to(MUSIC_DIR.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "非法的文件路径"})
    if not old_path.exists():
        return jsonify({"ok": False, "error": "文件不存在"})
    
    # 保持后缀名
    suffix = old_path.suffix
    # 清理非法字符
    new_filename = re.sub(r'[\\/:*?"<>|]', '_', new_name) + suffix
    new_path = old_path.parent / new_filename
    
    if new_path.exists() and new_path != old_path:
        return jsonify({"ok": False, "error": "同名文件已存在"})
    
    try:
        old_path.rename(new_path)
        
        # 如果传了 artist，同步更新 song_meta.json
        artist_val = ""
        if artist is not None:
            artist_val = artist.strip()
            meta = load_song_meta()
            meta[rel_path] = {"name": new_name, "artist": artist_val}
            save_song_meta(meta)
        
        return jsonify({
            "ok": True,
            "new_name": new_name,
            "new_filename": new_filename,
            "artist": artist_val,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/update-song", methods=["POST"])
@require_admin
def api_update_song():
    """更新歌曲的显示名称和歌手（不修改文件，仅存到 song_meta.json）"""
    data = request.get_json()
    rel_path = data.get("rel_path", "").strip()
    name = data.get("name", "").strip()
    artist = data.get("artist", "").strip()

    if not rel_path:
        return jsonify({"ok": False, "error": "缺少 rel_path"})
    if not name:
        return jsonify({"ok": False, "error": "歌名不能为空"})

    old_path = MUSIC_DIR / rel_path
    try:
        old_path.resolve().relative_to(MUSIC_DIR.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "非法的文件路径"})
    if not old_path.exists():
        return jsonify({"ok": False, "error": "文件不存在"})

    meta = load_song_meta()
    meta[rel_path] = {"name": name, "artist": artist}
    save_song_meta(meta)

    return jsonify({"ok": True, "name": name, "artist": artist})


@app.route("/api/search-song", methods=["POST"])
@require_auth
def api_search_song():
    """联网搜索歌曲的正确歌名和歌手（通过网易云音乐 API）。
    接受 {"rel_path": "..."}，返回候选列表。
    """
    import urllib.request
    import urllib.parse

    data = request.get_json()
    rel_path = data.get("rel_path", "").strip()
    if not rel_path:
        return jsonify({"ok": False, "error": "缺少 rel_path"})

    song_path = MUSIC_DIR / rel_path
    if not song_path.exists():
        return jsonify({"ok": False, "error": "文件不存在"})

    # 从文件名构建搜索关键词：去掉已知平台标记、音质标记、扩展名
    stem = song_path.stem
    query = stem.replace("_", " ")
    for kw in ["Official", "MV", "Music Video", "Lyrics", "KTV",
                "1080P", "HD1080P", "4K", "HD", "HQ", "FLAC",
                "Official MV", "Official Music Video",
                "with lyrics", "sing along", "English sub",
                "Remastered", "Lyrics Video"]:
        query = re.sub(rf'\b{kw}\b', '', query, flags=re.IGNORECASE)
    # 去掉括号及内容
    query = re.sub(r'[\(\[\{].*?[\)\]\}]', '', query)
    # 去掉域名、网址
    query = re.sub(r'\b\w+\.(com|net|org|cc|io|tv)\b', '', query, flags=re.IGNORECASE)
    # 合并空格、清理首尾
    query = re.sub(r'\s+', ' ', query).strip('- .').strip()
    if not query or len(query) < 2:
        return jsonify({"ok": True, "candidates": [], "query": stem, "hint": "无法构建有效搜索词"})

    # 调用网易云音乐搜索 API
    try:
        url = ("https://music.163.com/api/search/get?"
               f"csrf_token=&s={urllib.parse.quote(query)}&type=1&limit=6&offset=0")
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://music.163.com/",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        candidates = []
        if result.get("code") == 200:
            for s in result.get("result", {}).get("songs", []):
                name = s.get("name", "").strip()
                artists = ", ".join([a.get("name", "").strip()
                           for a in s.get("artists", []) if a.get("name")])
                if name:
                    candidates.append({"name": name, "artist": artists})

        return jsonify({"ok": True, "candidates": candidates, "query": query})

    except Exception as e:
        return jsonify({"ok": False, "error": f"搜索失败: {str(e)}", "query": query})


# ============================================
# Shazam 听歌识曲
# ============================================
RECOGNIZE_CACHE_FILE = DATA_DIR / "recognize_cache.json"

def load_recognize_cache() -> dict:
    """加载识曲结果缓存，避免重复识别同一首歌。"""
    if RECOGNIZE_CACHE_FILE.exists():
        try:
            return json.loads(RECOGNIZE_CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            return {}
    return {}

def save_recognize_cache(cache: dict):
    RECOGNIZE_CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


@app.route("/api/recognize-song", methods=["POST"])
@require_auth
def api_recognize_song():
    """使用 Shazam 听歌识曲识别歌曲。
    接受 {"rel_path": "..."}，返回候选结果。
    结果会缓存到本地，避免重复识别。
    """
    import asyncio
    import tempfile

    data = request.get_json()
    rel_path = data.get("rel_path", "").strip()
    if not rel_path:
        return jsonify({"ok": False, "error": "缺少 rel_path"})

    song_path = MUSIC_DIR / rel_path
    if not song_path.exists():
        return jsonify({"ok": False, "error": "文件不存在"})

    # 检查缓存
    cache = load_recognize_cache()
    if rel_path in cache:
        return jsonify({"ok": True, "cached": True, **cache[rel_path]})

    # 多时间段重试：前20秒 → 30秒处 → 60秒处 → 90秒处
    from shazamio import Shazam

    result = None
    tried_offsets = []
    response_data = {}

    try:
        async def _recognize(filepath):
            shazam = Shazam()
            return await shazam.recognize(filepath)

        for offset in [0, 30, 60, 90]:
            tmp_path = os.path.join(tempfile.gettempdir(), f"_shazam_{os.getpid()}_{offset}.wav")
            try:
                ffmpeg_cmd = [
                    "ffmpeg", "-y", "-i", str(song_path),
                    "-ac", "2", "-ar", "44100",
                ]
                if offset == 0:
                    ffmpeg_cmd += ["-t", "20"]
                else:
                    ffmpeg_cmd += ["-ss", str(offset), "-t", "20"]

                ffmpeg_cmd.append(tmp_path)
                subprocess.run(ffmpeg_cmd, capture_output=True, timeout=30)

                if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 10000:
                    continue

                result = asyncio.run(_recognize(tmp_path))

                if result and result.get('track') and result['track'].get('title'):
                    track = result['track']
                    title = track.get('title', '')
                    subtitle = track.get('subtitle', '')
                    genre = track.get('genres', {}).get('primary', '')
                    response_data = {
                        "candidates": [{
                            "name": title,
                            "artist": subtitle,
                            "genre": genre,
                        }],
                        "source": "shazam",
                        "offset_used": offset,
                    }
                    break  # 找到了，跳出循环
                else:
                    tried_offsets.append(offset)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

        if result is None or not (result.get('track') and result['track'].get('title')):
            response_data = {
                "candidates": [],
                "source": "shazam",
                "hint": f"Shazam 未识别（已尝试 {len(tried_offsets)} 个时间段）" if tried_offsets else "识别请求未返回有效结果",
                "tried_offsets": tried_offsets,
            }

    except Exception as e:
        return jsonify({"ok": False, "error": f"识曲失败: {str(e)}"})

    # 写入缓存（无论成功失败都缓存，避免重复调用）
    cache[rel_path] = response_data
    save_recognize_cache(cache)

    return jsonify({"ok": True, "cached": False, **response_data})


@app.route("/api/seek", methods=["POST"])
@require_auth
def api_seek():
    """跳转到指定位置"""
    data = request.get_json()
    position = float(data.get("position", 0))
    result = player.seek(position)
    return jsonify(result)

@app.route("/api/next", methods=["POST"])
@require_auth
def api_next():
    """强制切到下一首（安全网：VLC 回调未触发时的备用方案）"""
    global playlist_songs, playlist_index
    if not playlist_songs or playlist_index < 0:
        return jsonify({"ok": False, "error": "无播放列表"})

    mode = player._loop_mode
    if mode == "all":
        next_idx = playlist_index + 1
        if next_idx >= len(playlist_songs):
            next_idx = 0
        playlist_index = next_idx
        song = playlist_songs[next_idx]
        abs_path = song.get("path") or str(MUSIC_DIR / song["rel_path"])
        player.play(abs_path)
        return jsonify({"ok": True, "index": next_idx, "name": song.get("name", "")})
    elif mode == "off":
        playlist_index = -1
        player.stop()
        return jsonify({"ok": True, "stopped": True})
    else:
        # single 模式由 VLC 回调处理，这里直接返回
        return jsonify({"ok": True, "mode": "single", "msg": "单曲循环由 VLC 回调处理"})

@app.route("/api/prev", methods=["POST"])
@require_auth
def api_prev():
    """切换到上一首"""
    global playlist_songs, playlist_index
    if not playlist_songs or playlist_index < 0:
        return jsonify({"ok": False, "error": "无播放列表"})

    mode = player._loop_mode
    if mode == "all":
        prev_idx = playlist_index - 1
        if prev_idx < 0:
            prev_idx = len(playlist_songs) - 1
        playlist_index = prev_idx
        song = playlist_songs[prev_idx]
        abs_path = song.get("path") or str(MUSIC_DIR / song["rel_path"])
        player.play(abs_path)
        return jsonify({"ok": True, "index": prev_idx, "name": song.get("name", "")})
    elif mode == "single":
        # 单曲循环：从头播放当前歌曲
        song = playlist_songs[playlist_index]
        abs_path = song.get("path") or str(MUSIC_DIR / song["rel_path"])
        player.play(abs_path)
        return jsonify({"ok": True, "index": playlist_index, "name": song.get("name", ""), "restart": True})
    else:
        playlist_index = -1
        player.stop()
        return jsonify({"ok": True, "stopped": True})

@app.route("/api/delete", methods=["POST"])
@require_admin
def api_delete():
    """删除歌曲文件"""
    data = request.get_json()
    if not data or "rel_path" not in data:
        return jsonify({"ok": False, "error": "缺少参数 rel_path"})

    rel_path = data["rel_path"]
    file_path = MUSIC_DIR / rel_path

    # 安全检查：确保文件在音乐目录内
    try:
        file_path.resolve().relative_to(MUSIC_DIR.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "非法的文件路径"})

    if not file_path.exists():
        return jsonify({"ok": False, "error": "文件不存在"})

    try:
        file_path.unlink()
        # 清除缓存
        if "library_cache" in globals():
            global library_cache
            library_cache = None
        return jsonify({"ok": True, "msg": "歌曲已删除"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/loop-mode", methods=["GET", "POST"])
@require_auth
def api_loop_mode():
    """获取/设置循环播放模式"""
    if request.method == "GET":
        return jsonify({"ok": True, "loop_mode": player.get_loop_mode()})
    else:
        data = request.get_json()
        mode = data.get("mode", "all")
        result = player.set_loop_mode(mode)
        return jsonify(result)

# 浏览器连接检测（无连接时自动关闭服务器）
_last_status_poll = time.time()
_idle_shutdown_enabled = True


def _idle_monitor():
    """后台线程：检测无浏览器连接时自动关闭服务器（超时延长至 10 分钟，适配移动端播放）"""
    global _last_status_poll
    while _idle_shutdown_enabled:
        time.sleep(30)
        idle_seconds = time.time() - _last_status_poll
        # 移动端可能长时间流式播放不轮询状态，超时设为 600 秒（10 分钟）
        if idle_seconds > 600 and not player.status().get("active"):
            print(f"[自动关闭] 已 {int(idle_seconds)} 秒无浏览器连接，服务器自动退出")
            stop_schedule_checker()
            # 使用优雅关闭而非 os._exit()
            import signal
            os.kill(os.getpid(), signal.SIGTERM)


@app.route("/api/ping")
def api_ping():
    """心跳接口，移动端用它保持服务器存活（无需密码）"""
    global _last_status_poll
    _last_status_poll = time.time()
    # 告知客户端是否需要验证，以及当前角色
    need_auth = not _check_auth()
    is_admin = _check_admin()
    return jsonify({"ok": True, "need_auth": need_auth, "role": "admin" if is_admin else "readonly"})


@app.route("/api/status")
def api_status():
    """获取播放状态"""
    global _last_status_poll
    _last_status_poll = time.time()
    status = player.status()
    # 如果有当前文件，附上文件名
    if status["current_file"]:
        p = Path(status["current_file"])
        status["current_name"] = p.stem
    return jsonify(status)


@app.route("/api/shutdown", methods=["POST"])
@require_admin
def api_shutdown():
    """关闭服务器"""
    global _idle_shutdown_enabled
    _idle_shutdown_enabled = False
    player.force_stop()
    stop_schedule_checker()
    print("[手动关闭] 收到关闭请求，服务器即将退出")

    def _delayed_exit():
        time.sleep(0.5)
        import signal
        os.kill(os.getpid(), signal.SIGTERM)

    _t = threading.Thread(target=_delayed_exit, daemon=True)
    _t.start()
    return jsonify({"ok": True, "message": "服务器正在关闭..."})

@app.route("/api/rate", methods=["POST"])
@require_auth
def api_rate():
    """评分歌曲"""
    data = request.get_json()
    song_id = data.get("id", "")
    rating = int(data.get("rating", 0))

    if not song_id:
        return jsonify({"ok": False, "error": "缺少歌曲 ID"})

    # rating: 0=无评分, 1=一般喜欢, 2=较喜欢, 3=最爱
    if rating < 0 or rating > 3:
        return jsonify({"ok": False, "error": "评分范围为 0-3"})

    ratings = load_ratings()
    if rating == 0:
        ratings.pop(song_id, None)
    else:
        ratings[song_id] = rating
    save_ratings(ratings)

    return jsonify({"ok": True, "id": song_id, "rating": rating})

@app.route("/api/tags")
@require_auth
def api_tags():
    """获取所有标签（含空标签，应用用户自定义排序）"""
    tags = _all_tags_with_order()
    return jsonify({"ok": True, "tags": tags})


@app.route("/api/tags/order", methods=["GET"])
@require_auth
def api_tags_order_get():
    """获取用户保存的标签排序"""
    return jsonify({"ok": True, "order": _load_tag_order()})


@app.route("/api/tags/order", methods=["POST"])
@require_admin
def api_tags_order_set():
    """保存用户拖拽后的标签排序"""
    data = request.get_json() or {}
    order = data.get("order", [])
    if not isinstance(order, list):
        return jsonify({"ok": False, "error": "参数必须是数组"})
    # 安全检查：过滤非法字符，防止路径遍历
    valid = []
    for t in order:
        if isinstance(t, str) and t and "/" not in t and "\\" not in t and ".." not in t:
            valid.append(t)
    _save_tag_order(valid)
    return jsonify({"ok": True, "order": valid})


@app.route("/api/tag/create", methods=["POST"])
@require_admin
def api_tag_create():
    """创建新标签（新建音乐子目录）"""
    data = request.get_json()
    if not data or "name" not in data:
        return jsonify({"ok": False, "error": "缺少标签名称"})

    tag_name = data["name"].strip()
    if not tag_name:
        return jsonify({"ok": False, "error": "标签名称不能为空"})
    # 安全检查：不允许路径遍历
    if "/" in tag_name or "\\" in tag_name or ".." in tag_name:
        return jsonify({"ok": False, "error": "标签名称包含非法字符"})

    tag_dir = MUSIC_DIR / tag_name
    if tag_dir.exists():
        return jsonify({"ok": False, "error": f"标签「{tag_name}」已存在"})

    try:
        tag_dir.mkdir(parents=True)
        return jsonify({"ok": True, "msg": f"标签「{tag_name}」创建成功", "tag": tag_name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/tag/move", methods=["POST"])
@require_admin
def api_tag_move():
    """将歌曲移动到指定标签目录"""
    data = request.get_json()
    if not data or "rel_path" not in data or "tag" not in data:
        return jsonify({"ok": False, "error": "缺少参数"})

    rel_path = data["rel_path"]
    target_tag = data["tag"].strip()

    src = MUSIC_DIR / rel_path
    # 安全检查
    try:
        src.resolve().relative_to(MUSIC_DIR.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "非法的文件路径"})

    if not src.exists():
        return jsonify({"ok": False, "error": "文件不存在"})

    # 确保目标标签目录存在
    tag_dir = MUSIC_DIR / target_tag
    if not tag_dir.exists():
        tag_dir.mkdir(parents=True)

    dst = tag_dir / src.name
    if dst.exists():
        return jsonify({"ok": False, "error": f"目标位置已存在同名文件"})

    try:
        src.rename(dst)
        # 更新评分映射
        old_rel = rel_path
        new_rel = str(dst.relative_to(MUSIC_DIR))
        if "ratings" in globals() or True:
            r = load_ratings()
            if old_rel in r:
                r[new_rel] = r.pop(old_rel)
                save_ratings(r)
        # 清除缓存
        if "library_cache" in globals():
            global library_cache
            library_cache = None
        return jsonify({"ok": True, "msg": f"已移至「{target_tag}」", "new_path": new_rel, "tag": target_tag})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ============================================
# 定时播放 API
# ============================================
@app.route("/api/schedule", methods=["GET"])
@require_auth
def api_schedule_list():
    """获取所有定时播放项"""
    data = load_schedules()
    return jsonify({"ok": True, "items": data.get("items", [])})


@app.route("/api/schedule", methods=["POST"])
@require_admin
def api_schedule_add():
    """添加定时播放项"""
    req = request.get_json()
    time_val = req.get("time", "").strip()
    song_path = req.get("song_path", "").strip()
    song_name = req.get("song_name", "")
    volume = int(req.get("volume", 70))

    if not time_val or not song_path:
        return jsonify({"ok": False, "error": "缺少 time 或 song_path"})

    # 验证时间格式 HH:MM
    try:
        datetime.strptime(time_val, "%H:%M")
    except ValueError:
        return jsonify({"ok": False, "error": "时间格式错误，请使用 HH:MM"})

    # 验证文件存在且路径安全
    full_path = (MUSIC_DIR / song_path).resolve()
    try:
        full_path.relative_to(MUSIC_DIR.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "非法的文件路径"})
    if not full_path.exists():
        return jsonify({"ok": False, "error": f"文件不存在: {song_path}"})

    data = load_schedules()
    items = data.get("items", [])

    import uuid
    new_item = {
        "id": uuid.uuid4().hex[:8],
        "time": time_val,
        "song_path": song_path,
        "song_name": song_name,
        "volume": volume,
        "enabled": True,
        "created_at": datetime.now().isoformat(),
    }
    items.append(new_item)
    data["items"] = items
    save_schedules(data)

    return jsonify({"ok": True, "item": new_item})


@app.route("/api/schedule/<item_id>", methods=["PUT"])
@require_admin
def api_schedule_update(item_id):
    """更新定时播放项"""
    req = request.get_json()
    data = load_schedules()
    items = data.get("items", [])

    found = False
    for item in items:
        if item.get("id") == item_id:
            found = True
            if "time" in req:
                time_val = req["time"].strip()
                try:
                    datetime.strptime(time_val, "%H:%M")
                    item["time"] = time_val
                except ValueError:
                    return jsonify({"ok": False, "error": "时间格式错误"})
            if "song_path" in req:
                sp = req["song_path"].strip()
                fp = (MUSIC_DIR / sp).resolve()
                try:
                    fp.relative_to(MUSIC_DIR.resolve())
                except ValueError:
                    return jsonify({"ok": False, "error": "非法的文件路径"})
                if not fp.exists():
                    return jsonify({"ok": False, "error": f"文件不存在: {sp}"})
                item["song_path"] = sp
            if "song_name" in req:
                item["song_name"] = req["song_name"]
            if "volume" in req:
                item["volume"] = int(req["volume"])
            if "enabled" in req:
                item["enabled"] = bool(req["enabled"])
            break

    if not found:
        return jsonify({"ok": False, "error": "未找到该定时项"})

    save_schedules(data)
    return jsonify({"ok": True, "item": item})


@app.route("/api/schedule/<item_id>", methods=["DELETE"])
@require_admin
def api_schedule_delete(item_id):
    """删除定时播放项"""
    data = load_schedules()
    items = data.get("items", [])
    new_items = [i for i in items if i.get("id") != item_id]

    if len(new_items) == len(items):
        return jsonify({"ok": False, "error": "未找到该定时项"})

    data["items"] = new_items
    save_schedules(data)
    return jsonify({"ok": True, "deleted": item_id})


# ============================================
# 音乐文件静态服务
# ============================================
@app.route("/music/<path:filename>")
@require_auth
def serve_music(filename):
    """提供音乐文件访问（支持 Range 请求，适配 iOS Safari 流媒体播放）"""
    global _last_status_poll
    _last_status_poll = time.time()  # 流媒体传输也算活跃，防止空闲关闭
    return send_from_directory(MUSIC_DIR, filename)


# ============================================
# 后台歌曲结束事件轮询
# 独立于 API 状态轮询，确保无人访问网页时也能自动切歌
# ============================================
_song_end_polling = False

def _song_end_poller():
    global _song_end_polling
    while _song_end_polling:
        try:
            player.status()
        except Exception:
            pass
        time.sleep(2)

def start_song_end_poller():
    global _song_end_polling
    if _song_end_polling:
        return
    _song_end_polling = True
    threading.Thread(target=_song_end_poller, daemon=True).start()

def stop_song_end_poller():
    global _song_end_polling
    _song_end_polling = False

# ============================================
# 启动
# ============================================
if __name__ == "__main__":
    # 解析参数
    port = 5000
    no_schedule = False
    for arg in sys.argv[1:]:
        if arg == "--no-schedule":
            no_schedule = True
        elif arg.isdigit():
            port = int(arg)
    
    schedule_info = "（定时调度已禁用）" if no_schedule else ""
    # 尝试获取本机 IP
    local_ip = "无法获取"
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    print(f"""
╔══════════════════════════════════════════════════╗
║       🎵 MusicFlow Web 服务                    ║
║                                                  ║
║  电脑访问:     http://localhost:{port}              ║
║  手机访问:     http://{local_ip}:{port}           ║
║  手机播放器:   http://{local_ip}:{port}/mobile      ║
║                                                  ║
║  🔐 安全密钥（公网访问需要输入此密码）:          ║
║  {ACCESS_TOKEN}                                  ║
║                                                  ║
║  按 Ctrl+C 停止服务 {schedule_info}               ║
╚══════════════════════════════════════════════════╝
""")
    # 定时播放调度器（--no-schedule 时跳过）
    if not no_schedule:
        start_schedule_checker()
    else:
        print("[定时播放] 已通过 --no-schedule 禁用定时调度器")
    # 启动歌曲结束事件轮询（确保无人访问网页时也能自动切歌）
    start_song_end_poller()
    # 启动空闲监控（无浏览器连接时自动退出）
    _idle_thread = threading.Thread(target=_idle_monitor, daemon=True)
    _idle_thread.start()
    # 注册 SIGTERM 处理器，确保优雅关闭
    import signal as _signal
    def _graceful_shutdown(signum, frame):
        print("\n[服务器] 收到关闭信号，正在清理...")
        stop_song_end_poller()
        if not no_schedule:
            stop_schedule_checker()
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _graceful_shutdown)
    try:
        app.run(host="0.0.0.0", port=port, debug=False)
    finally:
        stop_song_end_poller()
        if not no_schedule:
            stop_schedule_checker()
