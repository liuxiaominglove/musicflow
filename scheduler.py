#!/usr/bin/env python3
"""
智能音乐定时播放器 - 核心调度引擎

功能：
- 根据 config.yaml 配置定时播放不同风格的音乐
- 自动下载缺失的音乐文件
- 支持后台守护进程模式
- 音量、播放模式等灵活控制
"""

import os
import sys
import time
import signal
import random
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

import yaml
import schedule

from music_downloader import MusicDownloader

# ============================================
# 日志配置
# ============================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scheduler.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


class SmartMusicPlayer:
    """智能音乐播放器"""

    def __init__(self, config_path: str = "config.yaml"):
        # 加载配置
        self.config_path = Path(config_path)
        self.config = self._load_config()

        # 初始化下载器
        music_dir = self.config.get("global", {}).get("music_dir", "./music_library")
        self.downloader = MusicDownloader(music_dir=music_dir)

        # 播放器实例（延迟导入，避免缺少依赖时启动失败）
        self._vlc = None
        self._vlc_instance = None
        self._player = None

        # 播放控制
        self._is_playing = False
        self._stop_event = threading.Event()
        self._play_thread: Optional[threading.Thread] = None
        self._current_schedule_name: str = ""

        # 运行标志
        self._running = True

        # 设置信号处理
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _load_config(self) -> dict:
        """加载 YAML 配置文件"""
        if not self.config_path.exists():
            logger.error(f"配置文件不存在: {self.config_path}")
            logger.info("正在创建默认配置文件...")
            self._create_default_config()
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"配置文件解析失败: {e}")
            sys.exit(1)

    def _create_default_config(self):
        """创建默认配置（如果不存在的话）"""
        # config.yaml 已由项目提供，此函数作为 fallback
        pass

    def _signal_handler(self, signum, frame):
        """处理退出信号"""
        logger.info("\n🛑 收到退出信号，正在停止播放器...")
        self._running = False
        self._stop_event.set()
        self._stop_playback()
        sys.exit(0)

    # ============================================
    # VLC 播放器管理
    # ============================================
    def _init_vlc(self):
        """初始化 VLC 播放器（延迟加载）"""
        if self._vlc is not None:
            return

        try:
            import vlc as _vlc
            self._vlc = _vlc
            self._vlc_instance = _vlc.Instance("--quiet")
            self._player = self._vlc_instance.media_list_player_new()
            logger.info("✅ VLC 播放器初始化成功")
        except ImportError:
            logger.error("❌ 未安装 python-vlc，请运行: pip install python-vlc")
            logger.error("   还需要安装 VLC 应用程序: brew install --cask vlc")
            sys.exit(1)

    def _stop_playback(self):
        """停止当前播放"""
        if self._player:
            try:
                self._player.stop()
            except Exception:
                pass
        self._is_playing = False
        logger.info(f"⏹️  停止播放 [{self._current_schedule_name}]")

    def _play_playlist(self, files: list, volume: float = 0.7, mode: str = "random"):
        """
        播放音乐列表

        Args:
            files: 音乐文件路径列表
            volume: 音量 (0.0-1.0)
            mode: 播放模式 "random"/"sequential"
        """
        if not files:
            logger.warning("⚠️  播放列表为空")
            return

        self._init_vlc()

        # 停止当前播放
        self._stop_playback()
        self._stop_event.clear()

        # 根据模式排列播放列表
        if mode == "random":
            random.shuffle(files)
        playlist = files.copy()

        def _play_worker():
            """播放工作线程"""
            media_list = self._vlc_instance.media_list_new()

            for filepath in playlist:
                if not os.path.exists(filepath):
                    logger.warning(f"文件不存在，跳过: {filepath}")
                    continue
                media = self._vlc_instance.media_new(filepath)
                media_list.add_media(media)

            if media_list.count() == 0:
                logger.warning("没有有效的音乐文件")
                return

            self._player.set_media_list(media_list)

            # 设置音量
            list_player = self._player.get_media_player()
            if list_player:
                list_player.audio_set_volume(int(volume * 100))

            # 开始播放
            self._player.play()
            self._is_playing = True

            logger.info(f"▶️  正在播放: {len(playlist)} 首歌曲")

            # 等待播放完成或被中断
            while self._player.is_playing() and not self._stop_event.is_set():
                time.sleep(1)

            if self._stop_event.is_set():
                self._player.stop()
            else:
                logger.info("✅ 播放列表播放完毕")

            self._is_playing = False

        # 在新线程中播放，避免阻塞调度器
        self._play_thread = threading.Thread(target=_play_worker, daemon=True)
        self._play_thread.start()

    # ============================================
    # 定时任务调度
    # ============================================
    def _schedule_job(self, schedule_config: dict):
        """执行单次定时播放任务"""
        name = schedule_config.get("name", "未命名")
        description = schedule_config.get("description", "")
        volume = schedule_config.get("volume", 0.7)
        mode = self.config.get("global", {}).get("play_mode", "random")
        songs_per_session = schedule_config.get(
            "songs_per_session",
            self.config.get("global", {}).get("songs_per_session", 5),
        )

        self._current_schedule_name = name

        logger.info("")
        logger.info("=" * 60)
        logger.info(f"🎯 触发定时任务: {name}")
        logger.info(f"📝 {description}")
        logger.info(f"🕐 时间: {datetime.now().strftime('%H:%M')}")
        logger.info("=" * 60)

        # 1. 获取本地音乐
        tag = name.replace(" ", "_")
        local_files = self.downloader.get_local_music(tag)

        # 2. 检查本地音乐是否足够
        if len(local_files) < songs_per_session:
            logger.info(f"📥 本地音乐不足 ({len(local_files)}/<{songs_per_session})，开始下载...")
            downloaded = self.downloader.download_schedule_music(schedule_config)
            tag_key = list(downloaded.keys())[0] if downloaded else tag
            new_files = downloaded.get(tag_key, [])
            local_files = self.downloader.get_local_music(tag)
            logger.info(f"📥 下载后共有 {len(local_files)} 首歌")

        # 3. 限制播放数量
        if songs_per_session > 0 and len(local_files) > songs_per_session:
            if mode == "random":
                play_files = random.sample(local_files, songs_per_session)
            else:
                play_files = local_files[:songs_per_session]
        else:
            play_files = local_files

        # 4. 开始播放
        if play_files:
            logger.info(f"🎵 本时段播放 {len(play_files)} 首:")
            for f in play_files:
                logger.info(f"   - {Path(f).name}")

            self._play_playlist(play_files, volume=volume, mode=mode)
        else:
            logger.warning(f"⚠️  没有可播放的音乐 [{name}]")
            logger.warning("   请先运行: python scheduler.py --download")

    def setup_schedules(self):
        """配置所有定时任务"""
        schedules = self.config.get("schedules", [])
        if not schedules:
            logger.error("配置文件中没有定时计划！")
            return

        # 清除旧任务
        schedule.clear()

        for sched in schedules:
            time_str = sched.get("time", "")
            name = sched.get("name", "未命名")

            if not time_str:
                logger.warning(f"跳过无效计划（缺少时间）: {name}")
                continue

            # 使用 schedule 库注册任务
            schedule.every().day.at(time_str).do(self._schedule_job, sched)
            logger.info(f"📅 已注册任务: [{time_str}] {name}")

        logger.info(f"\n✅ 共注册 {len(schedules)} 个定时任务\n")

    def run(self, daemon: bool = False):
        """
        启动调度器

        Args:
            daemon: 是否以守护进程模式运行
        """
        self.setup_schedules()

        # 打印下次运行时间
        self._print_next_runs()

        logger.info("🚀 智能音乐播放器已启动！")
        logger.info("   按 Ctrl+C 停止运行\n")

        if daemon:
            logger.info("💤 后台守护模式运行中...")

        try:
            while self._running:
                schedule.run_pending()
                time.sleep(30)  # 每30秒检查一次
        except KeyboardInterrupt:
            logger.info("\n👋 播放器已停止")
        finally:
            self._stop_playback()

    def _print_next_runs(self):
        """打印所有任务的下一次运行时间"""
        logger.info("\n📋 今日播放计划:")
        logger.info("-" * 40)
        jobs = schedule.get_jobs()
        for job in sorted(jobs, key=lambda j: str(j.next_run)):
            logger.info(f"  🕐 {str(job.next_run)[:19]} → {job.job_func.args[0].get('name', '未知')}")
        logger.info("-" * 40 + "\n")

    def download_only(self):
        """仅下载音乐，不启动播放"""
        schedules = self.config.get("schedules", [])
        if not schedules:
            logger.error("没有定时计划可下载")
            return

        logger.info("📥 开始下载所有计划的音乐...")
        results = self.downloader.download_all_schedules(schedules)

        logger.info("\n" + "=" * 60)
        logger.info("📊 下载汇总:")
        logger.info("=" * 60)
        for name, files in results.items():
            logger.info(f"  [{name}]: {len(files)} 首音乐")
        logger.info("")

    def test_play(self, schedule_name: str = None):
        """测试播放模式"""
        schedules = self.config.get("schedules", [])

        if not schedules:
            logger.error("没有可测试的计划")
            return

        if schedule_name:
            # 按名称查找
            target = next(
                (s for s in schedules if s.get("name") == schedule_name), None
            )
            if not target:
                logger.error(f"未找到计划: {schedule_name}")
                logger.info(f"可用计划: {[s.get('name') for s in schedules]}")
                return
        else:
            # 默认播放第一个
            target = schedules[0]
            logger.info(f"使用第一个计划测试: {target.get('name')}")

        self._schedule_job(target)


