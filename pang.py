#!/usr/bin/env python3
"""Pang Deluxe — block art + box drawing + sparkles + powerups + chiptune.

Controles:
  Flechas o A/D  : moverse
  Espacio        : disparar arpón
  M              : mute / unmute
  R              : reiniciar tras game over
  Q              : salir
"""

import copy
import curses
import hashlib
import json
import locale
import math
import os
import random
import time
from collections import deque
from pathlib import Path

locale.setlocale(locale.LC_ALL, '')

try:
    import pang_audio
    audio = pang_audio.make_audio()
    gameplay_song = pang_audio.gameplay_song_for
except Exception as _audio_err:
    import sys, traceback
    print(f"[pang] audio disabled: {_audio_err}", file=sys.stderr)
    if os.environ.get('PANG_AUDIO_DEBUG'):
        traceback.print_exc()
    class _NullAudio:
        muted = False
        def start(self): return False
        def shutdown(self): pass
        def play_music(self, n): pass
        def stop_music(self): pass
        def sfx(self, n, **kw): pass
        def toggle_mute(self): pass
        def is_muted(self): return False
        def is_ok(self): return False
    audio = _NullAudio()
    def gameplay_song(level): return 'GAMEPLAY_A'

# --- Tuning -----------------------------------------------------------------
GRAVITY        = 22.0
PLAYER_STEP    = 2
HARPOON_SPEED  = 55.0
TARGET_FPS     = 60
TICK           = 1.0 / TARGET_FPS

POWERUP_DROP_CHANCE = 0.32
POWERUP_FALL_SPEED  = 7.5
POWERUP_LIFETIME    = 14.0

# Weapon system
PROJ_HARPOON      = 'harpoon'
PROJ_STICKY       = 'sticky'    # sticks to ceiling as a barrier
PROJ_BULLET       = 'bullet'    # fast pellet from the power gun
STICKY_LIFE       = 8.0         # seconds a stuck barrier persists
BULLET_SPEED_MULT = 1.7
PIERCE_COOLDOWN   = 0.10        # avoid instant rehits on split children
MAGNET_PULL       = 5.0         # pull speed multiplier toward player

BALL_RADIUS = {4: 3.2, 3: 2.2, 2: 1.3, 1: 0.7}
SPLIT_VX    = {4: 10.0, 3: 11.0, 2: 12.0, 1: 13.0}
SPLIT_VY    = {4: -19.0, 3: -17.0, 2: -15.0, 1: -13.0}
POINTS      = {4: 50, 3: 100, 2: 200, 1: 400}
BALL_COLOR  = {4: 3, 3: 6, 2: 5, 1: 7}

BALL_SPRITES = {
    4: ([" ▄███▄ ", "███████", "███████", "███████", " ▀███▀ "], 3, 2,
        ['BOLD', 'BOLD', 'NORM', 'DIM', 'DIM']),
    3: (["▄███▄", "█████", "▀███▀"], 2, 1, ['BOLD', 'NORM', 'DIM']),
    2: (["▟█▙", "▜█▛"], 1, 0, ['BOLD', 'DIM']),
    1: (["●"], 0, 0, ['BOLD']),
}

# --- Player sprite (5 cols × 5 rows, animated, chunky block adventurer) ---
# Row 0 = hat, Row 1 = face, Row 2 = arms+chest, Row 3 = waist, Row 4 = legs.
# Wider canvas gives the silhouette real mass: brimmed cap, two-eyed visor,
# broad shoulders, tapered waist, chunky legs. Quadrant blocks bevel the
# corners so the body reads as a cartoon hero, not a stick figure. All cells
# stay single-column wide in monospace.
HAT_IDLE_A = "▗▟█▙▖"   # wide-brim peaked cap
HAT_IDLE_B = " ▟█▙ "   # brim drawn in (breath cycle)
HAT_TILT_L = " ▟█▙▖"   # trailing back-right (moving left)
HAT_TILT_R = "▗▟█▙ "   # trailing back-left  (moving right)
HAT_FIRE   = "▗▟█▙▖"
HAT_HIT    = "▗▞█▚▖"   # diagonals scrambled — knocked askew

FACE_IDLE   = "▐o o▌"  # visor + two eyes
FACE_BLINK  = "▐- -▌"
FACE_LEFT   = "▐< <▌"
FACE_RIGHT  = "▐> >▌"
FACE_HIT    = "▐x x▌"  # KO eyes
FACE_HAPPY  = "▐^ ^▌"  # smile eyes
FACE_AIM    = "▐• •▌"  # focused

ARMS_IDLE_A = "╱▐█▌╲"  # arms out around solid chest
ARMS_IDLE_B = "┃▐█▌┃"  # arms straight at sides (breath)
ARMS_FIRE_L = "◀▐█▌╲"  # gun pointer left, right arm idle
ARMS_FIRE_R = "╱▐█▌▶"  # left arm idle, gun pointer right
ARMS_HIT    = "╲▐▓▌╱"  # arms flung + damaged chest tone

TORSO_IDLE  = " ▟█▙ "  # tapered waist
TORSO_FIRE  = " ▟▓▙ "  # tense (mid-tone)
TORSO_HIT   = " ▟░▙ "  # damaged (faded)

LEGS_STAND  = " █ █ "  # two chunky legs, gap between
LEGS_PLANT  = " ▙ ▟ "  # boots planted, firing stance
LEGS_WALK_A = " ▙ ╲ "  # left boot planted, right leg striding
LEGS_WALK_B = " ╱ ▟ "  # right boot planted, left leg striding

PLAYER_ATTRS    = ['BOLD', 'BOLD', 'BOLD', 'BOLD', 'BOLD']
PLAYER_CX       = 2     # 5-col sprite: center is col 2
PLAYER_CY       = 3
PLAYER_HALF_W   = 1.5   # hitbox stays tight — outer cols are forgiveness margin
PLAYER_TOP_REACH = 2.0   # hat is decoration; the face row is the top hitbox
PLAYER_BOT_REACH = 1.0

POWERUP_BOX_TOP = "┏━━┓"
POWERUP_BOX_BOT = "┗━━┛"

POWERUPS = {
    'L': ("+1", 5, "EXTRA LIFE",   0.0),
    'D': ("2x", 2, "DOUBLE SHOT", 10.0),
    'S': ("SL", 7, "SLOW-MO",      8.0),
    'F': ("FZ", 1, "FREEZE",       4.0),
    'B': ("!!", 3, "BOMB",         0.0),
    'W': ("<>", 5, "WIDE BEAM",   10.0),
    'X': ("SH", 4, "SHIELD",       0.0),
    'K': ("HK", 6, "STICKY HOOK", 12.0),
    'G': ("GN", 3, "POWER GUN",   12.0),
    'P': ("PI", 4, "PIERCING",    10.0),
    'M': ("MG", 7, "MAGNET",      12.0),
    'R': ("MR", 2, "MIRROR SHOT", 10.0),
    'T': ("3X", 5, "TRIPLE",      10.0),
    'V': ("GH", 1, "GHOST",        5.0),
    'U': ("AU", 4, "AUTO-AIM",    10.0),
    'Z': ("RW", 6, "REWIND",       0.0),
}
POWERUP_WEIGHTS = {
    'L': 1.0, 'D': 1.5, 'S': 1.2, 'F': 0.8, 'B': 0.6, 'W': 1.3, 'X': 1.2,
    'K': 1.5, 'G': 1.5, 'P': 1.2, 'M': 1.1, 'R': 1.0, 'T': 1.2,
    'V': 1.0, 'U': 1.2, 'Z': 0.5,
}

REWIND_FRAMES = int(2.0 * TARGET_FPS)
REWIND_SAMPLE_EVERY = 2     # snapshot every Nth frame to save CPU

# Music stops itself after this many seconds on the same level so the loop
# doesn't fatigue the player. Long enough to let the full A/B 32-bar
# composition (~58 s) play through once with breathing room. Press J to
# disable music entirely.
MUSIC_FADE_AFTER = 90.0

# Weapon-swap powerups are mutually exclusive; picking one clears the others.
WEAPON_SWAPS = ('K', 'G', 'W')

# --- Ball variants ---------------------------------------------------------
BV_NORMAL    = 'normal'
BV_SPIKE     = 'spike'
BV_ICE       = 'ice'
BV_EXPLOSIVE = 'explosive'
BV_MAGNETIC  = 'magnetic'
BV_GOLD      = 'gold'
BV_BOSS      = 'boss'

VARIANT_GLYPH = {
    BV_SPIKE:     '✱',
    BV_ICE:       '❄',
    BV_EXPLOSIVE: '⚠',
    BV_MAGNETIC:  '↯',
    BV_GOLD:      '$',
    BV_BOSS:      '☣',
}
VARIANT_COLOR = {
    BV_SPIKE:     1,   # white
    BV_ICE:       4,   # cyan
    BV_EXPLOSIVE: 3,   # red
    BV_MAGNETIC:  6,   # magenta
    BV_GOLD:      2,   # yellow
    BV_BOSS:      3,   # red
}
GOLD_MULT      = 10
ICE_FREEZE_T   = 1.5
EXPLOSIVE_RANGE = 8.0
MAGNETIC_PULL  = 2.2
BOSS_HP        = {5: 4, 10: 6, 15: 9, 20: 12}    # by level milestone

# --- World / theme ---------------------------------------------------------
THEME_SPACE   = 'space'
THEME_SEA     = 'sea'
THEME_VOLCANO = 'volcano'
THEME_NEON    = 'neon'

THEMES = {
    THEME_SPACE: dict(
        gravity_mult=1.0,
        wrap=False,
        platform_color=1,
        bg_phases=[('·', 'DIM'), ('⋆', 'NORM'), ('✦', 'BOLD'), ('✧', 'BOLD')],
        floor_glyph='═',
    ),
    THEME_SEA: dict(
        gravity_mult=0.65,
        wrap=False,
        platform_color=4,
        bg_phases=[('·', 'DIM'), ('~', 'NORM'), ('≈', 'BOLD'), ('~', 'BOLD')],
        floor_glyph='≋',
    ),
    THEME_VOLCANO: dict(
        gravity_mult=1.4,
        wrap=False,
        platform_color=3,
        bg_phases=[('.', 'DIM'), ('•', 'NORM'), ('▴', 'BOLD'), ('▲', 'BOLD')],
        floor_glyph='▬',
    ),
    THEME_NEON: dict(
        gravity_mult=1.0,
        wrap=True,
        platform_color=6,
        bg_phases=[('·', 'DIM'), ('+', 'NORM'), ('×', 'BOLD'), ('✦', 'BOLD')],
        floor_glyph='═',
    ),
}

def theme_for_level(mode, level):
    if mode == MODE_DAILY:
        # Daily rotates by date hash so each day feels distinct.
        names = (THEME_SPACE, THEME_SEA, THEME_VOLCANO, THEME_NEON)
        return names[daily_seed() % 4]
    if mode == MODE_BOSS_RUSH:
        return THEME_VOLCANO
    if mode == MODE_PUZZLE:
        # Cycle the puzzle themes so each one feels distinct.
        return (THEME_SPACE, THEME_SEA, THEME_VOLCANO, THEME_NEON)[(level - 1) % 4]
    if level <= 4:
        return THEME_SPACE
    if level <= 9:
        return THEME_SEA
    if level <= 14:
        return THEME_VOLCANO
    return THEME_NEON

# --- Difficulty ------------------------------------------------------------
DIFFICULTIES = {
    'easy':   dict(lives_mod=+1, ball_speed=0.85, drop_chance=0.42, hurry_t=80.0),
    'normal': dict(lives_mod= 0, ball_speed=1.00, drop_chance=0.32, hurry_t=60.0),
    'hard':   dict(lives_mod=-1, ball_speed=1.20, drop_chance=0.22, hurry_t=45.0),
}
DIFFICULTY_ORDER = ('easy', 'normal', 'hard')

# --- Characters ------------------------------------------------------------
CHARACTERS = {
    'classic':  dict(lives=3, step=2,  fire_cooldown=0.0,  label="Classic"),
    'runner':   dict(lives=2, step=3,  fire_cooldown=0.0,  label="Runner"),    # fast, fragile
    'gunner':   dict(lives=3, step=2,  fire_cooldown=-0.2, label="Gunner"),    # faster fire
    'tank':     dict(lives=5, step=1,  fire_cooldown=0.10, label="Tank"),      # slow, tanky
}
CHARACTER_ORDER = ('classic', 'runner', 'gunner', 'tank')

# --- Skins -----------------------------------------------------------------
SKINS = {
    'classic': dict(label='Classic', player_color=4,
                    ball_palette={4: 3, 3: 6, 2: 5, 1: 7},
                    unlock='always',
                    unlock_hint=''),
    'retro':   dict(label='Retro',   player_color=2,
                    ball_palette={4: 2, 3: 5, 2: 3, 1: 7},
                    unlock='reach_10',
                    unlock_hint='Reach level 10'),
    'neon':    dict(label='Neon',    player_color=6,
                    ball_palette={4: 6, 3: 2, 2: 5, 1: 4},
                    unlock='gold_pop',
                    unlock_hint='Pop a gold ball'),
    'mono':    dict(label='Mono',    player_color=1,
                    ball_palette={4: 1, 3: 1, 2: 1, 1: 1},
                    unlock='flawless_level',
                    unlock_hint='Clear a level flawless'),
    'crimson': dict(label='Crimson', player_color=3,
                    ball_palette={4: 3, 3: 3, 2: 2, 1: 2},
                    unlock='first_boss',
                    unlock_hint='Defeat a boss'),
}
SKIN_ORDER = ['classic', 'retro', 'neon', 'mono', 'crimson']

# Mutable holder so draw helpers can read the active skin without a kwarg.
_ACTIVE_SKIN = ['classic']

# Music-only toggle. Independent of audio.toggle_mute() which silences SFX too.
_MUSIC_OFF = [False]

def play_music(name):
    if _MUSIC_OFF[0]:
        audio.stop_music()
        return
    audio.play_music(name)

def is_skin_unlocked(skin_id, save):
    info = SKINS.get(skin_id)
    if not info or info['unlock'] == 'always':
        return True
    return info['unlock'] in (save['achievements'] or {})

def unlocked_skins(save):
    return [s for s in SKIN_ORDER if is_skin_unlocked(s, save)]

def ball_color_for(ball):
    return SKINS[_ACTIVE_SKIN[0]]['ball_palette'].get(
        ball.size, BALL_COLOR[ball.size])

def player_color_for():
    return SKINS[_ACTIVE_SKIN[0]]['player_color']

# --- Game modes -------------------------------------------------------------
MODE_CLASSIC   = 'classic'
MODE_TIME      = 'time_attack'
MODE_SURVIVAL  = 'survival'
MODE_BOSS_RUSH = 'boss_rush'
MODE_DAILY     = 'daily'
MODE_PUZZLE    = 'puzzle'

