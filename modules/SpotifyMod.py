#             █ █ ▀ █▄▀ ▄▀█ █▀█ ▀
#             █▀█ █ █ █ █▀█ █▀▄ █
#              © Copyright 2022
#           https://t.me/hikariatama
#
# 🔒      Licensed under the GNU AGPLv3
# 🌐 https://www.gnu.org/licenses/agpl-3.0.html
# ORIGINAL MODULE: https://raw.githubusercontent.com/hikariatama/ftg/master/spotify.py
#
# =======================================
#   _  __         __  __           _
#  | |/ /___     |  \/  | ___   __| |___
#  | ' // _ \    | |\/| |/ _ \ / _` / __|
#  | . \  __/    | |  | | (_) | (_| \__ \
#  |_|\_\___|    |_|  |_|\___/ \__,_|___/
#           @ke_mods
# =======================================
#
# meta developer: @ke_mods
# requires: telethon spotipy pillow requests yt-dlp curl_cffi
# scope: ffmpeg

__version__ = (1, 0)

import asyncio
import contextlib
import functools
import io
import logging
import re
import textwrap
import time
import traceback
import os
from types import FunctionType

import random
import requests
import spotipy
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
from telethon.errors import FloodWaitError
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Message

from .. import loader, utils

logger = logging.getLogger(__name__)
logging.getLogger("spotipy").setLevel(logging.CRITICAL)