def main():
    """主入口"""
    import argparse

    parser = argparse.ArgumentParser(
        description="🎵 智能音乐定时播放器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 下载所有计划的音乐
  python scheduler.py --download

  # 启动定时播放（前台模式）
  python scheduler.py

  # 后台守护进程模式
  python scheduler.py --daemon

  # 查看音乐库
  python scheduler.py --list

  # 测试播放某个计划
  python scheduler.py --test "早安！起床音乐"

  # 查看所有计划
  python scheduler.py --status
        """,
    )

    parser.add_argument("--config", "-c", default="config.yaml", help="配置文件路径")
    parser.add_argument("--download", "-d", action="store_true", help="下载所有计划的音乐")
    parser.add_argument("--daemon", action="store_true", help="后台守护进程模式")
    parser.add_argument("--test", "-t", type=str, nargs="?", const="", help="测试播放某个计划")
    parser.add_argument("--list", "-l", action="store_true", help="查看音乐库")
    parser.add_argument("--status", "-s", action="store_true", help="查看播放计划状态")
    parser.add_argument("--search", type=str, help="搜索并下载音乐 (格式: 关键词)")
    parser.add_argument("--max", type=int, default=3, help="搜索下载最大数量")

    args = parser.parse_args()

    # 确保在正确目录运行
    script_dir = Path(__file__).parent
    os.chdir(script_dir)

    player = SmartMusicPlayer(config_path=args.config)

    if args.list:
        player.downloader.list_library()
    elif args.download:
        player.download_only()
    elif args.status:
        player.setup_schedules()
        player._print_next_runs()
    elif args.search:
        player.downloader.download_by_keyword(args.search, max_songs=args.max)
    elif args.test is not None:
        name = args.test if args.test else None
        player.test_play(schedule_name=name)
    else:
        player.run(daemon=args.daemon)


if __name__ == "__main__":
    main()