MODE_INFO = {
    MODE_CLASSIC:   ("CLASSIC",     "Beat as many levels as you can."),
    MODE_TIME:      ("TIME ATTACK", "Clear 10 levels — fastest wins."),
    MODE_SURVIVAL:  ("SURVIVAL",    "Endless. No retry. One life."),
    MODE_BOSS_RUSH: ("BOSS RUSH",   "Only bosses. Hope you brought hooks."),
    MODE_DAILY:     ("DAILY",       "Today's seed. Compare your score."),
    MODE_PUZZLE:    ("PUZZLE",      "Hand-crafted levels, limited shots."),
}
MODE_ORDER = [MODE_CLASSIC, MODE_TIME, MODE_SURVIVAL,
              MODE_BOSS_RUSH, MODE_DAILY, MODE_PUZZLE]
TIME_ATTACK_LEVELS = 10

# Puzzles: fractional coords resolved against the actual playfield size at
# build time. Ball entries are (x_frac, y_frac, vx, vy, size[, variant[, hp]]).
# Platforms are (x_frac, y_frac, w_frac). Hazards are (x_frac, w_frac) on the
# floor row.
PUZZLES = [
    dict(name="Intro",          shots=3,
         balls=[(0.40, 0.30, 8.0, 0.0, 3)],
         platforms=[], hazards=[]),
    dict(name="Roof",           shots=2,
         balls=[(0.50, 0.25, 8.0, 0.0, 4)],
         platforms=[(0.25, 0.55, 0.50)], hazards=[]),
    dict(name="Twins",          shots=4,
         balls=[(0.25, 0.30, 6.0, 0.0, 3),
                (0.75, 0.30, -6.0, 0.0, 3)],
         platforms=[], hazards=[]),
    dict(name="Thorny",         shots=5,
         balls=[(0.50, 0.30, 7.0, 0.0, 3, BV_SPIKE)],
         platforms=[], hazards=[]),
    dict(name="Chamber",        shots=3,
         balls=[(0.50, 0.25, 9.0, 0.0, 4)],
         platforms=[(0.15, 0.65, 0.20), (0.65, 0.65, 0.20)], hazards=[]),
    dict(name="Floor is Lava",  shots=4,
         balls=[(0.20, 0.30, 6.0, 0.0, 3),
                (0.80, 0.30, -6.0, 0.0, 3)],
         platforms=[], hazards=[(0.35, 0.30)]),
    dict(name="Powder Keg",     shots=2,
         balls=[(0.50, 0.30, 0.0, 0.0, 3, BV_EXPLOSIVE),
                (0.25, 0.30, 6.0, 0.0, 2),
                (0.75, 0.30, -6.0, 0.0, 2)],
         platforms=[], hazards=[]),
    dict(name="Mini Boss",      shots=6,
         balls=[(0.50, 0.25, 5.0, 0.0, 4, BV_BOSS, 6)],
         platforms=[], hazards=[]),
]

# --- Achievements -----------------------------------------------------------
ACHIEVEMENTS = {
    'first_pop':        "First Pop!",
    'combo_3':          "Triple Combo",
    'combo_8':          "Untouchable",
    'flawless_level':   "Flawless Stage",
    'bomb_5':           "Five at Once",
    'reach_10':         "Up to Level 10",
    'reach_20':         "Up to Level 20",
    'first_boss':       "Boss Down",
    'gold_pop':         "Goldsmith",
    'pierce_combo':     "Skewer",
    'sticky_wall':      "Wall of Hooks",
    'no_powerups':      "Purist",       # cleared a level without any active pu
    'survivor_50':      "Survivor",     # reached survival level 50
    'time_under_120':   "Speedrunner",  # time attack under 2:00
}

# --- Save file --------------------------------------------------------------
SAVE_PATH = Path(os.environ.get('PANG_SAVE',
                                str(Path.home() / '.pang_save.json')))

def _default_save():
    return {
        'high_scores': {m: [] for m in MODE_ORDER},
        'daily':       {},   # date -> {score, level}
        'achievements': {},  # key -> date
        'settings':    {'colorblind': False},
    }

def load_save():
    data = _default_save()
    try:
        with open(SAVE_PATH, 'r') as f:
            loaded = json.load(f)
        # shallow merge with defaults to tolerate older save files
        if isinstance(loaded, dict):
            for k in data:
                if k in loaded and isinstance(loaded[k], type(data[k])):
                    data[k] = loaded[k]
            # ensure every mode key exists
            for m in MODE_ORDER:
                data['high_scores'].setdefault(m, [])
    except Exception:
        pass
    return data

def write_save(save):
    try:
        SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SAVE_PATH, 'w') as f:
            json.dump(save, f, indent=2)
    except Exception:
        pass

def record_high_score(save, mode, score, level, extra=None):
    table = save['high_scores'].setdefault(mode, [])
    entry = {
        'score': int(score),
        'level': int(level),
        'date':  time.strftime('%Y-%m-%d'),
    }
    if extra:
        entry.update(extra)
    table.append(entry)
    table.sort(key=lambda r: r.get('score', 0), reverse=True)
    save['high_scores'][mode] = table[:10]
    write_save(save)

def record_daily(save, score, level):
    today = time.strftime('%Y-%m-%d')
    prev = save['daily'].get(today)
    if prev is None or score > prev.get('score', 0):
        save['daily'][today] = {'score': int(score), 'level': int(level)}
        write_save(save)

def daily_seed():
    today = time.strftime('%Y-%m-%d')
    return int(hashlib.sha1(today.encode()).hexdigest()[:8], 16)

def unlock(save, key, toasts):
    if not key or key in save['achievements']:
        return
    save['achievements'][key] = time.strftime('%Y-%m-%d')
    toasts.append([ACHIEVEMENTS.get(key, key), 3.0, 3.0])  # text, life, max
    write_save(save)

STAR_PHASES = [('·', 'DIM'), ('⋆', 'NORM'), ('✦', 'BOLD'), ('✧', 'BOLD')]

HEART       = '♥'
DIAMOND     = '◆'
TIP_CHARS   = ['▲', '✦']
SHAFT_CHARS = ['┃', '╿']

FR_TL, FR_TR = '╔', '╗'
FR_BL, FR_BR = '╚', '╝'
FR_H,  FR_V  = '═', '║'

PANG_ART = [
    "██████   █████   ██    ██  ██████ ",
    "██   ██ ██   ██  ███   ██  ██     ",
    "██████  ███████  ██ █  ██  ██ ███ ",
    "██      ██   ██  ██  █ ██  ██  ██ ",
    "██      ██   ██  ██    ██  ██████ ",
]
PANG_ART_SHADOW = [
    " ▀▀▀▀▀▀  ▀▀▀▀▀   ▀▀    ▀▀  ▀▀▀▀▀▀ ",
]


# --- Entities ---------------------------------------------------------------
class Ball:
    __slots__ = ("x", "y", "vx", "vy", "size", "variant", "hp", "phase", "alive")
    def __init__(self, x, y, vx, vy, size, variant=BV_NORMAL, hp=1):
        self.x, self.y = float(x), float(y)
        self.vx, self.vy = float(vx), float(vy)
        self.size = size
        self.variant = variant
        self.hp = hp
        self.phase = random.random() * math.tau
        self.alive = True

    def update(self, dt, w, h, player_x=None, gravity_mult=1.0, wrap=False):
        if self.variant == BV_MAGNETIC and player_x is not None:
            self.vx += (1.0 if player_x > self.x else -1.0) * MAGNETIC_PULL * dt
            self.vx = max(-18.0, min(18.0, self.vx))
        self.vy += GRAVITY * gravity_mult * dt
        self.x  += self.vx * dt
        self.y  += self.vy * dt
        self.phase += dt * 6
        r = BALL_RADIUS[self.size]
        span = max(1.0, w - 3.0)
        if wrap:
            if self.x < 1:
                self.x += span
            elif self.x > w - 2:
                self.x -= span
        else:
            if self.x - r < 1:
                self.x = 1 + r; self.vx = abs(self.vx)
            if self.x + r > w - 2:
                self.x = w - 2 - r; self.vx = -abs(self.vx)
        if self.y - r < 1:
            self.y = 1 + r; self.vy = abs(self.vy)
        if self.y + r > h - 3:
            self.y = h - 3 - r; self.vy = SPLIT_VY[self.size]

    def collide_platform(self, plat):
        """Bounces the ball off the top/underside of a platform. Returns True
        if a collision happened."""
        if not plat.alive:
            return False
        r = BALL_RADIUS[self.size]
        if self.x + r < plat.x or self.x - r > plat.x + plat.w:
            return False
        # Top contact: ball is just above and moving down.
        if self.vy > 0 and plat.y - r - 0.6 <= self.y <= plat.y - r + 0.6:
            self.y = plat.y - r - 0.1
            self.vy = SPLIT_VY[self.size]
            return True
        # Underside contact: ball moving up.
        if self.vy < 0 and plat.y + r - 0.6 <= self.y <= plat.y + r + 0.6:
            self.y = plat.y + r + 0.1
            self.vy = abs(self.vy) * 0.6
            return True
        return False

    def hits_vline(self, x, y_top, y_bot, half_width=0.0):
        r = BALL_RADIUS[self.size] + half_width
        dx = self.x - x
        if abs(dx) > r:
            return False
        cy = max(y_top, min(self.y, y_bot))
        dy = self.y - cy
        return dx * dx + dy * dy <= r * r

    def hits_player(self, px, py):
        r = BALL_RADIUS[self.size] + 0.3
        cx = max(px - PLAYER_HALF_W, min(self.x, px + PLAYER_HALF_W))
        cy = max(py - PLAYER_TOP_REACH, min(self.y, py + PLAYER_BOT_REACH))
        dx = self.x - cx
        dy = (self.y - cy) * 1.2
        return dx * dx + dy * dy <= r * r


class Projectile:
    __slots__ = ("x", "base_y", "tip_y", "alive", "kind",
                 "wide", "piercing", "mirror",
                 "stuck", "stuck_t", "dy_dir", "mirror_used",
                 "hit_cooldown")
    def __init__(self, x, base_y, kind=PROJ_HARPOON,
                 wide=False, piercing=False, mirror=False):
        self.x        = float(x)
        self.base_y   = float(base_y)
        self.tip_y    = float(base_y) - 1
        self.alive    = True
        self.kind     = kind
        self.wide     = wide and kind == PROJ_HARPOON
        self.piercing = piercing
        self.mirror   = mirror and kind == PROJ_BULLET
        self.stuck    = False
        self.stuck_t  = 0.0
        self.dy_dir   = -1                  # bullet direction: -1 up, +1 down
        self.mirror_used = not self.mirror
        self.hit_cooldown = 0.0

    def update(self, dt, h):
        if self.hit_cooldown > 0:
            self.hit_cooldown = max(0.0, self.hit_cooldown - dt)

        if self.kind == PROJ_BULLET:
            speed = HARPOON_SPEED * BULLET_SPEED_MULT
            self.tip_y += self.dy_dir * speed * dt
            self.base_y = self.tip_y + 1
            if self.dy_dir < 0 and self.tip_y < 1:
                if not self.mirror_used:
                    self.mirror_used = True
                    self.dy_dir = 1
                    self.tip_y = 1
                else:
                    self.alive = False
            elif self.dy_dir > 0 and self.tip_y > h - 3:
                self.alive = False
            return

        if self.stuck:
            self.stuck_t += dt
            if self.stuck_t > STICKY_LIFE:
                self.alive = False
            return

        self.tip_y -= HARPOON_SPEED * dt
        if self.tip_y < 1:
            if self.kind == PROJ_STICKY:
                self.tip_y = 1
                self.stuck = True
            else:
                self.alive = False

    def can_hit(self):
        return self.hit_cooldown <= 0


class Chest:
    __slots__ = ("x", "y", "hp", "kind", "alive", "phase")
    def __init__(self, x, y, hp=2, kind=None):
        self.x = float(x); self.y = float(y)
        self.hp = int(hp)
        self.kind = kind        # specific powerup, or None = roll random
        self.alive = True
        self.phase = random.random() * math.tau

    def hits_projectile(self, hp):
        return (abs(hp.x - self.x) <= 2.0
                and min(hp.tip_y, hp.base_y) <= self.y + 0.5
                and max(hp.tip_y, hp.base_y) >= self.y - 0.5)


class Powerup:
    __slots__ = ("x", "y", "vy", "kind", "life", "wobble", "landed", "warned")
    def __init__(self, x, y, kind):
        self.x, self.y = float(x), float(y)
        self.vy = POWERUP_FALL_SPEED
        self.kind = kind
        self.life = POWERUP_LIFETIME
        self.wobble = random.random() * math.tau
        self.landed = False
        self.warned = False
    def update(self, dt, h):
        self.life -= dt
        was_falling = self.vy > 0
        self.y += self.vy * dt
        self.wobble += dt * 4
        floor = h - 4
        if self.y >= floor and not self.landed and was_falling:
            self.landed = True
            audio.sfx('powerup_land')
        if self.y > floor:
            self.y = floor; self.vy = 0
        if not self.warned and self.life < 3.0:
            self.warned = True
            audio.sfx('powerup_warn')
    def draw_x(self):
        return self.x + (math.sin(self.wobble) * 0.5 if self.vy > 0 else 0)
    def caught_by(self, px, py):
        return abs(self.x - px) <= 3.0 and abs(self.y - py) <= 1.7


class Particle:
    __slots__ = ("x", "y", "vx", "vy", "life", "max_life", "color", "kind")
    def __init__(self, x, y, vx, vy, life, color, kind='spark'):
        self.x, self.y = float(x), float(y)
        self.vx, self.vy = float(vx), float(vy)
        self.life = life
        self.max_life = life
        self.color = color
        self.kind = kind        # 'spark' (default), 'trail' (no gravity, fade fast)
    def update(self, dt):
        self.life -= dt
        if self.kind == 'spark':
            self.vy += GRAVITY * 0.4 * dt
        self.x += self.vx * dt
        self.y += self.vy * dt


class ScorePopup:
    __slots__ = ("x", "y", "text", "life", "max_life", "color")
    def __init__(self, x, y, text, color=2):
        self.x, self.y = float(x), float(y)
        self.text = text
        self.life = 0.95
        self.max_life = 0.95
        self.color = color
    def update(self, dt):
        self.life -= dt
        self.y -= 6.0 * dt


class Platform:
    __slots__ = ("x", "y", "w", "hp", "max_hp", "alive")
    def __init__(self, x, y, w, hp=0):
        self.x = int(x)
        self.y = int(y)
        self.w = int(w)
        self.hp = int(hp)        # 0 = indestructible
        self.max_hp = int(hp)
        self.alive = True

    def overlaps_x(self, x, pad=0.0):
        return self.x - pad <= x <= self.x + self.w + pad