class Banners:
    def __init__(
        self,
        title: str,
        artists: list,
        duration: int,
        progress: int,
        track_cover: bytes,
        font,
        blur,
        album_title: str = "",
        meta_info: str = "",
    ):
        self.title = title
        self.artists = ", ".join(artists) if isinstance(artists, list) else artists
        self.duration = duration
        self.progress = progress
        self.track_cover = track_cover
        self.font_url = font
        self.blur_intensity = blur
        self.album_title = album_title
        self.meta_info = meta_info

    def _get_font(self, size, font_bytes):
        return ImageFont.truetype(io.BytesIO(font_bytes), size)

    def _prepare_cover(self, size, radius):
        cover = Image.open(io.BytesIO(self.track_cover)).convert("RGBA")
        cover = cover.resize((size, size), Image.Resampling.LANCZOS)
        
        mask = Image.new("L", (size, size), 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle((0, 0, size, size), radius=radius, fill=255)
        
        output = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        output.paste(cover, (0, 0), mask=mask)
        return output

    def _prepare_background(self, w, h):
        bg = Image.open(io.BytesIO(self.track_cover)).convert("RGBA")
        bg = bg.resize((w, h), Image.Resampling.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(radius=self.blur_intensity))
        bg = ImageEnhance.Brightness(bg).enhance(0.35) 
        return bg

    def _draw_progress_bar(self, draw, x, y, w, h, progress_pct, color="white", bg_color="#6b6b6b"):
        draw.rounded_rectangle((x, y, x + w, y + h), radius=h/2, fill=bg_color)
        
        fill_w = int(w * progress_pct)
        if fill_w > 0:
            draw.rounded_rectangle((x, y, x + fill_w, y + h), radius=h/2, fill=color)

    def horizontal(self):
        W, H = 1500, 600
        padding = 60
        cover_size = 480
        
        font_bytes = requests.get(self.font_url).content
        title_font = self._get_font(55, font_bytes)
        artist_font = self._get_font(45, font_bytes)
        time_font = self._get_font(25, font_bytes)

        img = self._prepare_background(W, H)
        draw = ImageDraw.Draw(img)
        
        cover = self._prepare_cover(cover_size, 30)
        img.paste(cover, (padding, (H - cover_size) // 2), cover)

        text_x = padding + cover_size + 60
        text_y_start = 100
        text_width_limit = W - text_x - padding

        wrapper = textwrap.TextWrapper(width=23)
        title_lines = wrapper.wrap(self.title)
        
        if len(title_lines) > 2:
            title_lines = title_lines[:2]
            title_lines[-1] += "..."

        current_y = text_y_start
        title_height = title_font.getbbox("Ah")[3] + 15

        for line in title_lines:
            draw.text((text_x, current_y), line, font=title_font, fill="white")
            current_y += title_height
        
        display_artist = self.artists
        while artist_font.getlength(display_artist) > text_width_limit and len(display_artist) > 0:
            display_artist = display_artist[:-1]
        if len(display_artist) < len(self.artists): display_artist += "…"

        artist_y = current_y + 10 
        draw.text((text_x, artist_y), display_artist, font=artist_font, fill="#b3b3b3")

        cur_time = f"{(self.progress//1000//60):02}:{(self.progress//1000%60):02}"
        dur_time = f"{(self.duration//1000//60):02}:{(self.duration//1000%60):02}"
        
        cur_w = time_font.getlength(cur_time)
        dur_w = time_font.getlength(dur_time)
        
        bar_y = 480
        bar_h = 8
        gap = 25
        
        draw.text((text_x, bar_y - 12), cur_time, font=time_font, fill="white")
        
        bar_start_x = text_x + cur_w + gap
        bar_end_x = text_x + text_width_limit - dur_w - gap
        bar_w = bar_end_x - bar_start_x
        
        prog_pct = self.progress / self.duration if self.duration > 0 else 0
        self._draw_progress_bar(draw, bar_start_x, bar_y, bar_w, bar_h, prog_pct)
        
        draw.text((bar_end_x + gap, bar_y - 12), dur_time, font=time_font, fill="white")

        by = io.BytesIO()
        img.save(by, format="PNG")
        by.seek(0)
        by.name = "banner.png"
        return by

    def vertical(self):
        W, H = 1000, 1500
        padding = 80
        cover_size = 800
        
        font_bytes = requests.get(self.font_url).content
        title_font = self._get_font(60, font_bytes)
        artist_font = self._get_font(45, font_bytes)
        time_font = self._get_font(35, font_bytes)

        img = self._prepare_background(W, H)
        draw = ImageDraw.Draw(img)

        cover = self._prepare_cover(cover_size, 40)
        cover_x = (W - cover_size) // 2
        cover_y = 120
        img.paste(cover, (cover_x, cover_y), cover)

        text_area_y = cover_y + cover_size + 120
        text_width_limit = W - (padding * 2)

        wrapper = textwrap.TextWrapper(width=23)
        title_lines = wrapper.wrap(self.title)
        
        if len(title_lines) > 2:
            title_lines = title_lines[:2]
            title_lines[-1] += "..."

        current_y = text_area_y
        title_height = title_font.getbbox("Ah")[3] + 15

        for line in title_lines:
            w = title_font.getlength(line)
            draw.text(((W - w) / 2, current_y), line, font=title_font, fill="white")
            current_y += title_height

        display_artist = self.artists
        while artist_font.getlength(display_artist) > text_width_limit and len(display_artist) > 0:
            display_artist = display_artist[:-1]
        if len(display_artist) < len(self.artists): display_artist += "…"

        artist_w = artist_font.getlength(display_artist)
        draw.text(((W - artist_w) / 2, current_y + 15), display_artist, font=artist_font, fill="#b3b3b3")

        bar_y = text_area_y + 260
        if len(title_lines) > 1:
            bar_y += 60

        bar_h = 8
        bar_w = W - (padding * 2)
        prog_pct = self.progress / self.duration if self.duration > 0 else 0
        
        self._draw_progress_bar(draw, padding, bar_y, bar_w, bar_h, prog_pct, color="white", bg_color="#6b6b6b")

        cur_time = f"{(self.progress//1000//60):02}:{(self.progress//1000%60):02}"
        dur_time = f"{(self.duration//1000//60):02}:{(self.duration//1000%60):02}"
        
        draw.text((padding, bar_y + 40), cur_time, font=time_font, fill="white")
        
        dur_w = time_font.getlength(dur_time)
        draw.text((W - padding - dur_w, bar_y + 40), dur_time, font=time_font, fill="white")

        by = io.BytesIO()
        img.save(by, format="PNG")
        by.seek(0)
        by.name = "banner.png"
        return by

    # Ultra banner from YaMusic by @codrago_m
    def ultra(self) -> io.BytesIO:
        WIDTH, HEIGHT = 2560, 1220

        font_bytes = requests.get(self.font_url).content

        def get_font(size):
            try:
                return ImageFont.truetype(io.BytesIO(font_bytes), size)
            except Exception:
                return ImageFont.load_default()

        try:
            original_cover = Image.open(io.BytesIO(self.track_cover)).convert("RGBA")
        except Exception:
            original_cover = Image.new("RGBA", (1000, 1000), "black")

        dominant_color_img = original_cover.resize((1, 1), Image.Resampling.LANCZOS)
        dominant_color = dominant_color_img.getpixel((0, 0))

        r, g, b, a = dominant_color
        brightness = (r * 299 + g * 587 + b * 114) / 1000
        if brightness < 60:
            r = min(255, r + 60)
            g = min(255, g + 60)
            b = min(255, b + 60)
            dominant_color = (r, g, b, 255)

        background = original_cover.copy()
        bg_w, bg_h = background.size

        target_ratio = WIDTH / HEIGHT
        current_ratio = bg_w / bg_h

        if current_ratio > target_ratio:
            new_w = int(bg_h * target_ratio)
            offset = (bg_w - new_w) // 2
            background = background.crop((offset, 0, offset + new_w, bg_h))
        else:
            new_h = int(bg_w / target_ratio)
            offset = (bg_h - new_h) // 2
            background = background.crop((0, offset, bg_w, offset + new_h))

        background = background.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

        if self.blur_intensity > 0:
            background = background.filter(ImageFilter.GaussianBlur(radius=self.blur_intensity))

        dark_overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 180))
        background = Image.alpha_composite(background, dark_overlay)

        cover_size = 500
        cover_x = (WIDTH - cover_size) // 2
        cover_y = 160

        glow_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw_glow = ImageDraw.Draw(glow_layer)

        glow_rect_size = 620
        g_x = (WIDTH - glow_rect_size) // 2
        g_y = cover_y + (cover_size - glow_rect_size) // 2

        draw_glow.rounded_rectangle(
            (g_x, g_y, g_x + glow_rect_size, g_y + glow_rect_size),
            radius=50,
            fill=dominant_color,
        )

        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=60))
        glow_layer = ImageEnhance.Brightness(glow_layer).enhance(1.4)
        glow_layer = ImageEnhance.Color(glow_layer).enhance(1.2)

        background = Image.alpha_composite(background, glow_layer)

        cover_img = original_cover.resize((cover_size, cover_size), Image.Resampling.LANCZOS)

        mask = Image.new("L", (cover_size, cover_size), 0)
        draw_mask = ImageDraw.Draw(mask)
        draw_mask.rounded_rectangle((0, 0, cover_size, cover_size), radius=45, fill=255)

        background.paste(cover_img, (cover_x, cover_y), mask)

        draw = ImageDraw.Draw(background)
        center_x = WIDTH // 2
        current_y = cover_y + cover_size + 130

        def draw_text_shadow(text, pos, font, fill="white", anchor="ms"):
            x, y = pos
            draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 240), anchor=anchor)
            draw.text((x, y), text, font=font, fill=fill, anchor=anchor)

        font_title = get_font(100)
        title_text = self.title if len(self.title) <= 30 else self.title[:30] + "..."
        draw_text_shadow(title_text.upper(), (center_x, current_y), font_title)

        current_y += 85

        font_artist = get_font(65)
        artist_text = self.artists if len(self.artists) <= 45 else self.artists[:45] + "..."
        draw_text_shadow(artist_text.upper(), (center_x, current_y), font_artist, fill=(255, 255, 255, 240))

        current_y += 80

        bar_width = 800
        font_time = get_font(40)

        bar_start_x = center_x - (bar_width // 2)
        bar_end_x = center_x + (bar_width // 2)
        bar_y = current_y

        total_time_str = f"{self.duration // 1000 // 60:02d}:{(self.duration // 1000) % 60:02d}"
        cur_time_str = f"{self.progress // 1000 // 60:02d}:{(self.progress // 1000) % 60:02d}"

        draw_text_shadow(cur_time_str, (bar_start_x - 30, bar_y), font_time, anchor="rm")
        draw_text_shadow(total_time_str, (bar_end_x + 30, bar_y), font_time, anchor="lm")

        old_state = random.getstate()
        random.seed(self.title + str(self.duration))

        num_bars = 65
        bar_spacing = bar_width / num_bars
        bar_w = max(4, int(bar_spacing * 0.5))
        max_h, min_h = 50, 6

        active_bars = int(num_bars * (self.progress / self.duration)) if self.duration > 0 else 0

        for i in range(num_bars):
            base_h = random.randint(min_h, max_h)
            edge_factor = 1.0 - abs((i - num_bars / 2) / (num_bars / 2))
            h = max(min_h, int(base_h * 0.4 + max_h * edge_factor * 0.6))
            x_center = bar_start_x + i * bar_spacing
            color = (255, 255, 255, 255) if i < active_bars else (80, 80, 80, 100)
            draw.rounded_rectangle(
                (x_center - bar_w / 2, bar_y - h / 2, x_center + bar_w / 2, bar_y + h / 2),
                radius=int(bar_w / 2),
                fill=color,
            )

        random.setstate(old_state)

        current_y += 80

        if self.album_title:
            font_album = get_font(50)
            album_text = self.album_title if len(self.album_title) <= 50 else self.album_title[:50] + "..."
            draw_text_shadow(album_text, (center_x, current_y), font_album, fill=(230, 230, 230))
            current_y += 60

        if self.meta_info:
            font_meta = get_font(40)
            draw_text_shadow(self.meta_info, (center_x, current_y), font_meta, fill=(210, 210, 210))

        by = io.BytesIO()
        background.save(by, format="PNG")
        by.seek(0)
        by.name = "banner.png"
        return by

@loader.tds
class SpotifyMod(loader.Module):
    """Card with the currently playing track on Spotify."""

    strings = {
        "name": "SpotifyMod",
        "need_auth": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Please execute"
            " </b><code>.sauth</code><b> before performing this action.</b>"
        ),
        "on-repeat": (
            "<tg-emoji emoji-id=5258420634785947640>🔄</tg-emoji> <b>Set on-repeat.</b>"
        ),
        "off-repeat": (
            "<tg-emoji emoji-id=5260687119092817530>🔄</tg-emoji> <b>Stopped track"
            " repeat.</b>"
        ),
        "skipped": (
            "<tg-emoji emoji-id=6037622221625626773>➡️</tg-emoji> <b>Skipped track.</b>"
        ),
        "playing": "<tg-emoji emoji-id=5773626993010546707>▶️</tg-emoji> <b>Playing...</b>",
        "back": (
            "<tg-emoji emoji-id=6039539366177541657>⬅️</tg-emoji> <b>Switched to previous"
            " track</b>"
        ),
        "paused": "<tg-emoji emoji-id=5774077015388852135>❌</tg-emoji> <b>Pause</b>",
        "restarted": (
            "<tg-emoji emoji-id=5843596438373667352>✅️</tg-emoji> <b>Playing track"
            " from the"
            " beginning</b>"
        ),
        "liked": (
            "<tg-emoji emoji-id=5258179403652801593>❤️</tg-emoji> <b>Liked current"
            " playback</b>"
        ),
        "unlike": (
            "<tg-emoji emoji-id=5774077015388852135>❌</tg-emoji>"
            " <b>Unliked current playback</b>"
        ),
        "err": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>An error occurred."
            "</b>\n<code>{}</code>"
        ),
        "already_authed": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Already authorized</b>"
        ),
        "authed": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Authentication"
            " successful</b>"
        ),
        "deauth": (
            "<tg-emoji emoji-id=5877341274863832725>🚪</tg-emoji> <b>Successfully logged out"
            " of account</b>"
        ),
        "auth": (
            '<tg-emoji emoji-id=5778168620278354602>🔗</tg-emoji> <a href="{}">Follow this'
            " link</a>, allow access, then enter <code>.scode https://...</code> with"
            " the link you received."
        ),
        "no_music": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>No music is playing!</b>"
        ),
        "dl_err": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Failed to download"
            " track.</b>"
        ),
        "volume_changed": (
            "<tg-emoji emoji-id=5890997763331591703>🔊</tg-emoji>"
            " <b>Volume changed to {}%.</b>"
        ),
        "volume_invalid": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Volume level must be"
            " a number between 0 and 100.</b>"
        ),
        "volume_err": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>An error occurred while"
            " changing volume.</b>"
        ),
        "no_volume_arg": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Please specify a"
            " volume level between 0 and 100.</b>"
        ),
        "searching_tracks": (
            "<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <b>Searching for tracks"
            " matching {}...</b>"
        ),
        "no_search_query": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Please specify a"
            " search query.</b>"
        ),
        "no_tracks_found": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>No tracks found for"
            " {}.</b>"
        ),
        "search_results": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Search results for"
            " {}:</b>\n\n{}"
        ),
        "search_results_inline": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Found {count} results"
            " for {query}.</b>\n<b>Select a track:</b>"
        ),
        "downloading_search_track": (
            "<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <b>Downloading {}...</b>"
        ),
        "download_success": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Successfully downloaded {} - {}</b>"
        ),
        "invalid_track_number": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Invalid track number."
            " Please search first or provide a valid number from the list.</b>"
        ),
        "device_list": (
            "<tg-emoji emoji-id=5956561916573782596>📄</tg-emoji> <b>Available devices:</b>\n{}"
        ),
        "no_devices_found": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>No devices found.</b>"
        ),
        "device_changed": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Playback transferred to"
            " {}.</b>"
        ),
        "invalid_device_id": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Invalid device ID."
            " Use</b> <code>.sdevice</code> <b>to see available devices.</b>"
        ),
        "autobio": (
            "<tg-emoji emoji-id=6319076999105087378>🎧</tg-emoji> <b>Spotify autobio {}</b>"
        ),
        "no_ytdlp": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>yt-dlp not found... Check config or install yt-dlp (<code>{}terminal pip install yt-dlp</code>)</b>",
        "snowt_failed": "\n\n<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Download failed</b>",
        "uploading_banner": "\n\n<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <i>Uploading banner...</i>",
        "downloading_track": "\n\n<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <i>Downloading track...</i>",
        "no_playlists": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>No playlists found.</b>",
        "playlists_list": "<tg-emoji emoji-id=5956561916573782596>📄</tg-emoji> <b>Your playlists:</b>\n\n{}",
        "added_to_playlist": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Added {} to {}</b>",
        "removed_from_playlist": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Removed {} from {}</b>",
        "invalid_playlist_index": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Invalid playlist number.</b>",
        "no_cached_playlists": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Use .splaylists first.</b>",
        "playlist_created": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Playlist {} created.</b>",
        "playlist_deleted": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Playlist {} deleted.</b>",
        "no_playlist_name": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Please specify a playlist name.</b>",
    }

    strings_ru = {
        "_cls_doc": "Карточка с играющим треком в Spotify.",
        "need_auth": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Выполни"
            " </b><code>.sauth</code><b> перед выполнением этого действия.</b>"
        ),
        "err": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Произошла ошибка."
            "</b>\n<code>{}</code>"
        ),
        "on-repeat": (
            "<tg-emoji emoji-id=5258420634785947640>🔄</tg-emoji> <b>Включен повтор трека.</b>"
        ),
        "off-repeat": (
            "<tg-emoji emoji-id=5260687119092817530>🔄</tg-emoji> <b>Повтор трека отключён.</b>"
        ),
        "skipped": (
            "<tg-emoji emoji-id=6037622221625626773>➡️</tg-emoji> <b>Трек пропущен.</b>"
        ),
        "playing": "<tg-emoji emoji-id=5773626993010546707>▶️</tg-emoji> <b>Играет...</b>",
        "back": (
            "<tg-emoji emoji-id=6039539366177541657>⬅️</tg-emoji> <b>Переключено на предыдущий трек</b>"
        ),
        "paused": "<tg-emoji emoji-id=5774077015388852135>❌</tg-emoji> <b>Пауза</b>",
        "restarted": (
            "<tg-emoji emoji-id=5843596438373667352>✅️</tg-emoji> <b>Воспроизведение трека с начала...</b>"
        ),
        "liked": (
            "<tg-emoji emoji-id=5258179403652801593>❤️</tg-emoji> <b>Текущий трек добавлен в избранное</b>"
        ),
        "unlike": (
            "<tg-emoji emoji-id=5774077015388852135>❌</tg-emoji> <b>Убрал лайк с текущего трека</b>"
        ),
        "already_authed": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Уже авторизован</b>"
        ),
        "authed": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Успешная аутентификация</b>"
        ),
        "deauth": (
            "<tg-emoji emoji-id=5877341274863832725>🚪</tg-emoji> <b>Успешный выход из аккаунта</b>"
        ),
        "auth": (
            '<tg-emoji emoji-id=5778168620278354602>🔗</tg-emoji> <a href="{}">Пройдите по этой ссылке</a>, разрешите вход, затем введите <code>.scode https://...</code> с ссылкой которую вы получили.'
        ),
        "no_music": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Музыка не играет!</b>"
        ),
        "dl_err": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Не удалось скачать трек.</b>"
        ),
        "volume_changed": (
            "<tg-emoji emoji-id=5890997763331591703>🔊</tg-emoji>"
            " <b>Громкость изменена на {}%.</b>"
        ),
        "volume_invalid": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Уровень громкости должен"
            " быть числом от 0 до 100.</b>"
        ),
        "volume_err": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Произошла ошибка при"
            " изменении громкости.</b>"
        ),
        "no_volume_arg": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Пожалуйста, укажите"
            " уровень громкости от 0 до 100.</b>"
        ),
        "searching_tracks": (
            "<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <b>Идет поиск треков"
            " по запросу {}...</b>"
        ),
        "no_search_query": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Пожалуйста, укажите"
            " поисковый запрос.</b>"
        ),
        "no_tracks_found": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>По запросу '{}'"
            " ничего не найдено.</b>"
        ),
        "search_results": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Результаты поиска"
            " по запросу {}:</b>\n\n{}"
        ),
        "search_results_inline": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Найдено {count} результатов"
            " по запросу {query}.</b>\n<b>Выберите трек:</b>"
        ),
        "downloading_search_track": (
            "<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <b>Скачиваю {}...</b>"
        ),
        "download_success": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Трек {} - {} успешно скачан.</b>"
        ),
        "invalid_track_number": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Некорректный номер трека."
            " Сначала выполните поиск или укажите правильный номер из списка.</b>"
        ),
        "device_list": (
            "<tg-emoji emoji-id=5956561916573782596>📄</tg-emoji> <b>Доступные устройства:</b>\n{}"
        ),
        "no_devices_found": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Устройства не найдены.</b>"
        ),
        "device_changed": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Воспроизведение переключено на"
            " {}.</b>"
        ),
        "invalid_device_id": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Некорректный ID устройства."
            " Используйте</b> <code>.sdevice</code> <b>, чтобы увидеть доступные устройства.</b>"
        ),
        "autobio": (
            "<tg-emoji emoji-id=6319076999105087378>🎧</tg-emoji> <b>Обновление био"
            " включено {}</b>"
        ),
        "no_ytdlp": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>yt-dlp не найден... Проверьте конфиг или установите yt-dlp (<code>{}terminal pip install yt-dlp</code>)</b>",
        "snowt_failed": "\n\n<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Ошибка скачивания.</b>",
        "uploading_banner": "\n\n<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <i>Загрузка баннера...</i>",
        "downloading_track": "\n\n<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <i>Скачивание трека...</i>",
        "no_playlists": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Плейлисты не найдены.</b>",
        "playlists_list": "<tg-emoji emoji-id=5956561916573782596>📄</tg-emoji> <b>Ваши плейлисты:</b>\n\n{}",
        "added_to_playlist": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Трек {} добавлен в {}</b>",
        "removed_from_playlist": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Трек {} удален из {}</b>",
        "invalid_playlist_index": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Неверный номер плейлиста.</b>",
        "no_cached_playlists": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Сначала используйте .splaylists.</b>",
        "playlist_created": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Плейлист {} создан.</b>",
        "playlist_deleted": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Плейлист {} удален.</b>",
        "no_playlist_name": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Пожалуйста, укажите название плейлиста.</b>",
    }

    def __init__(self):
        self._client_id = "e0708753ab60499c89ce263de9b4f57a"
        self._client_secret = "80c927166c664ee98a43a2c0e2981b4a"
        self.sp = None
        self.scope = (
            "user-read-playback-state playlist-read-private playlist-read-collaborative"
            " user-modify-playback-state user-library-modify"
            " playlist-modify-public playlist-modify-private"
        )
        self.sp_auth = spotipy.oauth2.SpotifyOAuth(
            client_id=self._client_id,
            client_secret=self._client_secret,
            redirect_uri="https://thefsch.github.io/spotify/",
            scope=self.scope,
        )
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "show_banner",
                True,
                "Show banner with track info",
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "custom_text",
                (
                    "<tg-emoji emoji-id=6007938409857815902>🎧</tg-emoji> <b>Now playing:</b> {track} — {artists}\n"
                    "<tg-emoji emoji-id=5877465816030515018>🔗</tg-emoji> <b><a href='{songlink}'>song.link</a></b>"
                ),
                "Custom text, supports {track}, {artists}, {album}, {playlist}, {playlist_owner}, {spotify_url}, {songlink}, {progress}, {duration}, {device} placeholders." + "\n\n" + "ℹ️ Custom placeholders: {}".format(utils.config_placeholders()),
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "font",
                "https://raw.githubusercontent.com/kamekuro/assets/master/fonts/Onest-Bold.ttf",
                "Custom font. Specify URL to .ttf file",
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "auto_bio_template",
                "🎧 {title} - {artist}",
                lambda: "Template for Spotify AutoBio, supports {artist}, {title}",
            ),
            loader.ConfigValue(
                "ytdlp_path",
                "",
                "Path to ytdlp binary",
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "cookies_path",
                "",
                "Path to your cookies for yt-dlp",
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "banner_version",
                "horizontal",
                lambda: "Banner version",
                validator=loader.validators.Choice(["horizontal", "vertical", "ultra"]),
            ),
            loader.ConfigValue(
                "blur_intensity",
                40,
                lambda: "Blur intensity",
                validator=loader.validators.Integer(minimum=0),
            ),
        )
        self._sp_store = {}

    def _init_spotify_client(self) -> bool:
        token = self.get("acs_tkn") or {}
        access_token = token.get("access_token")
        if not access_token:
            self.sp = None
            return False

        try:
            self.sp = spotipy.Spotify(auth=access_token)
        except Exception:
            self.sp = None
            return False

        return True

    async def client_ready(self, client, db):
        self.font_ready = asyncio.Event()

        self._premium = getattr(await client.get_me(), "premium", False)
        if not self._init_spotify_client():
            self.set("acs_tkn", None)

        self.bio_task = None

        if self.get("autobio", False) and self.sp:
            await self.autobio()

    def tokenized(func) -> FunctionType:
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            if not args[0].get("acs_tkn", False) or not args[0].sp:
                await utils.answer(args[1], args[0].strings("need_auth"))
                return

            return await func(*args, **kwargs)

        wrapped.__doc__ = func.__doc__
        wrapped.__module__ = func.__module__

        return wrapped

    def error_handler(func) -> FunctionType:
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error in {func.__name__}: {error_msg}")
                
                if "NO_ACTIVE_DEVICE" in error_msg:
                    user_error = "No active device"
                elif "PREMIUM_REQUIRED" in error_msg:
                    user_error = "Spotify Premium is required for this feature"
                elif "Insufficient client scope" in error_msg:
                    user_error = "Insufficient permissions. Please re-authenticate."
                else:
                    user_error = f"{type(e).__name__}: {error_msg[:50]}"
                
                with contextlib.suppress(Exception):
                    await utils.answer(
                        args[1],
                        args[0].strings("err").format(user_error),
                    )

        wrapped.__doc__ = func.__doc__
        wrapped.__module__ = func.__module__

        return wrapped


    async def autobio(self):
        if getattr(self, "bio_task", None) and not self.bio_task.done():
            self.bio_task.cancel()
    
        async def _loop():
            while self.get("autobio", False):
                try:
                    if not self.sp and not self._init_spotify_client():
                        self.set("autobio", False)
                        await self._restore_original_bio()
                        break

                    current_playback = await utils.run_sync(self.sp.current_playback)
    
                    if not current_playback or not current_playback.get("is_playing"):
                        if self.get("last_bio", ""):
                            await self._restore_original_bio(clear_original=False)
                        await asyncio.sleep(10)
                        continue
    
                    item = current_playback.get("item") or {}
                    title = item.get("name") or ""
                    artists = ", ".join(
                        [a.get("name", "") for a in item.get("artists", []) if a.get("name")]
                    )
    
                    if not title:
                        await asyncio.sleep(10)
                        continue
    
                    bio = self.config["auto_bio_template"].format(
                        title=title,
                        artist=artists or "Unknown Artist",
                    ).strip()
    
                    if len(bio) > 70:
                        bio = bio[:69] + "…"
    
                    if bio != self.get("last_bio", ""):
                        await self._client(UpdateProfileRequest(about=bio))
                        self.set("last_bio", bio)
    
                except FloodWaitError as e:
                    await asyncio.sleep(getattr(e, "seconds", 30) + 1)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.exception("autobio error: %s", e)
    
                await asyncio.sleep(self.config.get("BIO_UPDATE_DELAY", 30))

        self.bio_task = asyncio.create_task(_loop())

    async def _get_current_about(self) -> str:
        full_user = await self._client(GetFullUserRequest("me"))
        return getattr(full_user.full_user, "about", "") or ""

    async def _restore_original_bio(
        self,
        *,
        clear_original: bool = True,
        clear_last: bool = True,
    ):
        original_bio = self.get("original_bio", None)
        if original_bio is None:
            return

        await self._client(UpdateProfileRequest(about=original_bio))
        if clear_original:
            self.set("original_bio", None)
        if clear_last:
            self.set("last_bio", "")
    
    def _get_chat_id(self, target):
        if isinstance(target, int):
            return target
        if not target:
            return None
        chat_id = getattr(target, "chat_id", None)
        if chat_id:
            return chat_id
        with contextlib.suppress(Exception):
            return utils.get_chat_id(target)
        return None

    def _reply_id(self, message):
        reply_to_id = getattr(message, "reply_to_msg_id", None)
        if reply_to_id:
            return reply_to_id
        reply_to = getattr(message, "reply_to", None)
        return getattr(reply_to, "reply_to_msg_id", None) if reply_to else None

    async def _download_track(
        self,
        target,
        query,
        caption=None,
        track_name=None,
        artists=None,
        log_context=None,
        reply_to_id=None,
    ) -> bool:
        dl_dir = os.path.join(os.getcwd(), "spotifymod")
        if not os.path.exists(dl_dir):
            os.makedirs(dl_dir, exist_ok=True)

        for f in os.listdir(dl_dir):
            try:
                os.remove(os.path.join(dl_dir, f))
            except Exception:
                pass

        success = False
        if caption is None:
            safe_track = utils.escape_html(track_name or "Unknown")
            safe_artists = utils.escape_html(artists or "Unknown Artist")
            caption = self.strings["download_success"].format(safe_track, safe_artists)

        async def send_text(text: str) -> bool:
            if target is None:
                return False
            if isinstance(target, int):
                await self._client.send_message(target, text, reply_to=reply_to_id)
                return True
            try:
                await utils.answer(target, text)
                return True
            except Exception:
                chat_id = self._get_chat_id(target)
                if chat_id is None:
                    return False
                await self._client.send_message(chat_id, text, reply_to=reply_to_id)
                return True

        async def send_file(file_path: str) -> bool:
            if target is None:
                return False
            if isinstance(target, int):
                await self._client.send_file(
                    target,
                    file_path,
                    caption=caption,
                    reply_to=reply_to_id,
                )
                return True
            try:
                await utils.answer(target, caption, file=file_path)
                return True
            except Exception:
                chat_id = self._get_chat_id(target)
                if chat_id is None:
                    return False
                await self._client.send_file(
                    chat_id,
                    file_path,
                    caption=caption,
                    reply_to=reply_to_id,
                )
                return True

        try:
            squery = query.replace('"', '').replace("'", "")

            cookies = self.config["cookies_path"]
            
            if cookies:
                cmd = (
                    f'{self.config["ytdlp_path"]} -x --impersonate="" --cookies {cookies} --audio-format mp3 --add-metadata '
                    f'--audio-quality 0 -o "{dl_dir}/%(title)s [%(id)s].%(ext)s" '
                    f'"ytsearch1:{squery}"'
                )
            else:
                cmd = (
                    f'{self.config["ytdlp_path"]} -x --impersonate="" --audio-format mp3 --add-metadata '
                    f'--audio-quality 0 -o "{dl_dir}/%(title)s [%(id)s].%(ext)s" '
                    f'"ytsearch1:{squery}"'
                )

            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await proc.communicate()
            if proc.returncode and log_context:
                err_text = stderr.decode(errors="ignore").strip() if stderr else ""
                err_text = err_text[-400:] if err_text else "yt-dlp failed"
                logger.error("Search download failed (%s): %s", log_context, err_text)

            files = [f for f in os.listdir(dl_dir) if f.endswith(".mp3")]

            if files:
                first = files[0]
                target_file = os.path.join(dl_dir, first)
                success = await send_file(target_file)
                if not success:
                    if log_context:
                        logger.error(
                            "Search download send failed (%s). target=%s chat_id=%s",
                            log_context,
                            type(target).__name__,
                            self._get_chat_id(target),
                        )
                    await send_text(self.strings["dl_err"])
            else:
                if log_context:
                    logger.error("Search download produced no files (%s)", log_context)
                await send_text(self.strings["snowt_failed"])

        except Exception as e:
            if log_context:
                logger.exception("Search download error (%s)", log_context)
            else:
                logger.error(e)
            await send_text(self.strings["dl_err"])

        finally:
            if os.path.exists(dl_dir):
                for f in os.listdir(dl_dir):
                    try:
                        os.remove(os.path.join(dl_dir, f))
                    except Exception:
                        pass

        return success

    def _short_text(self, text: str, limit: int = 60) -> str:
        text = " ".join(text.split())
        if len(text) <= limit:
            return text
        if limit <= 3:
            return text[:limit]
        return text[: limit - 3] + "..."



    def _track_info(self, track_info) -> tuple:
        if isinstance(track_info, dict):
            track_name = track_info.get("name", "Unknown")
            artists_list = [
                a.get("name") for a in track_info.get("artists", []) if a.get("name")
            ]
            artists = ", ".join(artists_list) if artists_list else "Unknown Artist"
            return track_name, artists

        if isinstance(track_info, (list, tuple)):
            track_name = track_info[0] if len(track_info) > 0 else "Unknown"
            artists = track_info[1] if len(track_info) > 1 else "Unknown Artist"
            if not artists:
                artists = "Unknown Artist"
            return track_name or "Unknown", artists

        return "Unknown", "Unknown Artist"

    def _search_keyboard(self, tracks: list, chat_id=None, reply_to_id=None) -> list:
        keyboard = []
        for track in tracks:
            track_name, artists = self._track_info(track)
            label = f"{track_name} — {artists}" if artists else track_name
            keyboard.append(
                [
                    {
                        "text": self._short_text(label),
                        "callback": self._inline_download_track,
                        "args": (track_name, artists, reply_to_id, chat_id),
                    }
                ]
            )

        return keyboard

    async def _inline_download_track(
        self,
        call,
        track_name: str,
        artists: str,
        reply_to_id=None,
        chat_id=None,
    ):
        track_name = track_name or "Unknown"
        artists = artists or "Unknown Artist"

        with contextlib.suppress(Exception):
            await call.answer()

        with contextlib.suppress(Exception):
            await call.edit(self.strings["downloading_track"].lstrip(), reply_markup=None)

        target_message = getattr(call, "message", None)
        if reply_to_id is None:
            reply_to_id = self._reply_id(target_message)

        if chat_id is None:
            chat_id = self._get_chat_id(target_message)
        if chat_id is None:
            chat_id = getattr(call, "chat_id", None)
        if chat_id is None:
            chat_id = self._get_chat_id(call)

        if chat_id is None and target_message is None:
            logger.error("Inline download missing chat_id (%s - %s)", track_name, artists)
            with contextlib.suppress(Exception):
                await call.edit(self.strings["dl_err"], reply_markup=None)
            return

        target = chat_id if chat_id is not None else target_message

        success = await self._download_track(
            target,
            f"{artists} {track_name}",
            track_name=track_name,
            artists=artists,
            log_context=f"{track_name} - {artists}",
            reply_to_id=reply_to_id,
        )

        if success:
            with contextlib.suppress(Exception):
                await call.delete()
        else:
            with contextlib.suppress(Exception):
                await call.edit(self.strings["dl_err"], reply_markup=None)

    async def _inline_search_tracks(self, query):
        if not self.get("acs_tkn", False) or not self.sp:
            return {
                "title": "Auth required",
                "description": "Run .sauth",
                "message": self.strings["need_auth"],
            }

        query_text = (query.args or "").strip()
        if not query_text:
            return {
                "title": "No query",
                "description": "Provide search query",
                "message": self.strings["no_search_query"],
            }

        try:
            results = await asyncio.to_thread(
                self.sp.search,
                q=query_text,
                limit=5,
                type="track",
            )
        except Exception as e:
            return {
                "title": "Search error",
                "description": "Try again",
                "message": self.strings["err"].format(
                    utils.escape_html(str(e)[:50])
                ),
            }

        if not results or not results["tracks"]["items"]:
            return {
                "title": "No results",
                "description": self._short_text(query_text, limit=60),
                "message": self.strings["no_tracks_found"].format(
                    utils.escape_html(query_text)
                ),
            }

        tracks = results["tracks"]["items"]
        store_id = id(tracks)
        self._sp_store[store_id] = [(t.get("name", "Unknown"), ", ".join(a.get("name", "") for a in t.get("artists", []) if a.get("name")) or "Unknown Artist") for t in tracks]
        
        entries = []
        for i, track in enumerate(tracks):
            track_name, artists = self._track_info(track)
            cover_list = track.get("album", {}).get("images", [])
            thumb = cover_list[0]["url"] if cover_list else None

            entries.append(
                {
                    "title": self._short_text(track_name, limit=60),
                    "description": self._short_text(artists, limit=60) if artists else "",
                    "message": f"{self.strings["downloading_track"].lstrip()}\n<i>spdl_{store_id}_{i}</i>",
                    "thumb": thumb,
                }
            )

        return entries

    @loader.inline_handler(ru_doc="<запрос> - поиск треков Spotify.")
    async def sq(self, query):
        """<query> - search Spotify track"""
        return await self._inline_search_tracks(query)

    @loader.inline_handler(ru_doc="<запрос> - поиск треков Spotify.")
    async def ssearch(self, query):
        """<query> - search Spotify track"""
        return await self._inline_search_tracks(query)
                         
    @error_handler
    @tokenized
    @loader.command(
        ru_doc="| .spla - ➕ Добавить текущий трек в плейлист (используйте номер из .splaylists | .spls)",
        alias="spla"
    )
    async def splaylistadd(self, message: Message):
        """| .spla - ➕ Add current track to playlist (use number from .splaylists | .spls)"""
        args = utils.get_args_raw(message)
        if not args or not args.isdigit():
            await utils.answer(message, self.strings["invalid_playlist_index"])
            return
        
        index = int(args) - 1
        playlists = self.get("last_playlists", [])
        
        if not playlists:
            await utils.answer(message, self.strings["no_cached_playlists"])
            return
        if index < 0 or index >= len(playlists):
            await utils.answer(message, self.strings["invalid_playlist_index"])
            return
            
        current = self.sp.current_playback()
        if not current or not current.get("item"):
            await utils.answer(message, self.strings["no_music"])
            return
            
        track_uri = current["item"]["uri"]
        track_name = current["item"]["name"]
        artists = ", ".join([a["name"] for a in current["item"]["artists"]])
        full_track_name = f"{artists} - {track_name}"
        
        playlist_id = playlists[index]["id"]
        playlist_name = playlists[index]["name"]
        
        self.sp.playlist_add_items(playlist_id, [track_uri])
        await utils.answer(message, self.strings["added_to_playlist"].format(utils.escape_html(full_track_name), utils.escape_html(playlist_name)))

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="| .splr - ➖ Удалить текущий трек из плейлиста (используйте номер из .splaylists | .spls)",
        alias="splr"
    )
    async def splaylistrem(self, message: Message):
        """| .splr - ➖ Remove current track from playlist (use number from .splaylists | .spls)"""
        args = utils.get_args_raw(message)
        if not args or not args.isdigit():
            await utils.answer(message, self.strings["invalid_playlist_index"])
            return
        
        index = int(args) - 1
        playlists = self.get("last_playlists", [])
        
        if not playlists:
            await utils.answer(message, self.strings["no_cached_playlists"])
            return
        if index < 0 or index >= len(playlists):
            await utils.answer(message, self.strings["invalid_playlist_index"])
            return
            
        current = self.sp.current_playback()
        if not current or not current.get("item"):
            await utils.answer(message, self.strings["no_music"])
            return
            
        track_uri = current["item"]["uri"]
        track_name = current["item"]["name"]
        artists = ", ".join([a["name"] for a in current["item"]["artists"]])
        full_track_name = f"{artists} - {track_name}"
        
        playlist_id = playlists[index]["id"]
        playlist_name = playlists[index]["name"]
        
        self.sp.playlist_remove_all_occurrences_of_items(playlist_id, [track_uri])
        await utils.answer(message, self.strings["removed_from_playlist"].format(utils.escape_html(full_track_name), utils.escape_html(playlist_name)))

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="| .splc - 🆕 Создать новый плейлист",
        alias="splc"
    )
    async def splaylistcreate(self, message: Message):
        """| .splc - 🆕 Create a new playlist"""
        name = utils.get_args_raw(message)
        if not name:
            await utils.answer(message, self.strings["no_playlist_name"])
            return
        
        user_id = self.sp.me()["id"]
        self.sp.user_playlist_create(user_id, name)
        await utils.answer(message, self.strings["playlist_created"].format(utils.escape_html(name)))

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="| .spld - 🗑 Удалить плейлист (используйте номер из .splaylists | .spls)",
        alias="spld"
    )
    async def splaylistdelete(self, message: Message):
        """| .spld - 🗑 Delete playlist (use number from .splaylists | .spls)"""
        args = utils.get_args_raw(message)
        if not args or not args.isdigit():
            await utils.answer(message, self.strings["invalid_playlist_index"])
            return
        
        index = int(args) - 1
        playlists = self.get("last_playlists", [])
        
        if not playlists:
            await utils.answer(message, self.strings["no_cached_playlists"])
            return
        if index < 0 or index >= len(playlists):
            await utils.answer(message, self.strings["invalid_playlist_index"])
            return
            
        playlist_id = playlists[index]["id"]
        playlist_name = playlists[index]["name"]
        
        self.sp.current_user_unfollow_playlist(playlist_id)
        await utils.answer(message, self.strings["playlist_deleted"].format(utils.escape_html(playlist_name)))

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="| .spls - 📃 Получить все плейлисты",
        alias="spls"
    )
    async def splaylists(self, message: Message):
        """| .spls - 📃 Get all playlists"""
        user_id = self.sp.me()["id"]
        playlists = self.sp.current_user_playlists()
        
        editable_playlists = [
            p for p in playlists["items"] 
            if p["owner"]["id"] == user_id or p["collaborative"]
        ]
        
        self.set("last_playlists", editable_playlists)

        playlist_list_text = ""
        for i, playlist in enumerate(editable_playlists):
            name = utils.escape_html(playlist["name"])
            url = playlist["external_urls"]["spotify"]
            count = playlist["tracks"]["total"]
            playlist_list_text += f"<b>{i + 1}.</b> <a href='{url}'>{name}</a> ({count} tracks)\n"

        if playlist_list_text == "":
            await utils.answer(message, self.strings["no_playlists"])
        else:
            await utils.answer(message, self.strings["playlists_list"].format(playlist_list_text))

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="- ℹ️ Переключить стриминг воспроизведения в био"
    )
    async def sbiocmd(self, message):
        """- ℹ️ Toggle streaming playback in bio"""
        if not getattr(self, "sp", None):
            await utils.answer(message, self.strings["need_auth"])
            return
    
        state = not self.get("autobio", False)
        self.set("autobio", state)
    
        if state:
            self.set("original_bio", await self._get_current_about())
            self.set("last_bio", "")
            await self.autobio()
        else:
            task = getattr(self, "bio_task", None)
            if task and not task.done():
                task.cancel()
            self.bio_task = None
            await self._restore_original_bio()
    
        await utils.answer(
            message,
            self.strings["autobio"].format("on" if state else "off"),
        )

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="| .sv - 🔊 Изменить громкость. .svolume | .sv <0-100>",
        alias="sv"
    )
    async def svolume(self, message: Message):
        """| .sv - 🔊 Change playback volume. .svolume | .sv <0-100>"""
        args = utils.get_args_raw(message)
        if args == "":
            await utils.answer(message, self.strings["no_volume_arg"])
        else:
            try:
                volume_percent = int(args)
                if 0 <= volume_percent <= 100:
                    self.sp.volume(volume_percent)
                    await utils.answer(message, self.strings["volume_changed"].format(volume_percent))
                else:
                    await utils.answer(message, self.strings["volume_invalid"])
            except ValueError:
                await utils.answer(message, self.strings["volume_invalid"])

    @error_handler
    @tokenized
    @loader.command(
        ru_doc=(
            "| .sd - 🎵 Выбрать устройство для воспроизведения. Например: .sdevice <ID устройства>или .sdevice | .sd для вывода списка устройств"
        ),
        alias="sd"
    )
    async def sdevicecmd(self, message: Message):
        """| .sd - 🎵 Set preferred playback device. Usage: .sdevice <device_id> or .sdevice | .sd to list devices"""
        args = utils.get_args_raw(message)
        devices = self.sp.devices()["devices"]

        if args == "":
            if not devices:
                await utils.answer(message, self.strings["no_devices_found"])
            else:
                device_list_text = ""
                for i, device in enumerate(devices):
                    is_active = "(active)" if device["is_active"] else ""
                    device_list_text += (
                        f"<b>{i+1}.</b> {device['name']}"
                        f" ({device['type']}) {is_active}\n"
                    )
                await utils.answer(message, self.strings["device_list"].format(device_list_text.strip()))
        else:
            device_id = None
            try:
                device_number = int(args)
                if 0 < device_number <= len(devices):
                    device_id = devices[device_number - 1]["id"]
                    device_name = devices[device_number - 1]["name"]
                else:
                    await utils.answer(message, self.strings["invalid_device_id"])
                    return
            except ValueError:
                found_device = next((d for d in devices if d["id"] == args.strip()), None)
                if found_device:
                    device_id = found_device["id"]
                    device_name = found_device["name"]
                else:
                    await utils.answer(message, self.strings["invalid_device_id"])
                    return

            self.sp.transfer_playback(device_id=device_id)
            await utils.answer(message, self.strings["device_changed"].format(device_name))
            
    @error_handler
    @tokenized
    @loader.command(
        ru_doc="- 💫 Включить повтор трека"
    )
    async def srepeatcmd(self, message: Message):
        """- 💫 Repeat"""
        self.sp.repeat("track")
        await utils.answer(message, self.strings["on-repeat"])

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="- ✋ Остановить повтор"
    )
    async def sderepeatcmd(self, message: Message):
        """- ✋ Stop repeat"""
        self.sp.repeat("context")
        await utils.answer(message, self.strings["off-repeat"])

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="- 👉 Следующий трек"
    )
    async def snextcmd(self, message: Message):
        """- 👉 Next track"""
        self.sp.next_track()
        await utils.answer(message, self.strings["skipped"])

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="- 🤚 Продолжить воспроизведение"
    )
    async def sresumecmd(self, message: Message):
        """- 🤚 Resume"""
        self.sp.start_playback()
        await utils.answer(message, self.strings["playing"])

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="- 🤚 Пауза"
    )
    async def spausecmd(self, message: Message):
        """- 🤚 Pause"""
        self.sp.pause_playback()
        await utils.answer(message, self.strings["paused"])

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="- ⏮ Предыдущий трек"
    )
    async def sbackcmd(self, message: Message):
        """- ⏮ Previous track"""
        self.sp.previous_track()
        await utils.answer(message, self.strings["back"])

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="- ⏪ Перезапустить трек"
    )
    async def sbegincmd(self, message: Message):
        """- ⏪ Restart track"""
        self.sp.seek_track(0)
        await utils.answer(message, self.strings["restarted"])

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="- ❤️ Лайкнуть играющий трек"
    )
    async def slikecmd(self, message: Message):
        """- ❤️ Like current track"""
        cupl = self.sp.current_playback()
        self.sp.current_user_saved_tracks_add([cupl["item"]["id"]])
        await utils.answer(message, self.strings["liked"])
    
    @error_handler
    @tokenized
    @loader.command(
        ru_doc="- 💔 Убрать лайк с играющего трека"
    )
    async def sunlikecmd(self, message: Message):
        """- 💔 Unlike current track"""
        cupl = self.sp.current_playback()
        self.sp.current_user_saved_tracks_delete([cupl["item"]["id"]])
        await utils.answer(message, self.strings["unlike"])

    @error_handler
    @loader.command(
        ru_doc="- Получить ссылку для авторизации"
    )
    async def sauthcmd(self, message: Message):
        """- Get authorization link"""
        if self.get("acs_tkn", False) and not self.sp:
            await utils.answer(message, self.strings["already_authed"])
        else:
            self.sp_auth.get_authorize_url()
            await utils.answer(
                message,
                self.strings["auth"].format(self.sp_auth.get_authorize_url()),
            )

    @error_handler
    @loader.command(
        ru_doc="- Вставить код авторизации"
    )
    async def scodecmd(self, message: Message):
        """- Paste authorization code"""
        url = message.message.split(" ")[1]
        code = self.sp_auth.parse_auth_response_url(url)
        self.set("acs_tkn", self.sp_auth.get_access_token(code, True, False))
        self._init_spotify_client()
        await utils.answer(message, self.strings["authed"])

    @error_handler
    @loader.command(
        ru_doc="- Выйти из аккаунта"
    )
    async def unauthcmd(self, message: Message):
        """- Log out of account"""
        self.set("acs_tkn", None)
        self.sp = None
        await utils.answer(message, self.strings["deauth"])

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="| .stokr - Обновить токен авторизации",
        alias="stokr"
    )
    async def stokrefreshcmd(self, message: Message):
        """| .stokr - Refresh authorization token"""
        self.set(
            "acs_tkn",
            self.sp_auth.refresh_access_token(self.get("acs_tkn")["refresh_token"]),
        )
        self.set("NextRefresh", time.time() + 45 * 60)
        self._init_spotify_client()
        await utils.answer(message, self.strings["authed"])

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="| .sn - 🎧 Показать карточку играющего трека",
        alias="sn"
    )
    async def snowcmd(self, message: Message):
        """| .sn - 🎧 View current track card."""
        current_playback = self.sp.current_playback()
        if not current_playback or not current_playback.get("is_playing", False):
            await utils.answer(message, self.strings["no_music"])
            return

        track = current_playback["item"]["name"]
        track_id = current_playback["item"]["id"]
        artists = ", ".join([a["name"] for a in current_playback["item"]["artists"]])
        album_name = current_playback["item"]["album"].get("name", "Unknown Album")
        duration_ms = current_playback["item"].get("duration_ms", 0)
        progress_ms = current_playback.get("progress_ms", 0)

        duration = f"{duration_ms//1000//60}:{duration_ms//1000%60:02}"
        progress = f"{progress_ms//1000//60}:{progress_ms//1000%60:02}"

        spotify_url = f"https://open.spotify.com/track/{track_id}"
        songlink = f"https://song.link/s/{track_id}"

        try:
            device_raw = (
                current_playback["device"]["name"]
                + " "
                + current_playback["device"]["type"].lower()
            )
            device = device_raw.replace("computer", "").replace("smartphone", "").strip()
        except Exception:
            device = None

        try:
            playlist_id = current_playback["context"]["uri"].split(":")[-1]
            playlist = self.sp.playlist(playlist_id)
            playlist_name = playlist.get("name", None)
            try:
                playlist_owner = (
                    f'<a href="https://open.spotify.com/user/{playlist["owner"]["id"]}">'
                    f'{playlist["owner"]["display_name"]}</a>'
                )
            except KeyError:
                playlist_owner = playlist.get("owner", {}).get("display_name", "")
        except Exception:
            playlist_name = ""
            playlist_owner = ""

        sdata = {
            "track": utils.escape_html(track),
            "artists": utils.escape_html(artists),
            "album": utils.escape_html(album_name),
            "duration": duration,
            "progress": progress,
            "device": device,
            "spotify_url": spotify_url,
            "songlink": songlink,
            "playlist": utils.escape_html(playlist_name) if playlist_name else "",
            "playlist_owner": playlist_owner or "",
        }
        
        data = await utils.get_placeholders(sdata, self.config["custom_text"])
        
        text = self.config["custom_text"].format(**data)

        if self.config["show_banner"]:
            cover_url = current_playback["item"]["album"]["images"][0]["url"]

            tmp_msg = await utils.answer(message, text + self.strings["uploading_banner"])

            banners = Banners(
                title=track,
                artists=artists,
                duration=duration_ms,
                progress=progress_ms,
                track_cover=requests.get(cover_url).content,
                font=self.config["font"],
                blur=self.config["blur_intensity"],
                album_title=album_name,
                meta_info="Spotify",
            )

            version = self.config["banner_version"]
            if version == "ultra":
                file = banners.ultra()
            elif version == "vertical":
                file = banners.vertical()
            else:
                file = banners.horizontal()
            
            await utils.answer(tmp_msg, text, file=file)
        else:
            await utils.answer(message, text)

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="| .snt - 🎧 Скачать играющий трек",
        alias="snt"
    )
    async def snowtcmd(self, message: Message):
        """| .snt - 🎧 Download current track."""
        current_playback = self.sp.current_playback()
        if not current_playback or not current_playback.get("is_playing", False):
            await utils.answer(message, self.strings["no_music"])
            return

        track = current_playback["item"]["name"]
        artists = ", ".join([a["name"] for a in current_playback["item"]["artists"]])
        album_name = current_playback["item"]["album"].get("name", "Unknown Album")
        duration_ms = current_playback["item"].get("duration_ms", 0)
        progress_ms = current_playback.get("progress_ms", 0)

        duration = f"{duration_ms//1000//60}:{duration_ms//1000%60:02}"
        progress = f"{progress_ms//1000//60}:{progress_ms//1000%60:02}"

        spotify_url = f"https://open.spotify.com/track/{current_playback['item']['id']}"
        songlink = f"https://song.link/s/{current_playback['item']['id']}"

        try:
            device_raw = (
                current_playback["device"]["name"]
                + " "
                + current_playback["device"]["type"].lower()
            )
            device = device_raw.replace("computer", "").replace("smartphone", "").strip()
        except Exception:
            device = None

        try:
            playlist_id = current_playback["context"]["uri"].split(":")[-1]
            playlist = self.sp.playlist(playlist_id)
            playlist_name = playlist.get("name", None)
            try:
                playlist_owner = (
                    f'<a href="https://open.spotify.com/user/{playlist["owner"]["id"]}">'
                    f'{playlist["owner"]["display_name"]}</a>'
                )
            except KeyError:
                playlist_owner = playlist.get("owner", {}).get("display_name", "")
        except Exception:
            playlist_name = ""
            playlist_owner = ""

        sdata = {
            "track": utils.escape_html(track),
            "artists": utils.escape_html(artists),
            "album": utils.escape_html(album_name),
            "duration": duration,
            "progress": progress,
            "device": device,
            "spotify_url": spotify_url,
            "songlink": songlink,
            "playlist": utils.escape_html(playlist_name) if playlist_name else "",
            "playlist_owner": playlist_owner or "",
        }
        
        data = await utils.get_placeholders(sdata, self.config["custom_text"])
        
        text = self.config["custom_text"].format(**data)

        msg = await utils.answer(message, text + self.strings["downloading_track"])
        
        await self._download_track(msg, f"{artists} {track}", caption=text)

    @error_handler
    @tokenized
    @loader.command(
        ru_doc="| .sq - 🔍 Поиск треков.",
        alias="sq"
    )
    async def ssearchcmd(self, message: Message):
        """| .sq - 🔍 Search for tracks."""
        args = utils.get_args_raw(message)
        if not args:
            await utils.answer(message, self.strings["no_search_query"])
            return

        search_results = self.get("last_search_results", [])
        
        is_selection = False
        if args.isdigit():
            track_number = int(args)
            if search_results and 0 < track_number <= len(search_results):
                is_selection = True
        
        if is_selection:
            track_number = int(args)
            msg = await utils.answer(message, self.strings["downloading_track"])
            track_info = search_results[track_number - 1]
            track_name, artists = self._track_info(track_info)
            reply_to_id = self._reply_id(message)

            chat_id = self._get_chat_id(message)
            target = chat_id if chat_id is not None else msg
            success = await self._download_track(
                target,
                f"{artists} {track_name}",
                track_name=track_name,
                artists=artists,
                log_context=f"{track_name} - {artists}",
                reply_to_id=reply_to_id,
            )
            if success:
                with contextlib.suppress(Exception):
                    await msg.delete()
            self.set("last_search_results", [])
                
        else:
            results = await asyncio.to_thread(
                self.sp.search,
                q=args,
                limit=5,
                type="track",
            )

            if not results or not results["tracks"]["items"]:
                await utils.answer(message, self.strings["no_tracks_found"].format(args))
                return

            tracks = results["tracks"]["items"]
            self.set("last_search_results", tracks)

            reply_to_id = self._reply_id(message)

            await self.inline.form(
                self.strings["search_results_inline"].format(
                    count=len(tracks),
                    query=utils.escape_html(args),
                ),
                message=message,
                reply_markup=self._search_keyboard(
                    tracks,
                    self._get_chat_id(message),
                    reply_to_id,
                ),
            )

    async def watcher(self, message: Message):
        """Watcher is used to update token"""
        if not self.sp:
            return

        raw = getattr(message, "raw_text", "") or ""
        if "spdl_" in raw:
            try:
                tag = raw.split("spdl_")[1].split("</i>")[0]
                sid, idx = tag.split("_")
                store_id, index = int(sid), int(idx)
            except:
                return
            
            data = self._sp_store.pop(store_id, [])
            if not data or index >= len(data):
                return
            
            track_name, artists = data[index]
            chat_id = self._get_chat_id(message)
            if not chat_id:
                return
            
            reply_to_id = self._reply_id(message)
            success = await self._download_track(
                chat_id, f"{artists} {track_name}",
                track_name=track_name, artists=artists,
                log_context=f"{track_name} - {artists}",
                reply_to_id=reply_to_id,
            )
            if success:
                with contextlib.suppress(Exception):
                    await message.delete()
            return

        next_refresh = self.get("NextRefresh")
        if not next_refresh or next_refresh < time.time():
            acs_tkn = self.get("acs_tkn")
            if not acs_tkn or not acs_tkn.get("refresh_token"):
                self.set("NextRefresh", time.time() + 300)
                return
            try:
                new_token = self.sp_auth.refresh_access_token(acs_tkn["refresh_token"])
                self.set("acs_tkn", new_token)
                self.set("NextRefresh", time.time() + 45 * 60)
                if new_token and new_token.get("access_token"):
                    self.sp = spotipy.Spotify(auth=new_token["access_token"])
            except Exception as e:
                logger.error(f"Spotify watcher error: {e}")
                if "Refresh token revoked" in str(e):
                    refresh_token = await self.invoke("stokrefresh", "", self.inline.bot.id)
                    await refresh_token.delete()
                else:
                    self.set("NextRefresh", time.time() + 300)