class Hazard:
    __slots__ = ("x", "y", "w", "kind")
    def __init__(self, x, y, w, kind='spike'):
        self.x = int(x)
        self.y = int(y)
        self.w = int(w)
        self.kind = kind

    def contains(self, x):
        return self.x - 0.5 <= x <= self.x + self.w + 0.5


ENEMY_CRAB = 'crab'
ENEMY_BAT  = 'bat'

class Enemy:
    __slots__ = ("x", "y", "vx", "vy", "kind", "hp", "phase", "alive")
    def __init__(self, x, y, kind, hp=1):
        self.x = float(x); self.y = float(y)
        self.vx = -8.0 if kind == ENEMY_CRAB else 6.0
        self.vy = 0.0
        self.kind = kind
        self.hp = hp
        self.phase = random.random() * math.tau
        self.alive = True

    def update(self, dt, w, h, player_x, player_y, gravity):
        self.phase += dt * 3
        if self.kind == ENEMY_CRAB:
            self.vy += gravity * dt
            self.x  += self.vx * dt
            self.y  += self.vy * dt
            if self.x < 2:
                self.x = 2; self.vx = abs(self.vx)
            if self.x > w - 3:
                self.x = w - 3; self.vx = -abs(self.vx)
            if self.y > h - 4:
                self.y = h - 4; self.vy = 0
        else:
            # Bat: hovers above, occasionally dives at the player.
            dive = math.sin(self.phase * 0.5) > 0.55
            if dive:
                target_y = max(4, player_y - 1)
            else:
                target_y = max(4, min(h - 8, player_y - 8))
            self.vy += (target_y - self.y) * 1.8 * dt
            self.vy *= 0.92
            if player_x > self.x:
                self.vx += 6.0 * dt
            else:
                self.vx -= 6.0 * dt
            self.vx = max(-12.0, min(12.0, self.vx))
            self.y += self.vy * dt + math.sin(self.phase) * dt * 0.6
            self.x += self.vx * dt
            if self.x < 2:
                self.x = 2; self.vx = abs(self.vx)
            if self.x > w - 3:
                self.x = w - 3; self.vx = -abs(self.vx)

    def hitbox(self):
        if self.kind == ENEMY_CRAB:
            return (self.x - 1.5, self.x + 1.5, self.y - 0.5, self.y + 0.5)
        return (self.x - 1.5, self.x + 1.5, self.y - 0.5, self.y + 0.5)

    def hits_player(self, px, py):
        x1, x2, y1, y2 = self.hitbox()
        return x1 <= px <= x2 and y1 - 1.5 <= py <= y2 + 1.5

    def hits_projectile(self, hp):
        x1, x2, y1, y2 = self.hitbox()
        return (x1 <= hp.x <= x2
                and min(hp.tip_y, hp.base_y) <= y2
                and max(hp.tip_y, hp.base_y) >= y1)


class Player:
    __slots__ = ("x", "y", "lives", "score", "active",
                 "facing", "last_move_t", "hit_flash", "happy_flash", "fire_flash",
                 "recoil_t", "combo", "combo_t", "freeze_t", "shake_t")
    def __init__(self, x, y):
        self.x, self.y = x, y
        self.lives = 3
        self.score = 0
        self.active = {}
        self.facing = 0
        self.last_move_t = -10.0
        self.hit_flash   = 0.0
        self.happy_flash = 0.0
        self.fire_flash  = 0.0
        self.recoil_t    = 0.0
        self.combo       = 0
        self.combo_t     = -10.0
        self.freeze_t    = 0.0
        self.shake_t     = 0.0
    def reset_face(self):
        self.facing = 0
        self.last_move_t = -10.0
        self.hit_flash = 0.0
        self.happy_flash = 0.0
        self.fire_flash = 0.0
        self.recoil_t = 0.0
        self.combo = 0
        self.combo_t = -10.0
        self.freeze_t = 0.0
        self.shake_t  = 0.0


# --- Levels & helpers -------------------------------------------------------
def split_ball(b):
    if b.size == 1:
        return []
    s = b.size - 1
    if b.variant == BV_BOSS:
        # Boss collapses into 4 normal balls when its HP runs out.
        return [
            Ball(b.x - 1, b.y, -SPLIT_VX[s] * 1.2, SPLIT_VY[s], s),
            Ball(b.x + 1, b.y,  SPLIT_VX[s] * 1.2, SPLIT_VY[s], s),
            Ball(b.x,     b.y, -SPLIT_VX[s] * 0.6, SPLIT_VY[s] * 0.7, s),
            Ball(b.x,     b.y,  SPLIT_VX[s] * 0.6, SPLIT_VY[s] * 0.7, s),
        ]
    return [
        Ball(b.x, b.y, -SPLIT_VX[s], SPLIT_VY[s], s),
        Ball(b.x, b.y,  SPLIT_VX[s], SPLIT_VY[s], s),
    ]


def boss_hp_for(level):
    hp = next(iter(BOSS_HP.values()))
    for mk in sorted(BOSS_HP.keys()):
        if level >= mk:
            hp = BOSS_HP[mk]
    return hp


def is_boss_level(mode, level):
    if mode == MODE_BOSS_RUSH:
        return True
    if mode == MODE_PUZZLE:
        if 1 <= level <= len(PUZZLES):
            return any(len(b) >= 6 and b[5] == BV_BOSS
                       for b in PUZZLES[level - 1]['balls'])
        return False
    return level >= 5 and level % 5 == 0


def build_puzzle_world(level, w, h, theme_name):
    """Build a hand-crafted puzzle level. Returns a partial world dict."""
    if not (1 <= level <= len(PUZZLES)):
        return None
    puzzle = PUZZLES[level - 1]
    balls = []
    for entry in puzzle['balls']:
        xf, yf, vx, vy, size = entry[:5]
        variant = entry[5] if len(entry) >= 6 else BV_NORMAL
        hp      = entry[6] if len(entry) >= 7 else 1
        balls.append(Ball(xf * w, yf * h, vx, vy, size, variant, hp))
    platforms = []
    for xf, yf, wf in puzzle['platforms']:
        platforms.append(Platform(int(xf * w), int(yf * h), int(wf * w)))
    hazards = []
    for xf, wf in puzzle['hazards']:
        hazards.append(Hazard(int(xf * w), h - 3, int(wf * w), kind='spike'))
    return {
        'balls': balls,
        'platforms': platforms,
        'hazards': hazards,
        'enemies': [],
        'chests': [],
        'theme': theme_name,
        'theme_def': THEMES[theme_name],
        'shot_budget': puzzle['shots'],
        'puzzle_name': puzzle['name'],
    }


def build_world(mode, level, w, h, base_seed=0, difficulty='normal'):
    """Returns a dict with balls/platforms/hazards/enemies/chests/theme."""
    theme_name = theme_for_level(mode, level)
    theme = THEMES[theme_name]
    diff = DIFFICULTIES[difficulty]

    if mode == MODE_PUZZLE:
        pworld = build_puzzle_world(level, w, h, theme_name)
        if pworld is not None:
            pworld['diff'] = diff
            return pworld

    rng = random.Random(level * 7919 + 13 + base_seed)

    balls = make_level(mode, level, w, h, base_seed)
    for b in balls:
        b.vx *= diff['ball_speed']

    platforms = []
    hazards   = []
    enemies   = []
    chests    = []

    is_boss = is_boss_level(mode, level)

    # Platforms appear from level 3+, except on bosses (open arena).
    if level >= 3 and not is_boss:
        mid_y = h // 2
        if level % 3 == 0:
            # Two short side platforms.
            pw = max(8, w // 5)
            platforms.append(Platform(w // 6, mid_y - 1, pw))
            platforms.append(Platform(w - w // 6 - pw, mid_y - 1, pw))
        elif level % 3 == 1 and level >= 4:
            # One wide centre platform (destructible at higher levels).
            pw = max(10, w // 3)
            hp = 0 if level < 8 else 3
            platforms.append(Platform((w - pw) // 2, mid_y, pw, hp=hp))
        else:
            # Stepped layout.
            pw = max(6, w // 7)
            platforms.append(Platform(w // 8,       mid_y + 1, pw))
            platforms.append(Platform((w - pw) // 2, mid_y - 2, pw))
            platforms.append(Platform(w - w // 8 - pw, mid_y + 1, pw))

    # Spike hazards from level 6+.
    if level >= 6 and not is_boss and rng.random() < 0.6:
        hw = rng.randint(4, max(5, w // 8))
        hx = rng.randint(3, max(4, w - hw - 4))
        hazards.append(Hazard(hx, h - 3, hw, kind='spike'))

    # Enemies sprinkled in non-boss levels.
    if level >= 4 and not is_boss:
        if level >= 4 and rng.random() < 0.45:
            enemies.append(Enemy(rng.randint(4, w - 5), h - 4, ENEMY_CRAB))
        if level >= 7 and rng.random() < 0.40:
            enemies.append(Enemy(rng.randint(4, w - 5),
                                 rng.randint(3, h // 2), ENEMY_BAT))

    # A chest now and then on platforms (extra reward).
    if platforms and level >= 5 and rng.random() < 0.5:
        plat = rng.choice(platforms)
        cx = plat.x + plat.w // 2
        chests.append(Chest(cx, plat.y - 1))

    return {
        'balls': balls,
        'platforms': platforms,
        'hazards': hazards,
        'enemies': enemies,
        'chests': chests,
        'theme': theme_name,
        'theme_def': theme,
        'diff': diff,
        'shot_budget': None,
        'puzzle_name': None,
    }


def make_level(mode, level, w, h, base_seed=0):
    rng = random.Random(level * 7919 + 13 + base_seed)
    balls = []

    if is_boss_level(mode, level):
        balls.append(Ball(
            w * 0.5, h * 0.3, 6.0 + level * 0.2, 0.0,
            4, BV_BOSS, hp=boss_hp_for(level),
        ))
        return balls

    if level == 1:
        balls.append(Ball(w * 0.30, h * 0.30, 8.0, 0.0, 3))
    elif level == 2:
        balls.append(Ball(w * 0.50, h * 0.30, 9.0, 0.0, 4))
    elif level == 3:
        balls.append(Ball(w * 0.25, h * 0.30, -8.0, 0.0, 3))
        balls.append(Ball(w * 0.75, h * 0.30,  8.0, 0.0, 3))
    else:
        n = min(4, 2 + (level - 3) // 2)
        for i in range(n):
            balls.append(Ball(
                w * (i + 1) / (n + 1),
                h * 0.25,
                rng.choice([-1, 1]) * (7.0 + level * 0.5),
                0.0,
                rng.choice([3, 4]),
            ))

    # Sprinkle a variant onto an existing ball from level 4 onward.
    if level >= 4 and balls:
        variant_pool = [BV_SPIKE, BV_ICE, BV_EXPLOSIVE, BV_MAGNETIC]
        variant_chance = min(0.65, 0.25 + 0.05 * (level - 3))
        if rng.random() < variant_chance:
            target = rng.choice(balls)
            target.variant = rng.choice(variant_pool)

    # Rare bonus: a tiny gold ball worth 10x.
    if level >= 3 and rng.random() < 0.18:
        balls.append(Ball(
            w * rng.uniform(0.25, 0.75),
            h * 0.20,
            rng.choice([-1, 1]) * 9.5,
            0.0, 2, BV_GOLD,
        ))

    return balls


def random_powerup_kind():
    kinds   = list(POWERUP_WEIGHTS.keys())
    weights = [POWERUP_WEIGHTS[k] for k in kinds]
    return random.choices(kinds, weights=weights, k=1)[0]


def spawn_explosion(particles, x, y, n, color):
    for _ in range(n):
        ang = random.uniform(0, math.tau)
        spd = random.uniform(6, 14)
        particles.append(Particle(
            x, y,
            math.cos(ang) * spd,
            math.sin(ang) * spd * 0.7 - 4,
            random.uniform(0.4, 0.9),
            color,
        ))


def spawn_muzzle_flash(particles, x, y):
    for _ in range(9):
        ang = -math.pi / 2 + random.uniform(-0.7, 0.7)
        spd = random.uniform(8, 16)
        particles.append(Particle(
            x, y,
            math.cos(ang) * spd,
            math.sin(ang) * spd,
            random.uniform(0.12, 0.28),
            2,                              # yellow
            'spark',
        ))


def spawn_harpoon_trail(particles, hp):
    # subtle exhaust behind the rising tip
    for _ in range(2):
        particles.append(Particle(
            hp.x + random.uniform(-0.4, 0.4),
            hp.tip_y + random.uniform(0.5, 1.8),
            random.uniform(-0.6, 0.6),
            random.uniform(2.0, 5.0),
            random.uniform(0.12, 0.25),
            2,
            'trail',
        ))


def combo_mult(combo):
    """Score multiplier from current combo length."""
    if combo >= 8: return 3.0
    if combo >= 5: return 2.0
    if combo >= 3: return 1.5
    return 1.0


def apply_powerup_pickup(player, kind, balls, particles):
    info = POWERUPS[kind]
    if kind == 'L':
        player.lives += 1
    elif kind == 'B':
        # Bombs nuke regular balls but only chip bosses.
        boss_dying = []
        for b in balls:
            if not b.alive:
                continue
            spawn_explosion(particles, b.x, b.y, 22, BALL_COLOR[b.size])
            if b.variant == BV_BOSS:
                b.hp -= 2
                if b.hp > 0:
                    continue
                # Boss died from bomb — match the harpoon-kill behaviour so
                # players don't lose the split-and-bonus reward.
                boss_dying.append(b)
                player.score += POINTS[4] * 5
            player.score += POINTS[b.size] // 2
            b.alive = False
        # Splitting after marking dead, then extending the live list.
        for b in boss_dying:
            balls.extend(split_ball(b))
    elif kind == 'X':
        player.active['X'] = True
    else:
        if kind in WEAPON_SWAPS:
            for other in WEAPON_SWAPS:
                if other != kind:
                    player.active.pop(other, None)
        player.active[kind] = info[3]
    player.happy_flash = 0.4


def current_weapon_kind(player):
    if 'K' in player.active:
        return PROJ_STICKY
    if 'G' in player.active:
        return PROJ_BULLET
    return PROJ_HARPOON


def spawn_shot(player, h, projectiles):
    """Append projectiles for one fire-button press. Returns spawn count."""
    kind     = current_weapon_kind(player)
    wide     = 'W' in player.active
    piercing = 'P' in player.active
    mirror   = 'R' in player.active
    spread   = 'T' in player.active

    base_y = h - 4
    if spread:
        offsets = (-3, 0, 3)
    else:
        offsets = (0,)

    for ox in offsets:
        projectiles.append(Projectile(
            player.x + ox, base_y,
            kind=kind, wide=wide, piercing=piercing, mirror=mirror,
        ))


def weapon_max_in_flight(player):
    kind = current_weapon_kind(player)
    base = 3 if kind == PROJ_BULLET else 1
    if 'D' in player.active:
        base *= 2
    # Triple spawns 3 projectiles per shot; lift the cap so the spread fits
    # without instantly maxing out the in-flight budget.
    if 'T' in player.active:
        base = max(base * 3, base + 2)
    return base


def tick_powerups(player, dt):
    expired = []
    for k, v in list(player.active.items()):
        if v is True:
            continue
        v -= dt
        if v <= 0:
            expired.append(k)
        else:
            player.active[k] = v
    for k in expired:
        del player.active[k]


def speed_factor(player):
    if player.active.get('F'):
        return 0.0
    if player.active.get('S'):
        return 0.45
    return 1.0


# --- Render helpers ---------------------------------------------------------
def attr_from(name):
    if name == 'BOLD':
        return curses.A_BOLD
    if name == 'DIM':
        return curses.A_DIM
    return 0


_SHAKE = [0, 0]   # set by play loop; applied transparently by safe_addstr.


def safe_addstr(stdscr, y, x, s, attr=0):
    try:
        stdscr.addstr(y + _SHAKE[0], x + _SHAKE[1], s, attr)
    except curses.error:
        pass


def draw_sprite(stdscr, lines, top_y, left_x, base_attr, w, h, row_attrs=None):
    for dy, line in enumerate(lines):
        y = top_y + dy
        if y < 1 or y >= h - 2:
            continue
        extra = attr_from(row_attrs[dy]) if row_attrs else 0
        line_attr = base_attr | extra
        i = 0
        n = len(line)
        while i < n:
            while i < n and line[i] == ' ':
                i += 1
            if i >= n:
                break
            j = i
            while j < n and line[j] != ' ':
                j += 1
            x = left_x + i
            run = line[i:j]
            if x < 1:
                run = run[1 - x:]
                x = 1
            if x + len(run) > w - 1:
                run = run[:w - 1 - x]
            if run:
                safe_addstr(stdscr, y, x, run, line_attr)
            i = j


def draw_frame(stdscr, w, h, t):
    color = curses.color_pair(1) | curses.A_BOLD
    safe_addstr(stdscr, 0, 0, FR_TL + FR_H * (w - 2) + FR_TR, color)
    safe_addstr(stdscr, h - 2, 0, FR_BL + FR_H * (w - 2) + FR_BR, color)
    for y in range(1, h - 2):
        safe_addstr(stdscr, y, 0, FR_V, color)
        safe_addstr(stdscr, y, w - 1, FR_V, color)


def draw_stars(stdscr, stars, t, phases=None):
    phases = phases or STAR_PHASES
    for sx, sy, seed in stars:
        idx = int((t * 0.8 + seed * len(phases)) % len(phases))
        ch, mode = phases[idx]
        attr = curses.color_pair(8) | attr_from(mode)
        safe_addstr(stdscr, sy, sx, ch, attr)


def draw_platform(stdscr, plat, w, h, theme_def, t):
    if not plat.alive:
        return
    color = theme_def.get('platform_color', 1)
    attr = curses.color_pair(color) | curses.A_BOLD
    if plat.max_hp and plat.hp < plat.max_hp:
        attr |= curses.A_DIM
    chars = '═' * plat.w
    if plat.max_hp:
        # Destructible platforms get a hatched glyph so they read different.
        chars = ('▓' if int(t * 4) % 2 else '░') * plat.w
    safe_addstr(stdscr, plat.y, plat.x, chars[:max(0, w - plat.x - 1)], attr)
    if plat.max_hp:
        pips = '◆' * plat.hp
        x = plat.x + plat.w // 2 - len(pips) // 2
        safe_addstr(stdscr, plat.y - 1, x, pips,
                    curses.color_pair(3) | curses.A_BOLD)


def draw_hazard(stdscr, hz, w, h, t):
    color = curses.color_pair(3) | curses.A_BOLD
    if int(t * 4) % 2 == 0:
        color |= curses.A_REVERSE
    safe_addstr(stdscr, hz.y, hz.x, ('▲' * hz.w)[:max(0, w - hz.x - 1)], color)


def draw_enemy(stdscr, e, w, h, t):
    if not e.alive:
        return
    x = int(round(e.x))
    y = int(round(e.y))
    if e.kind == ENEMY_CRAB:
        body  = curses.color_pair(3) | curses.A_BOLD
        claws = curses.color_pair(2) | curses.A_BOLD
        eyes  = curses.color_pair(1) | curses.A_BOLD
        wiggle = int((t + e.phase) * 4) % 2
        if 1 <= y < h - 2:
            if 1 <= x - 1 < w - 1:
                safe_addstr(stdscr, y, x - 1, '◣' if wiggle else '◤', claws)
            if 1 <= x < w - 1:
                safe_addstr(stdscr, y, x, '◍', body)
            if 1 <= x + 1 < w - 1:
                safe_addstr(stdscr, y, x + 1, '◢' if wiggle else '◥', claws)
            if y - 1 > 0 and 1 <= x < w - 1:
                safe_addstr(stdscr, y - 1, x, '••', eyes)
    else:  # bat
        body = curses.color_pair(6) | curses.A_BOLD
        wing_open = int((t + e.phase) * 6) % 2 == 0
        if 1 <= y < h - 2 and 1 <= x < w - 1:
            chars = '/Λ\\' if wing_open else '_Λ_'
            safe_addstr(stdscr, y, max(1, x - 1), chars[:max(0, w - x - 1)], body)


def draw_chest(stdscr, c, w, h, t):
    if not c.alive:
        return
    x = int(round(c.x))
    y = int(round(c.y))
    bob = int((t + c.phase) * 3) % 2
    attr = curses.color_pair(2) | curses.A_BOLD
    if y - 1 >= 1 and 1 <= x - 1 < w - 2:
        safe_addstr(stdscr, y - 1, x - 1, "┏━┓", attr)
    if 1 <= y < h - 2 and 1 <= x - 1 < w - 2:
        safe_addstr(stdscr, y, x - 1, "┃$┃" if bob else "┃◆┃", attr | curses.A_REVERSE)
    if y + 1 < h - 1 and 1 <= x - 1 < w - 2:
        safe_addstr(stdscr, y + 1, x - 1, "┗━┛", attr)


def draw_ball(stdscr, b, w, h, colorblind=False, t=0.0):
    sprite, cx, cy, row_attrs = BALL_SPRITES[b.size]
    bx, by = int(round(b.x)), int(round(b.y))
    if b.variant == BV_NORMAL:
        color = ball_color_for(b)
    else:
        color = VARIANT_COLOR.get(b.variant, ball_color_for(b))
    # Explosive balls flash red/yellow before pop; boss pulses.
    if b.variant == BV_EXPLOSIVE and int(t * 6 + b.phase) % 2 == 0:
        color = 2
    if b.variant == BV_GOLD:
        color = 2
    base = curses.color_pair(color)
    draw_sprite(stdscr, sprite, by - cy, bx - cx, base, w, h, row_attrs)
    # Overlay variant glyph on the centre for clarity (and colorblind mode).
    if b.variant != BV_NORMAL:
        glyph = VARIANT_GLYPH.get(b.variant, '?')
        attr = curses.color_pair(1) | curses.A_BOLD | curses.A_REVERSE
        if b.size >= 2 and 1 <= by < h - 2 and 1 <= bx < w - 1:
            safe_addstr(stdscr, by, bx, glyph, attr)
    # Boss hp pips beneath the body.
    if b.variant == BV_BOSS and b.hp > 0:
        pips = ('●' * b.hp)[:10]
        x = max(1, bx - len(pips) // 2)
        y = min(h - 3, by + cy + 1)
        safe_addstr(stdscr, y, x, pips,
                    curses.color_pair(3) | curses.A_BOLD)


def draw_projectile(stdscr, proj, w, h, t):
    if not proj.alive:
        return
    x = int(round(proj.x))

    # Power-gun bullet: small pellet + dim trail.
    if proj.kind == PROJ_BULLET:
        y = int(round(proj.tip_y))
        if not (1 <= y < h - 2 and 1 <= x < w - 1):
            return
        ch = '▲' if proj.dy_dir < 0 else '▼'
        attr = curses.color_pair(3) | curses.A_BOLD
        if proj.mirror and not proj.mirror_used:
            attr = curses.color_pair(2) | curses.A_BOLD  # yellow before bounce
        safe_addstr(stdscr, y, x, ch, attr)
        ty = y + (1 if proj.dy_dir < 0 else -1)
        if 1 <= ty < h - 2 and 1 <= x < w - 1:
            safe_addstr(stdscr, ty, x, '·',
                        curses.color_pair(2) | curses.A_DIM)
        return

    top = max(1, int(round(proj.tip_y)))
    bot = min(h - 3, int(round(proj.base_y)))

    # Sticky harpoon parked on the ceiling as a barrier.
    if proj.kind == PROJ_STICKY and proj.stuck:
        remaining = STICKY_LIFE - proj.stuck_t
        body_attr = curses.color_pair(6) | curses.A_BOLD
        if remaining < 1.5 and int(t * 8) % 2 == 0:
            body_attr = curses.color_pair(6) | curses.A_DIM
        for y in range(top, bot + 1):
            if not (1 <= x < w - 1):
                continue
            if y == top:
                ch = '╤'
            else:
                ch = '║' if (y + int(t * 6)) % 3 else '╫'
            safe_addstr(stdscr, y, x, ch, body_attr)
        return

    # In-flight sticky harpoon — magenta shaft with diamond claw.
    if proj.kind == PROJ_STICKY:
        body_attr = curses.color_pair(6) | curses.A_BOLD
        tip_attr  = curses.color_pair(6) | curses.A_BOLD | curses.A_REVERSE
        for y in range(top + 1, bot + 1):
            ch = SHAFT_CHARS[(y + int(t * 25)) % len(SHAFT_CHARS)]
            if 1 <= x < w - 1:
                safe_addstr(stdscr, y, x, ch, body_attr)
        if 1 <= x < w - 1:
            safe_addstr(stdscr, top, x, '◆', tip_attr)
        return

    # Standard harpoon (wide or thin), with optional piercing tint.
    body_attr = curses.color_pair(2) | curses.A_BOLD
    tip_attr  = curses.color_pair(3) | curses.A_BOLD
    glow_attr = curses.color_pair(2) | curses.A_DIM
    if proj.piercing:
        body_attr = curses.color_pair(5) | curses.A_BOLD
        tip_attr  = curses.color_pair(5) | curses.A_BOLD | curses.A_REVERSE
    if proj.wide:
        for y in range(top + 1, bot + 1):
            if 1 <= x - 1 < w - 1:
                safe_addstr(stdscr, y, x - 1, '▌', body_attr)
            ch = '█' if (y + int(t * 25)) % 2 == 0 else '▓'
            if 1 <= x < w - 1:
                safe_addstr(stdscr, y, x, ch, body_attr)
            if 1 <= x + 1 < w - 1:
                safe_addstr(stdscr, y, x + 1, '▐', body_attr)
        tip_char = TIP_CHARS[int(t * 8) % len(TIP_CHARS)]
        if 1 <= x - 1 < w - 1: safe_addstr(stdscr, top, x - 1, '◣', tip_attr)
        if 1 <= x     < w - 1: safe_addstr(stdscr, top, x,     tip_char, tip_attr)
        if 1 <= x + 1 < w - 1: safe_addstr(stdscr, top, x + 1, '◢', tip_attr)
    else:
        for y in range(top + 1, bot + 1):
            ch = SHAFT_CHARS[(y + int(t * 25)) % len(SHAFT_CHARS)]
            if 1 <= x < w - 1:
                safe_addstr(stdscr, y, x, ch, body_attr)
        tip_char = TIP_CHARS[int(t * 8) % len(TIP_CHARS)]
        if 1 <= x < w - 1:
            safe_addstr(stdscr, top, x, tip_char, tip_attr)
        if top + 1 < h - 2 and 1 <= x < w - 1:
            safe_addstr(stdscr, top + 1, x, '╿', glow_attr)


def draw_score_popups(stdscr, popups, w, h):
    for p in popups:
        x, y = int(round(p.x)), int(round(p.y))
        ratio = p.life / p.max_life if p.max_life else 0
        if ratio > 0.5:
            attr = curses.color_pair(p.color) | curses.A_BOLD
        elif ratio > 0.25:
            attr = curses.color_pair(p.color)
        else:
            attr = curses.color_pair(p.color) | curses.A_DIM
        s = p.text
        x_start = max(1, x - len(s) // 2)
        if 1 <= y < h - 2:
            safe_addstr(stdscr, y, x_start, s[:max(0, w - 2 - x_start)], attr)


def player_face(p, t):
    if p.hit_flash > 0:
        return FACE_HIT
    if p.happy_flash > 0:
        return FACE_HAPPY
    if p.fire_flash > 0:
        return FACE_AIM
    if t - p.last_move_t < 0.4:
        return FACE_LEFT if p.facing < 0 else FACE_RIGHT
    if (t % 3.5) < 0.18:
        return FACE_BLINK
    return FACE_IDLE


def player_sprite(p, t):
    """Build the 5-row animated adventurer sprite from state + time."""
    moving = (t - p.last_move_t) < 0.25
    firing = p.fire_flash > 0
    hit    = p.hit_flash > 0

    # Hat: tilts with stride, askew on hit, gentle bob on idle.
    if hit:
        hat = HAT_HIT
    elif firing:
        hat = HAT_FIRE
    elif moving:
        hat = HAT_TILT_L if p.facing < 0 else HAT_TILT_R
    else:
        hat = HAT_IDLE_A if int(t * 1.4) % 2 == 0 else HAT_IDLE_B

    face = player_face(p, t)

    # Arms: weapon-extended on fire, flung-up on hit, swing on walk/idle breath.
    if firing:
        arms = ARMS_FIRE_L if p.facing < 0 else ARMS_FIRE_R
    elif hit:
        arms = ARMS_HIT
    elif moving:
        arms = ARMS_IDLE_A if int(t * 9) % 2 == 0 else ARMS_IDLE_B
    else:
        arms = ARMS_IDLE_A if int(t * 1.6) % 2 == 0 else ARMS_IDLE_B

    # Torso: tensed on fire, jolted on hit, vest otherwise.
    if firing:
        torso = TORSO_FIRE
    elif hit:
        torso = TORSO_HIT
    else:
        torso = TORSO_IDLE

    # Legs: planted on fire, alternating step on walk, otherwise stance.
    if firing:
        legs = LEGS_PLANT
    elif moving:
        legs = LEGS_WALK_A if int(t * 9) % 2 == 0 else LEGS_WALK_B
    else:
        legs = LEGS_STAND

    return [hat, face, arms, torso, legs]


def draw_player(stdscr, p, w, h, t):
    if p.hit_flash > 0:
        base = curses.color_pair(3) | curses.A_BOLD
    elif p.happy_flash > 0:
        base = curses.color_pair(5) | curses.A_BOLD
    else:
        base = curses.color_pair(player_color_for()) | curses.A_BOLD

    sprite = player_sprite(p, t)
    sx = p.x - PLAYER_CX
    sy = p.y - PLAYER_CY
    # Recoil kick: head jumps up briefly during the first half of fire_flash.
    if p.recoil_t > 0.09:
        sy -= 1
    draw_sprite(stdscr, sprite, sy, sx, base, w, h, PLAYER_ATTRS)

    if p.active.get('X'):
        bright = int(t * 4) % 2 == 0
        attr = curses.color_pair(4) | (curses.A_BOLD if bright else curses.A_DIM)
        safe_addstr(stdscr, p.y - 3, p.x - 2, '╲', attr)
        safe_addstr(stdscr, p.y - 2, p.x - 2, '┃', attr)
        safe_addstr(stdscr, p.y - 1, p.x - 2, '┃', attr)
        safe_addstr(stdscr, p.y,     p.x - 2, '┃', attr)
        safe_addstr(stdscr, p.y + 1, p.x - 2, '╱', attr)
        safe_addstr(stdscr, p.y - 3, p.x + 2, '╱', attr)
        safe_addstr(stdscr, p.y - 2, p.x + 2, '┃', attr)
        safe_addstr(stdscr, p.y - 1, p.x + 2, '┃', attr)
        safe_addstr(stdscr, p.y,     p.x + 2, '┃', attr)
        safe_addstr(stdscr, p.y + 1, p.x + 2, '╲', attr)


def draw_powerup(stdscr, pu, w, h, t):
    label, color, _, _ = POWERUPS[pu.kind]
    bx = int(round(pu.draw_x()))
    by = int(round(pu.y))
    blink = pu.life < 3 and int(t * 6) % 2 == 0
    attr = curses.color_pair(color) | curses.A_BOLD
    if blink:
        attr |= curses.A_DIM
    mid = f"┃{label}┃"
    safe_addstr(stdscr, by - 1, bx - 1, POWERUP_BOX_TOP, attr)
    safe_addstr(stdscr, by,     bx - 1, mid,             attr | curses.A_REVERSE)
    safe_addstr(stdscr, by + 1, bx - 1, POWERUP_BOX_BOT, attr)


def draw_particles(stdscr, particles, w, h):
    for p in particles:
        x, y = int(round(p.x)), int(round(p.y))
        if not (1 <= y < h - 2 and 1 <= x < w - 1):
            continue
        ratio = p.life / p.max_life if p.max_life else 0
        if ratio > 0.66:
            ch, mode = '✦', curses.A_BOLD
        elif ratio > 0.33:
            ch, mode = '✧', 0
        else:
            ch, mode = '·', curses.A_DIM
        safe_addstr(stdscr, y, x, ch, curses.color_pair(p.color) | mode)


def draw_hud(stdscr, w, h, p, level, t, mode=MODE_CLASSIC, session_time=0.0,
             hide_score=False, theme=None, shots_left=None, shot_budget=None):
    title = f" {DIAMOND} P A N G  D E L U X E {DIAMOND} "
    safe_addstr(stdscr, 0, max(2, (w - len(title)) // 2), title,
                curses.color_pair(3) | curses.A_BOLD | curses.A_REVERSE)

    if audio.is_muted():
        safe_addstr(stdscr, 0, 2, "[♪ OFF]",
                    curses.color_pair(7) | curses.A_BOLD)
    else:
        safe_addstr(stdscr, 0, 2, "[♪ ON ]",
                    curses.color_pair(5) | curses.A_DIM)

    mode_tag = f"[{MODE_INFO[mode][0]}]"
    if theme:
        mode_tag = f"[{MODE_INFO[mode][0]} · {theme.upper()}]"
    safe_addstr(stdscr, 0, max(11, w - len(mode_tag) - 2), mode_tag,
                curses.color_pair(6) | curses.A_BOLD)

    hearts = ' '.join([HEART] * p.lives) if p.lives > 0 else '--'
    score_str = "******" if hide_score else f"{p.score:06d}"
    line = f" LVL {level:02d}   SCORE {score_str}   {hearts} "
    if mode == MODE_TIME:
        line = (f" LVL {level:02d}/{TIME_ATTACK_LEVELS}  SCORE {score_str}  "
                f"{format_time(session_time)}  {hearts} ")
    elif mode == MODE_PUZZLE and shot_budget is not None:
        line = (f" PUZZLE {level:02d}/{len(PUZZLES)}  SCORE {score_str}  "
                f"SHOTS {shots_left}/{shot_budget}  {hearts} ")
    safe_addstr(stdscr, h - 1, 1, line[:w - 2],
                curses.color_pair(4) | curses.A_BOLD)

    # Combo badge — colour climbs with combo length
    if p.combo >= 2:
        mult = combo_mult(p.combo)
        if   p.combo >= 8: cc = 3      # red, hottest
        elif p.combo >= 5: cc = 2      # yellow
        else:              cc = 5      # green
        badge = f" ✦ COMBO x{p.combo}"
        if mult > 1.0:
            badge += f" ({mult:.1f}×)"
        badge += " "
        bx = max(0, len(line) + 2)
        safe_addstr(stdscr, h - 1, bx, badge[:max(0, w - bx - 2)],
                    curses.color_pair(cc) | curses.A_BOLD | curses.A_REVERSE)

    parts = []
    for k, rem in p.active.items():
        if rem is True:
            parts.append(f"[{POWERUPS[k][0]}]")
        else:
            parts.append(f"[{POWERUPS[k][0]}:{int(rem)+1}s]")
    if parts:
        s = ' '.join(parts)
        safe_addstr(stdscr, h - 1, max(0, w - len(s) - 2), s,
                    curses.color_pair(2) | curses.A_BOLD)


def draw(stdscr, w, h, player, balls, harpoons, powerups, particles, popups,
         level, stars, t, toasts=None, mode=MODE_CLASSIC, session_time=0.0,
         colorblind=False, world=None, hide_score=False, hurry_active=False,
         shots_left=None, shot_budget=None):
    stdscr.erase()
    draw_frame(stdscr, w, h, t)
    draw_stars(stdscr, stars, t,
               phases=world['theme_def']['bg_phases'] if world else None)
    if world:
        for plat in world['platforms']:
            draw_platform(stdscr, plat, w, h, world['theme_def'], t)
        for hz in world['hazards']:
            draw_hazard(stdscr, hz, w, h, t)
        for e in world['enemies']:
            draw_enemy(stdscr, e, w, h, t)
        for c in world['chests']:
            draw_chest(stdscr, c, w, h, t)
    for b in balls:
        draw_ball(stdscr, b, w, h, colorblind=colorblind, t=t)
    for hp in harpoons:
        draw_projectile(stdscr, hp, w, h, t)
    for pu in powerups:
        draw_powerup(stdscr, pu, w, h, t)
    draw_particles(stdscr, particles, w, h)
    draw_player(stdscr, player, w, h, t)
    draw_score_popups(stdscr, popups, w, h)
    draw_hud(stdscr, w, h, player, level, t, mode=mode, session_time=session_time,
             hide_score=hide_score, theme=world['theme'] if world else None,
             shots_left=shots_left, shot_budget=shot_budget)
    if toasts:
        draw_toasts(stdscr, w, h, toasts)
    if player.freeze_t > 0:
        msg = " ❄ FROZEN ❄ "
        x = max(2, (w - len(msg)) // 2)
        safe_addstr(stdscr, h // 2, x, msg,
                    curses.color_pair(4) | curses.A_BOLD | curses.A_REVERSE)
    if hurry_active:
        msg = " ⚠ HURRY UP! ⚠ "
        if int(t * 4) % 2 == 0:
            attr = curses.color_pair(3) | curses.A_BOLD | curses.A_REVERSE
        else:
            attr = curses.color_pair(3) | curses.A_BOLD
        x = max(2, (w - len(msg)) // 2)
        safe_addstr(stdscr, 1, x, msg, attr)
    stdscr.refresh()


# --- Box overlay ------------------------------------------------------------
def draw_overlay_box(stdscr, w, h, lines, color=4, top_pad=1):
    inner_w = max(len(l) for l in lines) + 4
    box_h = len(lines) + 2 + top_pad * 2
    top_y = max(1, (h - box_h) // 2)
    left_x = max(1, (w - inner_w) // 2)
    attr = curses.color_pair(color) | curses.A_BOLD
    safe_addstr(stdscr, top_y, left_x, FR_TL + FR_H * (inner_w - 2) + FR_TR, attr)
    for i in range(1, box_h - 1):
        safe_addstr(stdscr, top_y + i, left_x, FR_V + ' ' * (inner_w - 2) + FR_V, attr)
    safe_addstr(stdscr, top_y + box_h - 1, left_x, FR_BL + FR_H * (inner_w - 2) + FR_BR, attr)
    for i, line in enumerate(lines):
        x = left_x + (inner_w - len(line)) // 2
        safe_addstr(stdscr, top_y + 1 + top_pad + i, x, line, attr)


def show_message(stdscr, w, h, lines, color=4):
    stdscr.nodelay(False)
    stdscr.erase()
    draw_overlay_box(stdscr, w, h, lines, color=color, top_pad=1)
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (ord('m'), ord('M')):
            audio.toggle_mute()
            continue
        if ch == curses.KEY_RESIZE:
            continue
        break
    stdscr.nodelay(True)
    return ch


def format_time(secs):
    secs = max(0.0, secs)
    m = int(secs) // 60
    s = secs - m * 60
    return f"{m:02d}:{s:05.2f}"


def level_intro(stdscr, w, h, mode, level, world, player_lives, draw_world_fn):
    """Show LEVEL N — <subtitle> and wait until the player moves or fires.

    draw_world_fn() should render the current frozen frame so the intro
    looks like a paused screenshot of the upcoming level.
    """
    name  = (world.get('puzzle_name')
             or ("BOSS" if is_boss_level(mode, level) else None)
             or f"LEVEL {level}")
    mode_label = MODE_INFO[mode][0]
    subtitle = name
    extra_lines = []
    if mode == MODE_PUZZLE:
        budget = world.get('shot_budget')
        if budget is not None:
            extra_lines.append(f"Shots allowed: {budget}")
    if is_boss_level(mode, level):
        extra_lines.append("Boss fight!")
    if mode == MODE_TIME:
        extra_lines.append(f"{TIME_ATTACK_LEVELS - level + 1} levels remaining")
    hearts = (HEART + ' ') * player_lives
    extra_lines.append(f"Lives: {hearts.strip()}")
    extra_lines.append("")
    extra_lines.append("Move or shoot to begin · Q to quit")

    t0 = time.monotonic()
    stdscr.nodelay(True)
    while True:
        t = time.monotonic() - t0
        draw_world_fn()
        # Compose box lines: title + subtitle + spacer + extras
        lines = [
            f" ▶  {mode_label}  ◀ ",
            f"   {subtitle}   ",
            "",
        ] + extra_lines
        draw_overlay_box(stdscr, w, h, lines,
                         color=5 if int(t * 2) % 2 == 0 else 4, top_pad=1)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == -1:
            time.sleep(TICK)
            continue
        if ch == curses.KEY_RESIZE:
            continue
        if ch in (ord('q'), ord('Q')):
            return False
        if ch in (ord('m'), ord('M')):
            audio.toggle_mute()
            continue
        if ch in (ord('j'), ord('J')):
            _MUSIC_OFF[0] = not _MUSIC_OFF[0]
            if _MUSIC_OFF[0]:
                audio.stop_music()
            continue
        if ch in (ord('p'), ord('P'), 27):
            pause_overlay(stdscr, w, h)
            continue
        # Any other input begins the level.
        audio.sfx('select')
        return True


def pause_overlay(stdscr, w, h):
    show_message(stdscr, w, h, [
        "▶▶  P A U S E D  ◀◀",
        "",
        "Press any key to resume",
        "(M to mute · Q to quit during play)",
    ], color=4)


def draw_toasts(stdscr, w, h, toasts):
    # Bottom-right vertical stack of fading achievement toasts.
    y0 = h - 3
    for i, (text, life, life_max) in enumerate(toasts[-3:]):
        ratio = life / life_max if life_max else 0
        if ratio > 0.66:
            attr = curses.color_pair(2) | curses.A_BOLD | curses.A_REVERSE
        elif ratio > 0.33:
            attr = curses.color_pair(2) | curses.A_BOLD
        else:
            attr = curses.color_pair(2) | curses.A_DIM
        msg = f" ★ {text} "
        x = max(2, w - len(msg) - 2)
        y = y0 - i
        if 1 <= y < h - 1:
            safe_addstr(stdscr, y, x, msg[:w - 4], attr)


def mode_selector(stdscr, w, h, save):
    """Lets the user pick a mode. Returns mode string or None to quit."""
    idx = 0
    stdscr.nodelay(True)
    t0 = time.monotonic()
    while True:
        stdscr.erase()
        draw_frame(stdscr, w, h, time.monotonic() - t0)
        title = f" {DIAMOND}  SELECT MODE  {DIAMOND} "
        x = max(2, (w - len(title)) // 2)
        safe_addstr(stdscr, max(1, h // 2 - 8), x, title,
                    curses.color_pair(3) | curses.A_BOLD | curses.A_REVERSE)
        base_y = max(3, h // 2 - 5)
        for i, m in enumerate(MODE_ORDER):
            name, desc = MODE_INFO[m]
            sel = (i == idx)
            row_attr = (curses.color_pair(2) | curses.A_BOLD | curses.A_REVERSE
                        if sel else curses.color_pair(5) | curses.A_BOLD)
            label = f"  {'▶ ' if sel else '  '}{name:<12}  {desc}  "
            xl = max(2, (w - len(label)) // 2)
            safe_addstr(stdscr, base_y + i * 2, xl,
                        label[:w - 4], row_attr)
            # Show top score (or today's daily) under the selected entry.
            if sel:
                if m == MODE_DAILY:
                    today = time.strftime('%Y-%m-%d')
                    entry = save['daily'].get(today)
                    if entry:
                        sub = f"  Today: {entry['score']} (L{entry['level']})"
                    else:
                        sub = "  No run yet today"
                else:
                    table = save['high_scores'].get(m, [])
                    if table:
                        sub = f"  Top: {table[0]['score']} (L{table[0]['level']})"
                    else:
                        sub = "  No runs yet"
                xs = max(2, (w - len(sub)) // 2)
                safe_addstr(stdscr, base_y + i * 2 + 1, xs, sub,
                            curses.color_pair(4))
        prompt = " ▲ ▼  pick   SPACE / ENTER  start   Q  back "
        xp = max(2, (w - len(prompt)) // 2)
        safe_addstr(stdscr, h - 2, xp, prompt,
                    curses.color_pair(4) | curses.A_BOLD)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == curses.KEY_RESIZE:
            h, w = stdscr.getmaxyx()
        elif ch in (curses.KEY_UP, ord('w'), ord('W')):
            idx = (idx - 1) % len(MODE_ORDER)
            audio.sfx('select')
        elif ch in (curses.KEY_DOWN, ord('s'), ord('S')):
            idx = (idx + 1) % len(MODE_ORDER)
            audio.sfx('select')
        elif ch in (ord(' '), 10, 13, curses.KEY_ENTER):
            audio.sfx('select')
            return MODE_ORDER[idx]
        elif ch in (ord('q'), ord('Q'), 27):
            return None
        elif ch in (ord('m'), ord('M')):
            audio.toggle_mute()
        time.sleep(TICK)


# --- Title screen -----------------------------------------------------------
def cycle(seq, current, step=1):
    if current not in seq:
        return seq[0]
    i = (seq.index(current) + step) % len(seq)
    return seq[i]


def tutorial_screen(stdscr, w, h):
    """Walk the player through the basics across five short screens."""
    pages = [
        [
            " ✦  WELCOME TO PANG DELUXE  ✦ ",
            "",
            "Pop every ball on screen to clear a level.",
            "Big balls split into smaller ones — until they vanish.",
            "",
            "Use   ◀ ▶   or   A / D   to move.",
            "",
            "Press any key to continue · Q to skip",
        ],
        [
            " ✦  SHOOTING  ✦ ",
            "",
            "SPACE fires a harpoon straight up.",
            "Only one shot in flight at a time — make it count.",
            "",
            "The harpoon dies on hit or when it reaches the top.",
            "",
            "Press any key to continue",
        ],
        [
            " ✦  COMBOS  ✦ ",
            "",
            "Pop multiple balls in a row to build a COMBO.",
            "Each combo step multiplies the score (up to 3×).",
            "",
            "But miss a beat for >1.5 s and the combo resets.",
            "",
            "Press any key to continue",
        ],
        [
            " ✦  POWERUPS  ✦ ",
            "",
            "Balls sometimes drop boxes when popped.",
            "Walk over a box to grab the powerup inside.",
            "",
            "Examples: SH shield · 2x double · MG magnet · PI pierce",
            "          HK sticky barrier · GN power gun · GH ghost",
            "",
            "Press any key to continue",
        ],
        [
            " ✦  STICKY HOOK  ✦ ",
            "",
            "Pick up HK and your harpoons stick to the ceiling.",
            "Each sticky becomes a column that pops the next ball",
            "to touch it. Up to 3 barriers up at once.",
            "",
            "Pair with 3X for instant force fields.",
            "",
            "Press any key to start playing",
        ],
    ]
    stdscr.nodelay(False)
    rng = random.Random(42)
    stars = [(rng.randint(2, w - 3), rng.randint(2, h - 4), rng.random())
             for _ in range(30)]
    for i, lines in enumerate(pages):
        t0 = time.monotonic()
        stdscr.erase()
        draw_frame(stdscr, w, h, 0)
        draw_stars(stdscr, stars, time.monotonic() - t0)
        # Page indicator
        nav = f"  page {i + 1} / {len(pages)}  "
        safe_addstr(stdscr, h - 2, max(2, (w - len(nav)) // 2), nav,
                    curses.color_pair(7) | curses.A_DIM)
        draw_overlay_box(stdscr, w, h, lines, color=4, top_pad=1)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord('q'), ord('Q')):
            break
    stdscr.nodelay(True)


def title_screen(stdscr, w, h, save=None):
    stdscr.nodelay(True)
    play_music('TITLE')
    rng = random.Random()
    stars = [(rng.randint(2, w - 3), rng.randint(2, h - 4), rng.random())
             for _ in range(60)]
    t0 = time.monotonic()

    while True:
        t = time.monotonic() - t0
        stdscr.erase()
        draw_frame(stdscr, w, h, t)
        draw_stars(stdscr, stars, t)

        # Vertical layout — anchor the title block at one-third from the top
        # so the upper half stays airy and the bottom is just one status line.
        art_w  = max(len(line) for line in PANG_ART)
        art_x  = max(2, (w - art_w) // 2)
        art_y  = max(2, h // 3 - len(PANG_ART) // 2)

        # Cyan body with a softer dim shadow line underneath — much calmer
        # than the previous magenta/red flashing.
        body_attr   = curses.color_pair(4) | curses.A_BOLD
        shadow_attr = curses.color_pair(7) | curses.A_DIM
        for i, line in enumerate(PANG_ART):
            safe_addstr(stdscr, art_y + i, art_x, line, body_attr)
        for line in PANG_ART_SHADOW:
            safe_addstr(stdscr, art_y + len(PANG_ART), art_x, line, shadow_attr)

        # Subtle "DELUXE" tag a hair below the shadow.
        sub = f"{DIAMOND}  D E L U X E  {DIAMOND}"
        safe_addstr(stdscr, art_y + len(PANG_ART) + 2,
                    max(2, (w - len(sub)) // 2),
                    sub, curses.color_pair(2) | curses.A_BOLD)

        # Blinking call to action — single line, bold, centred.
        cta_y = art_y + len(PANG_ART) + 5
        if int(t * 2) % 2 == 0:
            cta = "▶▶▶   press SPACE to play   ◀◀◀"
            cta_attr = curses.color_pair(3) | curses.A_BOLD | curses.A_REVERSE
        else:
            cta = "      press SPACE to play       "
            cta_attr = curses.color_pair(3) | curses.A_BOLD
        safe_addstr(stdscr, cta_y, max(2, (w - len(cta)) // 2), cta, cta_attr)

        # Single options line — everything cyclable on one row.
        diff_key  = save['settings'].get('difficulty', 'normal') if save else 'normal'
        char_key  = save['settings'].get('character',  'classic') if save else 'classic'
        skin_key  = save['settings'].get('skin', 'classic') if save else 'classic'
        if save and not is_skin_unlocked(skin_key, save):
            skin_key = 'classic'
        unlocked_n = len(unlocked_skins(save)) if save else 1
        opts = (f"[1] {diff_key.upper():>6s}  ·  "
                f"[2] {CHARACTERS[char_key]['label']:<7s}  ·  "
                f"[3] {SKINS[skin_key]['label']:<7s} {unlocked_n}/{len(SKIN_ORDER)}  ·  "
                f"[T] Tutorial")
        opts_y = cta_y + 2
        safe_addstr(stdscr, opts_y, max(2, (w - len(opts)) // 2),
                    opts, curses.color_pair(5) | curses.A_BOLD)

        # Best score (only if any has been recorded).
        if save:
            best = 0
            best_mode = None
            for m, table in save['high_scores'].items():
                if table and table[0]['score'] > best:
                    best = table[0]['score']
                    best_mode = m
            if best > 0:
                bm = MODE_INFO.get(best_mode, ('?', ''))[0]
                s = f"best  {best:,}  in {bm}"
                safe_addstr(stdscr, opts_y + 1,
                            max(2, (w - len(s)) // 2),
                            s, curses.color_pair(2))

        # Bottom status row — minimal: audio left, hotkey hints right.
        if audio.is_muted():
            mus = "[♪ muted — press M]"
            mus_attr = curses.color_pair(7) | curses.A_BOLD
        elif _MUSIC_OFF[0]:
            mus = "[♪ off — press J]"
            mus_attr = curses.color_pair(7) | curses.A_DIM
        else:
            mus = "[♪ chiptune]"
            mus_attr = curses.color_pair(5) | curses.A_DIM
        safe_addstr(stdscr, h - 1, 2, mus, mus_attr)
        hint = "Q quit"
        safe_addstr(stdscr, h - 1, max(2, w - len(hint) - 2), hint,
                    curses.color_pair(7) | curses.A_DIM)

        stdscr.refresh()
        ch = stdscr.getch()
        if ch == curses.KEY_RESIZE:
            h, w = stdscr.getmaxyx()
            stars = [(rng.randint(2, max(3, w - 3)),
                      rng.randint(2, max(3, h - 4)), rng.random()) for _ in range(60)]
        elif ch in (ord('q'), ord('Q')):
            return False
        elif ch in (ord('m'), ord('M')):
            audio.toggle_mute()
        elif ch == ord('1') and save:
            save['settings']['difficulty'] = cycle(
                list(DIFFICULTY_ORDER),
                save['settings'].get('difficulty', 'normal'))
            write_save(save)
            audio.sfx('select')
        elif ch == ord('2') and save:
            save['settings']['character'] = cycle(
                list(CHARACTER_ORDER),
                save['settings'].get('character', 'classic'))
            write_save(save)
            audio.sfx('select')
        elif ch == ord('3') and save:
            avail = unlocked_skins(save)
            cur = save['settings'].get('skin', 'classic')
            if cur not in avail:
                cur = 'classic'
            save['settings']['skin'] = cycle(avail, cur)
            write_save(save)
            audio.sfx('select')
        elif ch in (ord('t'), ord('T')) and save:
            tutorial_screen(stdscr, w, h)
        elif ch != -1:
            audio.sfx('select')
            return True
        time.sleep(TICK)


def player_face_demo(t):
    cycle = (t * 0.7) % 6.0
    if cycle < 0.18:
        return FACE_BLINK
    if cycle < 1.6:
        return FACE_IDLE
    if cycle < 3.0:
        return FACE_LEFT
    if cycle < 4.4:
        return FACE_RIGHT
    return FACE_HAPPY


class _DemoPlayer:
    """Fake Player for the menu showcase — cycles through animation states."""
    __slots__ = ("x", "y", "facing", "last_move_t",
                 "hit_flash", "happy_flash", "fire_flash", "recoil_t")
    def __init__(self):
        self.x = self.y = 0
        self.facing = 0
        self.last_move_t = -10.0
        self.hit_flash = 0.0
        self.happy_flash = 0.0
        self.fire_flash = 0.0
        self.recoil_t = 0.0


_DEMO_PLAYER = _DemoPlayer()


def player_sprite_demo(t):
    """Drive the showcase by simulating state changes over an 8s loop."""
    p = _DEMO_PLAYER
    p.hit_flash = p.happy_flash = p.fire_flash = p.recoil_t = 0.0
    phase = t % 8.0
    if phase < 1.8:                       # idle
        p.last_move_t = -10.0
        p.facing = 0
    elif phase < 3.4:                     # walk left
        p.last_move_t = t
        p.facing = -1
    elif phase < 5.0:                     # walk right
        p.last_move_t = t
        p.facing = 1
    elif phase < 5.7:                     # fire pose
        p.fire_flash = 0.2
        p.facing = 1
    elif phase < 6.3:                     # happy
        p.happy_flash = 0.3
    elif phase < 6.9:                     # hit
        p.hit_flash = 0.3
    else:                                 # back to idle
        p.last_move_t = -10.0
        p.facing = 0
    return player_sprite(p, t)


# --- Game session -----------------------------------------------------------
def _handle_resize(stdscr, w, h):
    """Returns (new_w, new_h, ok). If too small, returns ok=False."""
    h2, w2 = stdscr.getmaxyx()
    if (w2, h2) == (w, h):
        return w, h, True
    if w2 < 64 or h2 < 22:
        show_message(stdscr, w2, h2, [
            "Terminal too small.",
            f"Got {w2}x{h2}, need >= 64x22.",
            "Resize and press any key.",
        ])
        h2, w2 = stdscr.getmaxyx()
        if w2 < 64 or h2 < 22:
            return w2, h2, False
    return w2, h2, True


def play_session(stdscr, w, h, save, mode=MODE_CLASSIC):
    """Run one session in the chosen mode. Returns True to re-enter selector."""
    base_seed = daily_seed() if mode == MODE_DAILY else 0
    difficulty = save['settings'].get('difficulty', 'normal')
    character  = save['settings'].get('character', 'classic')
    if difficulty not in DIFFICULTIES:
        difficulty = 'normal'
    if character not in CHARACTERS:
        character = 'classic'
    # Daily challenge must be a fair fight — force normal difficulty so the
    # leaderboard isn't decided by who picked Easy.
    if mode == MODE_DAILY:
        difficulty = 'normal'
    char_def = CHARACTERS[character]
    diff_def = DIFFICULTIES[difficulty]
    skin_id  = save['settings'].get('skin', 'classic')
    if not is_skin_unlocked(skin_id, save):
        skin_id = 'classic'
    _ACTIVE_SKIN[0] = skin_id

    player = Player(w // 2, h - 4)
    player.lives = max(1, char_def['lives'] + diff_def['lives_mod'])
    if mode == MODE_SURVIVAL:
        player.lives = 1
    elif mode == MODE_BOSS_RUSH:
        player.lives = max(2, char_def['lives'] + diff_def['lives_mod'] + 2)
    player_step = char_def['step']
    fire_cd_mod = char_def['fire_cooldown']

    level = 1
    rng = random.Random(base_seed if base_seed else None)
    stars = [(rng.randint(2, w - 3), rng.randint(2, h - 4), rng.random())
             for _ in range(28)]

    session_t0 = time.monotonic()
    toasts = []
    colorblind = bool(save['settings'].get('colorblind', False))
    hide_score = False

    while True:
        play_music(gameplay_song(level))
        world        = build_world(mode, level, w, h, base_seed, difficulty)
        balls        = world['balls']
        platforms    = world['platforms']
        hazards      = world['hazards']
        enemies      = world['enemies']
        chests       = world['chests']
        theme_def    = world['theme_def']
        gravity_mult = theme_def['gravity_mult']
        wrap_edges   = theme_def['wrap']
        hurry_t      = diff_def['hurry_t']
        drop_chance  = diff_def['drop_chance']
        shot_budget  = world.get('shot_budget')
        shots_fired  = 0
        harpoons     = []
        powerups     = []
        particles    = []
        score_popups = []
        last_wall_t  = -10.0
        hazard_cd    = 0.0
        auto_fire_t  = 0.0
        snapshots    = deque(maxlen=max(2, REWIND_FRAMES // REWIND_SAMPLE_EVERY))
        snapshot_frame = 0
        music_alive  = True
        music_started_at = time.monotonic()
        no_damage_this_level = True
        no_pu_this_level     = True

        # Pre-level intro: draw a frozen frame and wait for the player.
        def _intro_frame():
            draw(stdscr, w, h, player, balls, harpoons, powerups,
                 [], [], level, stars, 0.0,
                 toasts=toasts, mode=mode, session_time=0.0,
                 colorblind=colorblind, world=world,
                 hide_score=hide_score, hurry_active=False,
                 shots_left=shot_budget, shot_budget=shot_budget)
        if not level_intro(stdscr, w, h, mode, level, world,
                           player.lives, _intro_frame):
            return False
        last = time.monotonic()
        t0   = last

        while (balls or enemies) and player.lives > 0:
            now = time.monotonic()
            dt_real = min(0.05, now - last)
            last = now
            t = now - t0
            frozen = player.freeze_t > 0
            ball_mod = (0.0 if frozen else 1.0) * speed_factor(player)
            hurry_active = t > hurry_t and not is_boss_level(mode, level)
            if hurry_active:
                ball_mod *= 1.5
            dt_balls = dt_real * ball_mod
            session_time = now - session_t0
            if hazard_cd > 0:
                hazard_cd = max(0.0, hazard_cd - dt_real)
            if auto_fire_t > 0:
                auto_fire_t = max(0.0, auto_fire_t - dt_real)

            # --- timers ---
            for attr in ('hit_flash', 'happy_flash', 'fire_flash',
                         'recoil_t', 'freeze_t', 'shake_t'):
                v = getattr(player, attr)
                if v > 0:
                    setattr(player, attr, max(0.0, v - dt_real))
            # Combo timer pauses while frozen so an ice freeze can't eat it.
            if frozen and player.combo > 0:
                player.combo_t += dt_real
            if player.combo > 0 and t - player.combo_t > 1.5:
                player.combo = 0
            for tt in toasts:
                tt[1] -= dt_real
            toasts[:] = [tt for tt in toasts if tt[1] > 0]

            # --- Input ---
            ch = stdscr.getch()
            while ch != -1:
                if ch == curses.KEY_RESIZE:
                    w, h, ok = _handle_resize(stdscr, w, h)
                    if not ok:
                        return False
                    player.x = min(max(2, player.x), w - 3)
                    player.y = h - 4
                    # Rebuild only the geometry (platforms/hazards) at the new
                    # screen size; keep balls/enemies/chests so their physics
                    # state isn't lost mid-level.
                    fresh = build_world(mode, level, w, h, base_seed, difficulty)
                    platforms = fresh['platforms']
                    hazards   = fresh['hazards']
                    world['platforms'] = platforms
                    world['hazards']   = hazards
                    last = time.monotonic()
                elif ch in (ord('q'), ord('Q')):
                    return False
                elif ch in (ord('p'), ord('P'), 27):
                    pause_overlay(stdscr, w, h)
                    last = time.monotonic()
                elif ch in (ord('m'), ord('M')):
                    audio.toggle_mute()
                elif ch in (ord('j'), ord('J')):
                    _MUSIC_OFF[0] = not _MUSIC_OFF[0]
                    if _MUSIC_OFF[0]:
                        audio.stop_music()
                    else:
                        play_music(gameplay_song(level))
                elif ch in (ord('c'), ord('C')):
                    colorblind = not colorblind
                    save['settings']['colorblind'] = colorblind
                    write_save(save)
                elif ch in (ord('h'), ord('H')):
                    hide_score = not hide_score
                elif frozen:
                    pass
                elif ch in (curses.KEY_LEFT, ord('a'), ord('A')):
                    player.x -= player_step
                    if wrap_edges:
                        if player.x < 2:
                            player.x = w - 3
                    else:
                        player.x = max(2, player.x)
                    player.facing = -1
                    player.last_move_t = t
                elif ch in (curses.KEY_RIGHT, ord('d'), ord('D')):
                    player.x += player_step
                    if wrap_edges:
                        if player.x > w - 3:
                            player.x = 2
                    else:
                        player.x = min(w - 3, player.x)
                    player.facing = 1
                    player.last_move_t = t
                elif ch == ord(' '):
                    if shot_budget is None or shots_fired < shot_budget:
                        in_flight = [p for p in harpoons if not p.stuck]
                        if len(in_flight) < weapon_max_in_flight(player):
                            spawn_shot(player, h, harpoons)
                            player.fire_flash = 0.18
                            player.recoil_t   = 0.18
                            spawn_muzzle_flash(particles, player.x, h - 5)
                            audio.sfx('shoot')
                            shots_fired += 1
                ch = stdscr.getch()

            tick_powerups(player, dt_real)
            if player.active:
                no_pu_this_level = False

            # --- Auto-aim: rate-limited shot at the closest target ---
            if (player.active.get('U') and not frozen and auto_fire_t <= 0
                    and (shot_budget is None or shots_fired < shot_budget)):
                target_x = closest_target_x(
                    balls, enemies, chests, player.x, w, wrap_edges)
                if target_x is not None:
                    in_flight = [p for p in harpoons if not p.stuck]
                    if len(in_flight) < weapon_max_in_flight(player):
                        ox = player.x
                        player.x = int(target_x)
                        spawn_shot(player, h, harpoons)
                        player.x = ox
                        spawn_muzzle_flash(particles, target_x, h - 5)
                        audio.sfx('shoot')
                        shots_fired += 1
                        auto_fire_t = max(0.25, 0.45 + fire_cd_mod)

            # --- Updates ---
            for hp in harpoons:
                hp.update(dt_real, h)
            for hp in harpoons:
                if hp.alive and not hp.stuck and hp.kind == PROJ_HARPOON:
                    spawn_harpoon_trail(particles, hp)
            for b in balls:
                pre_vx = b.vx
                b.update(dt_balls, w, h, player_x=player.x,
                         gravity_mult=gravity_mult, wrap=wrap_edges)
                if b.size >= 3 and (b.vx * pre_vx < 0) and (t - last_wall_t > 0.18):
                    audio.sfx('wall')
                    last_wall_t = t
                for plat in platforms:
                    if b.collide_platform(plat):
                        break
            for e in enemies:
                if e.alive:
                    e.update(dt_real, w, h, player.x, player.y,
                             GRAVITY * gravity_mult)
            for pu in powerups:
                pu.update(dt_real, h)
            if player.active.get('M'):
                for pu in powerups:
                    if pu.landed:
                        continue          # don't slide grounded boxes
                    dx, _ = wrap_dx(player.x, pu.x, w, wrap_edges)
                    pu.x += dx * MAGNET_PULL * dt_real
                    if wrap_edges:
                        span = max(1.0, w - 3.0)
                        if pu.x < 1: pu.x += span
                        elif pu.x > w - 2: pu.x -= span
            for pp in particles:
                pp.update(dt_real)
            for sp in score_popups:
                sp.update(dt_real)

            harpoons     = [hp for hp in harpoons     if hp.alive]
            powerups     = [pu for pu in powerups     if pu.life > 0]
            particles    = [pp for pp in particles    if pp.life > 0]
            score_popups = [sp for sp in score_popups if sp.life > 0]

            if sum(1 for hp in harpoons if hp.stuck) >= 3:
                unlock(save, 'sticky_wall', toasts)

            # --- Projectile ↔ ball ---  (marked-dead: O(n+m) per frame)
            new_balls = []
            for hp in harpoons:
                if not hp.alive or not hp.can_hit():
                    continue
                hw = 1.0 if hp.wide else 0.0
                pierced_this_shot = 0
                for b in balls:
                    if not b.alive:
                        continue
                    if not hp.can_hit():
                        break
                    if not b.hits_vline(hp.x, hp.tip_y, hp.base_y, hw):
                        continue

                    if b.variant == BV_SPIKE and not hp.piercing:
                        spawn_explosion(particles, hp.x, hp.tip_y, 6, 1)
                        audio.sfx('wall')
                        hp.alive = False
                        break

                    if b.variant == BV_BOSS:
                        b.hp -= 1
                        spawn_explosion(particles, b.x, b.y, 18, 3)
                        audio.sfx('pop', size=4)
                        pts = POINTS[4] // 2
                        player.score += pts
                        score_popups.append(ScorePopup(
                            b.x, b.y, f"+{pts}", color=3))
                        if b.hp <= 0:
                            b.alive = False
                            new_balls.extend(split_ball(b))
                            spawn_explosion(particles, b.x, b.y, 60, 2)
                            player.score += POINTS[4] * 5
                            unlock(save, 'first_boss', toasts)
                            player.shake_t = max(player.shake_t, 0.35)
                            # Boss drops a powerup with high probability —
                            # otherwise Boss Rush leaves you starved.
                            if random.random() < max(0.75, drop_chance * 2):
                                powerups.append(Powerup(
                                    b.x, b.y, random_powerup_kind()))
                                audio.sfx('powerup_spawn')
                        if hp.piercing and hp.kind != PROJ_STICKY:
                            hp.hit_cooldown = PIERCE_COOLDOWN
                        else:
                            hp.alive = False
                        break

                    b.alive = False
                    new_balls.extend(split_ball(b))

                    if t - player.combo_t < 1.5:
                        player.combo += 1
                    else:
                        player.combo = 1
                    player.combo_t = t
                    mult = combo_mult(player.combo)

                    if b.variant == BV_GOLD:
                        base_pts = POINTS[b.size] * GOLD_MULT
                        unlock(save, 'gold_pop', toasts)
                    else:
                        base_pts = POINTS[b.size]
                    gain = int(base_pts * mult)
                    player.score += gain

                    txt = f"+{gain}"
                    if mult > 1.0:
                        txt += f" x{mult:.1f}"
                    pop_color = VARIANT_COLOR.get(b.variant, BALL_COLOR[b.size])
                    score_popups.append(ScorePopup(
                        b.x, b.y, txt, color=pop_color))

                    spawn_explosion(particles, b.x, b.y,
                                    16 + (b.size * 4),
                                    BALL_COLOR[b.size])
                    audio.sfx('pop', size=b.size)
                    if player.combo >= 2:
                        audio.sfx('combo', level=player.combo)
                    if player.combo == 3:
                        unlock(save, 'combo_3', toasts)
                    if player.combo >= 8:
                        unlock(save, 'combo_8', toasts)

                    if b.variant == BV_EXPLOSIVE:
                        spawn_explosion(particles, b.x, b.y, 35, 3)
                        audio.sfx('bomb')
                        player.shake_t = max(player.shake_t, 0.25)
                        cx, cy = b.x, b.y
                        for other in balls:
                            if not other.alive:
                                continue
                            if (other.x - cx) ** 2 + (other.y - cy) ** 2 \
                                    < EXPLOSIVE_RANGE ** 2:
                                other.alive = False
                                new_balls.extend(split_ball(other))
                                player.score += POINTS[other.size] // 2
                                spawn_explosion(particles, other.x, other.y,
                                                12, BALL_COLOR[other.size])

                    if random.random() < drop_chance:
                        powerups.append(Powerup(
                            b.x, b.y, random_powerup_kind()))
                        audio.sfx('powerup_spawn')

                    unlock(save, 'first_pop', toasts)

                    if hp.piercing and hp.kind != PROJ_STICKY:
                        hp.hit_cooldown = PIERCE_COOLDOWN
                        pierced_this_shot += 1
                        if pierced_this_shot >= 2:
                            unlock(save, 'pierce_combo', toasts)
                    else:
                        hp.alive = False
                        break

            harpoons = [hp for hp in harpoons if hp.alive]
            balls = [b for b in balls if b.alive] + new_balls

            # --- Projectile ↔ destructible platform ---
            for hp in harpoons:
                if not hp.alive:
                    continue
                for plat in platforms:
                    if not plat.alive or not plat.max_hp:
                        continue
                    if (plat.x <= hp.x <= plat.x + plat.w
                            and min(hp.tip_y, hp.base_y) <= plat.y
                            <= max(hp.tip_y, hp.base_y)):
                        plat.hp -= 1
                        spawn_explosion(particles, hp.x, plat.y, 6,
                                        theme_def['platform_color'])
                        if plat.hp <= 0:
                            plat.alive = False
                            audio.sfx('bomb')
                            player.shake_t = max(player.shake_t, 0.2)
                        else:
                            audio.sfx('wall')
                        hp.alive = False
                        break

            # --- Projectile ↔ enemy ---
            for hp in harpoons:
                if not hp.alive:
                    continue
                for e in enemies:
                    if not e.alive:
                        continue
                    if e.hits_projectile(hp):
                        e.hp -= 1
                        spawn_explosion(particles, e.x, e.y, 14, 3)
                        audio.sfx('pop', size=2)
                        if e.hp <= 0:
                            e.alive = False
                            gain = 150 if e.kind == ENEMY_CRAB else 220
                            player.score += gain
                            score_popups.append(ScorePopup(
                                e.x, e.y, f"+{gain}", color=3))
                            if random.random() < drop_chance * 0.5:
                                powerups.append(Powerup(
                                    e.x, e.y, random_powerup_kind()))
                                audio.sfx('powerup_spawn')
                        if hp.piercing and hp.kind != PROJ_STICKY:
                            hp.hit_cooldown = PIERCE_COOLDOWN
                        else:
                            hp.alive = False
                        break

            # --- Projectile ↔ chest ---
            for hp in harpoons:
                if not hp.alive:
                    continue
                for c in chests:
                    if not c.alive:
                        continue
                    if c.hits_projectile(hp):
                        c.hp -= 1
                        spawn_explosion(particles, c.x, c.y, 8, 2)
                        audio.sfx('wall')
                        if c.hp <= 0:
                            c.alive = False
                            kind = c.kind or random_powerup_kind()
                            powerups.append(Powerup(c.x, c.y, kind))
                            audio.sfx('powerup_spawn')
                        if hp.piercing and hp.kind != PROJ_STICKY:
                            hp.hit_cooldown = PIERCE_COOLDOWN
                        else:
                            hp.alive = False
                        break

            harpoons  = [hp for hp in harpoons if hp.alive]
            enemies   = [e for e in enemies if e.alive]
            chests    = [c for c in chests if c.alive]
            platforms = [p for p in platforms if p.alive]

            # --- Player ↔ powerup ---
            bomb_pops = 0
            rewind_triggered = False
            for pu in list(powerups):
                if pu.caught_by(player.x, player.y):
                    if pu.kind == 'B':
                        bomb_pops = sum(1 for b in balls if b.alive)
                    if pu.kind == 'Z':
                        rewind_triggered = True
                    else:
                        apply_powerup_pickup(player, pu.kind, balls, particles)
                    spawn_explosion(particles, pu.x, pu.y, 12,
                                    POWERUPS[pu.kind][1])
                    audio.sfx('powerup', kind=pu.kind)
                    powerups.remove(pu)
            if bomb_pops >= 5:
                unlock(save, 'bomb_5', toasts)
            balls = [b for b in balls if b.alive]

            if rewind_triggered and snapshots:
                # Pop the oldest snapshot (~2s ago) and restore from it.
                snap = snapshots.popleft()
                balls, harpoons, enemies, powerups, chests = \
                    restore_state(snap, player)
                snapshots.clear()
                # Clear cosmetic-only state so the screen matches what was
                # actually saved (no orphan popups or sparks from later frames).
                particles.clear()
                score_popups.clear()
                player.happy_flash = 0.5
                player.shake_t = 0.2
                spawn_explosion(particles, player.x, player.y, 30, 6)
                # Keep frame timing sane post-rewind.
                last = time.monotonic()

            # --- Player ↔ hazard (spikes) ---
            ghost = bool(player.active.get('V'))
            if not frozen and not ghost and hazard_cd <= 0:
                for hz in hazards:
                    if hz.contains(player.x):
                        hazard_cd = 1.2
                        consumed_shield = damage_player(
                            player, particles, w, source_x=hz.x + hz.w / 2)
                        if not consumed_shield:
                            no_damage_this_level = False
                            _shake_set(player)
                            draw(stdscr, w, h, player, balls, harpoons, powerups,
                                 particles, score_popups, level, stars, t,
                                 toasts=toasts, mode=mode, session_time=session_time,
                                 colorblind=colorblind, world=world,
                                 hide_score=hide_score, hurry_active=hurry_active, shots_left=(shot_budget - shots_fired) if shot_budget is not None else None, shot_budget=shot_budget)
                            _shake_clear()
                            if player.lives <= 0:
                                break
                        break

            if player.lives <= 0:
                break

            # --- Player ↔ enemy ---
            if not frozen and not ghost:
                enemy_hit = next((e for e in enemies
                                  if e.alive and e.hits_player(player.x, player.y)),
                                 None)
                if enemy_hit is not None:
                    consumed = damage_player(player, particles, w,
                                             source_x=enemy_hit.x)
                    if not consumed:
                        no_damage_this_level = False
                        _shake_set(player)
                        draw(stdscr, w, h, player, balls, harpoons, powerups,
                             particles, score_popups, level, stars, t,
                             toasts=toasts, mode=mode, session_time=session_time,
                             colorblind=colorblind, world=world,
                             hide_score=hide_score, hurry_active=hurry_active, shots_left=(shot_budget - shots_fired) if shot_budget is not None else None, shot_budget=shot_budget)
                        _shake_clear()
                        if player.lives <= 0:
                            break
                        # Bump the enemy aside so it doesn't insta-rehit.
                        enemy_hit.x += (10 if enemy_hit.x < player.x else -10)

            if player.lives <= 0:
                break

            # --- Ball ↔ player ---
            if ghost:
                hit_ball = None
            else:
                hit_ball = next((b for b in balls
                                 if b.hits_player(player.x, player.y)), None)
            if hit_ball is not None and not frozen:
                if hit_ball.variant == BV_ICE:
                    if player.active.get('X'):
                        # Shield absorbs the ice hit, same rule as any other.
                        del player.active['X']
                        spawn_explosion(particles, player.x, player.y, 24, 4)
                        audio.sfx('shield')
                        player.happy_flash = 0.3
                    else:
                        player.freeze_t = ICE_FREEZE_T
                        spawn_explosion(particles, player.x, player.y, 14, 4)
                        audio.sfx('wall')
                elif player.active.get('X'):
                    del player.active['X']
                    spawn_explosion(particles, player.x, player.y, 24, 4)
                    audio.sfx('shield')
                    for b in balls:
                        if b.hits_player(player.x, player.y):
                            b.vy = -abs(b.vy) - 5
                            b.vx = -b.vx
                    player.happy_flash = 0.3
                else:
                    player.lives -= 1
                    player.hit_flash = 0.5
                    player.shake_t = 0.4
                    player.combo = 0
                    no_damage_this_level = False
                    spawn_explosion(particles, player.x, player.y, 28, 3)
                    audio.sfx('hit')
                    _shake_set(player)
                    draw(stdscr, w, h, player, balls, harpoons, powerups,
                         particles, score_popups, level, stars, t,
                         toasts=toasts, mode=mode, session_time=session_time,
                         colorblind=colorblind, world=world,
                         hide_score=hide_score, hurry_active=hurry_active, shots_left=(shot_budget - shots_fired) if shot_budget is not None else None, shot_budget=shot_budget)
                    _shake_clear()
                    if player.lives <= 0:
                        break
                    show_message(stdscr, w, h, [
                        "✦  ✦  O U C H !  ✦  ✦",
                        "",
                        f"Lives left: {' '.join([HEART]*player.lives)}",
                        "",
                        "Press any key to retry the level...",
                    ], color=3)
                    world        = build_world(mode, level, w, h, base_seed, difficulty)
                    balls        = world['balls']
                    platforms    = world['platforms']
                    hazards      = world['hazards']
                    enemies      = world['enemies']
                    chests       = world['chests']
                    theme_def    = world['theme_def']
                    gravity_mult = theme_def['gravity_mult']
                    wrap_edges   = theme_def['wrap']
                    # Keep stuck sticky barriers across retries — they belong
                    # to the level state, not to the player's run.
                    stuck_keep   = [hp for hp in harpoons if hp.stuck]
                    harpoons     = stuck_keep
                    powerups     = []
                    particles    = []
                    score_popups = []
                    player.x     = w // 2
                    player.active.clear()
                    player.reset_face()

                    # Show READY screen again so the player chooses when to resume.
                    def _intro_frame_retry():
                        draw(stdscr, w, h, player, balls, harpoons, powerups,
                             [], [], level, stars, 0.0,
                             toasts=toasts, mode=mode, session_time=0.0,
                             colorblind=colorblind, world=world,
                             hide_score=hide_score, hurry_active=False,
                             shots_left=(shot_budget - shots_fired) if shot_budget is not None else None,
                             shot_budget=shot_budget)
                    if not level_intro(stdscr, w, h, mode, level, world,
                                       player.lives, _intro_frame_retry):
                        return False

                    last = time.monotonic()
                    t0   = last
                    continue

            _shake_set(player)
            draw(stdscr, w, h, player, balls, harpoons, powerups,
                 particles, score_popups, level, stars, t,
                 toasts=toasts, mode=mode, session_time=session_time,
                 colorblind=colorblind, world=world,
                 hide_score=hide_score, hurry_active=hurry_active, shots_left=(shot_budget - shots_fired) if shot_budget is not None else None, shot_budget=shot_budget)
            _shake_clear()

            # Auto-fade music after a while on the same level.
            if music_alive and now - music_started_at > MUSIC_FADE_AFTER:
                audio.stop_music()
                music_alive = False

            # Rewind snapshot every Nth frame.
            snapshot_frame += 1
            if snapshot_frame >= REWIND_SAMPLE_EVERY:
                snapshot_frame = 0
                snapshots.append(snapshot_state(
                    player, balls, harpoons, enemies, powerups, chests))

            # Puzzle: shots exhausted while balls remain → fail.
            if (shot_budget is not None
                    and shots_fired >= shot_budget
                    and not harpoons
                    and balls):
                break

            sleep = TICK - (time.monotonic() - now)
            if sleep > 0:
                time.sleep(sleep)

        # --- post-level / death ---
        if player.lives <= 0:
            play_music('GAME_OVER')
            elapsed = time.monotonic() - session_t0
            if mode == MODE_DAILY:
                record_daily(save, player.score, level)
            else:
                extra = {'time': round(elapsed, 1)} if mode == MODE_TIME else None
                record_high_score(save, mode, player.score, level, extra)
            if mode == MODE_SURVIVAL and level >= 50:
                unlock(save, 'survivor_50', toasts)

            lines = [
                "█▀▀ ▄▀█ █▀▄▀█ █▀▀   █▀█ █ █ █▀▀ █▀█",
                "█▄█ █▀█ █ ▀ █ ██▄   █▄█ ▀▄▀ ██▄ █▀▄",
                "",
                f"Mode  : {MODE_INFO[mode][0]}",
                f"Score : {player.score}",
                f"Level : {level}",
            ]
            if mode == MODE_TIME:
                lines.append(f"Time  : {format_time(elapsed)}")
            table = save['high_scores'].get(mode, [])
            if table:
                lines.append("")
                lines.append("Top scores:")
                for i, entry in enumerate(table[:5], 1):
                    lines.append(f"  {i}. {entry['score']:>7d}  (L{entry.get('level',0)})")
            lines += ["", "[R] retry  [Q] menu"]
            show_message(stdscr, w, h, lines, color=3)
            stdscr.nodelay(False)
            while True:
                k = stdscr.getch()
                if k == curses.KEY_RESIZE:
                    h, w = stdscr.getmaxyx()
                    continue
                if k in (ord('m'), ord('M')):
                    audio.toggle_mute()
                    continue
                if k in (ord('q'), ord('Q')):
                    stdscr.nodelay(True)
                    return True   # return to mode selector
                if k in (ord('r'), ord('R')):
                    stdscr.nodelay(True)
                    return True

        # Puzzle: shots exhausted with balls remaining → fail and exit.
        if mode == MODE_PUZZLE and balls and shot_budget is not None \
                and shots_fired >= shot_budget:
            show_message(stdscr, w, h, [
                f" ✗  PUZZLE FAILED  ✗ ",
                "",
                f"Puzzle: {world.get('puzzle_name', '?')}",
                f"Shots used: {shots_fired}/{shot_budget}",
                "",
                "Press any key to return to menu.",
            ], color=3)
            return True

        # Level cleared — achievement checks.
        if no_damage_this_level:
            unlock(save, 'flawless_level', toasts)
        if no_pu_this_level:
            unlock(save, 'no_powerups', toasts)
        if level >= 10:
            unlock(save, 'reach_10', toasts)
        if level >= 20:
            unlock(save, 'reach_20', toasts)

        # Puzzle: all puzzles cleared.
        if mode == MODE_PUZZLE and level >= len(PUZZLES):
            record_high_score(save, mode, player.score, level,
                              {'shots': shots_fired})
            show_message(stdscr, w, h, [
                f"{DIAMOND}  ALL PUZZLES SOLVED  {DIAMOND}",
                "",
                f"Score : {player.score}",
                "",
                "Press any key.",
            ], color=5)
            return True

        # Time Attack ending after fixed level count.
        if mode == MODE_TIME and level >= TIME_ATTACK_LEVELS:
            elapsed = time.monotonic() - session_t0
            record_high_score(save, mode, player.score, level,
                              {'time': round(elapsed, 1)})
            if elapsed < 120:
                unlock(save, 'time_under_120', toasts)
            show_message(stdscr, w, h, [
                f"{DIAMOND}  TIME ATTACK CLEARED  {DIAMOND}",
                "",
                f"Score : {player.score}",
                f"Time  : {format_time(elapsed)}",
                "",
                "Press any key.",
            ], color=5)
            return True

        level += 1
        play_music('LEVEL_CLEAR')
        ch = show_message(stdscr, w, h, [
            f"{DIAMOND}  Level {level - 1:02d} cleared!  {DIAMOND}",
            "",
            f"Score: {player.score}",
            "",
            f"Get ready for level {level}...",
            "Press any key (Q to menu).",
        ], color=5)
        if ch in (ord('q'), ord('Q')):
            return True


def snapshot_state(player, balls, harpoons, enemies, powerups, chests):
    """Deep snapshot used by Time Rewind. Stores only living entities."""
    return copy.deepcopy({
        'player_attrs': {slot: getattr(player, slot) for slot in player.__slots__},
        'balls':    [b for b in balls if b.alive],
        'harpoons': [h for h in harpoons if h.alive],
        'enemies':  [e for e in enemies if e.alive],
        'powerups': [pu for pu in powerups if pu.life > 0],
        'chests':   [c for c in chests if c.alive],
    })


def restore_state(snap, player):
    """Returns fresh lists rewound to the snapshot."""
    fresh = copy.deepcopy(snap)
    for slot, val in fresh['player_attrs'].items():
        setattr(player, slot, val)
    return (fresh['balls'], fresh['harpoons'], fresh['enemies'],
            fresh['powerups'], fresh['chests'])


def wrap_dx(target_x, src_x, w, wrap):
    """Signed horizontal delta from src to target, taking the short way round
    when wrap is enabled. Returns (dx, abs_dx)."""
    dx = target_x - src_x
    if not wrap:
        return dx, abs(dx)
    span = max(1.0, w - 3.0)
    if dx > span / 2:
        dx -= span
    elif dx < -span / 2:
        dx += span
    return dx, abs(dx)


def closest_target_x(balls, enemies, chests, px, w, wrap):
    """Closest interactable column for auto-aim. Considers balls, then enemies,
    then chests if nothing else. Uses wrap-aware distance."""
    best = None
    best_d = None
    pools = (
        ((b.x, b.y) for b in balls if b.alive),
        ((e.x, e.y) for e in enemies if e.alive),
        ((c.x, c.y) for c in chests if c.alive),
    )
    for pool in pools:
        for tx, ty in pool:
            _, d = wrap_dx(tx, px, w, wrap)
            d += abs(ty - 5) * 0.3
            if best is None or d < best_d:
                best = tx
                best_d = d
        if best is not None:
            return best
    return None


# Kept for compatibility — delegates to the broader targeting helper.
def closest_ball_x(balls, px):
    return closest_target_x(balls, [], [], px, 9999, False)


def damage_player(player, particles, w, source_x=None):
    """Inflicts one hit on the player. Returns True if it consumed a shield."""
    if player.active.get('X'):
        del player.active['X']
        spawn_explosion(particles, player.x, player.y, 24, 4)
        audio.sfx('shield')
        player.happy_flash = 0.3
        return True
    player.lives -= 1
    player.hit_flash = 0.5
    player.shake_t = max(player.shake_t, 0.4)
    player.combo = 0
    spawn_explosion(particles, player.x, player.y, 28, 3)
    audio.sfx('hit')
    if source_x is not None:
        if player.x < source_x:
            player.x = max(2, player.x - 3)
        else:
            player.x = min(w - 3, player.x + 3)
    return False


def _shake_set(player):
    if player.shake_t > 0:
        _SHAKE[0] = random.randint(-1, 1)
        _SHAKE[1] = random.randint(-1, 1)


def _shake_clear():
    _SHAKE[0] = 0
    _SHAKE[1] = 0


def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_WHITE,   -1)
    curses.init_pair(2, curses.COLOR_YELLOW,  -1)
    curses.init_pair(3, curses.COLOR_RED,     -1)
    curses.init_pair(4, curses.COLOR_CYAN,    -1)
    curses.init_pair(5, curses.COLOR_GREEN,   -1)
    curses.init_pair(6, curses.COLOR_MAGENTA, -1)
    curses.init_pair(7, curses.COLOR_BLUE,    -1)
    curses.init_pair(8, curses.COLOR_WHITE,   -1)

    audio.start()
    save = load_save()

    h, w = stdscr.getmaxyx()
    if w < 64 or h < 22:
        show_message(stdscr, w, h, [
            "Terminal too small.",
            f"Got {w}x{h}, need >= 64x22.",
            "Resize and run again.",
            "(press any key)",
        ])
        return

    while True:
        if not title_screen(stdscr, w, h, save):
            return
        mode = mode_selector(stdscr, w, h, save)
        if mode is None:
            continue
        # Refresh window size in case it changed on the menus.
        h, w = stdscr.getmaxyx()
        if w < 64 or h < 22:
            show_message(stdscr, w, h, [
                "Terminal too small.",
                f"Got {w}x{h}, need >= 64x22.",
                "Resize and press any key.",
            ])
            continue
        play_session(stdscr, w, h, save, mode)


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            audio.shutdown()
        except Exception:
            pass
