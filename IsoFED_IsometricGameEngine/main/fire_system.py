import os
import math
import random
import numpy as np
import pygame


IGNITE_CHANCE_FROM_LIGHTNING = 0.05

SPREAD_DELAY_RANGE = (4.0, 10.0)     
BURN_DURATION_RANGE = (15.0, 25.0)   
SPREAD_RADIUS = 1                    

FIRE_LIGHT_COLOR = (255, 130, 40)
FIRE_LIGHT_RADIUS = 3.2
FIRE_LIGHT_INTENSITY = 1.0
FIRE_FLICKER_AMPLITUDE = 0.3
FIRE_FLICKER_SPEED = 6.0


GRASS_DARK_DURATION = 120.0           
GRASS_DARK_FADE_TIME = 12.0           
GRASS_DARKEN_STRENGTH = 0.6            
TREE_REGROW_DURATION_RANGE = (240.0, 300.0)   


FIRE_TEXTURE_SEARCH_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'textures', 'fire'),
    '/textures/fire',
]
FIRE_ANIMATION_FPS = 10

FIRE_SOUND_SEARCH_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sound', 'fire'),
    '/sound/fire',
]

SUPPORTED_IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.bmp')
SUPPORTED_SOUND_EXTENSIONS = ('.wav', '.mp3', '.ogg')
IGNITE_SOUND_SAMPLE_RATE = 22050


class BurningTree:
    __slots__ = ('tile_x', 'tile_y', 'age', 'burn_duration', 'spread_delay',
                 'spread_done', 'phase')

    def __init__(self, tile_x, tile_y, burn_duration, spread_delay, phase):
        self.tile_x = tile_x
        self.tile_y = tile_y
        self.age = 0.0
        self.burn_duration = burn_duration
        self.spread_delay = spread_delay
        self.spread_done = False
        self.phase = phase  

    @property
    def life_fraction(self):
        if self.burn_duration <= 0:
            return 1.0
        return min(1.0, self.age / self.burn_duration)


class BurntGround:
    __slots__ = ('tile_x', 'tile_y', 'age', 'dark_duration', 'regrow_duration')

    def __init__(self, tile_x, tile_y, dark_duration, regrow_duration):
        self.tile_x = tile_x
        self.tile_y = tile_y
        self.age = 0.0
        self.dark_duration = dark_duration
        self.regrow_duration = regrow_duration


class FireSystem:

    def __init__(self, seed=None,
                 ignite_chance=IGNITE_CHANCE_FROM_LIGHTNING,
                 spread_delay_range=SPREAD_DELAY_RANGE,
                 burn_duration_range=BURN_DURATION_RANGE,
                 spread_radius=SPREAD_RADIUS,
                 grass_dark_duration=GRASS_DARK_DURATION,
                 tree_regrow_range=TREE_REGROW_DURATION_RANGE,
                 sound_system=None,
                 texture_dirs=None,
                 sound_dirs=None):
        self.rng = random.Random(seed)
        self.ignite_chance = ignite_chance
        self.spread_delay_range = spread_delay_range
        self.burn_duration_range = burn_duration_range
        self.spread_radius = spread_radius
        self.grass_dark_duration = grass_dark_duration
        self.tree_regrow_range = tree_regrow_range
        self.sound_system = sound_system   

        self.fires = {}        
        self.ash = {}           # (tile_x, tile_y) -> BurntGround
        self.time = 0.0         

        self.texture_dirs = texture_dirs or FIRE_TEXTURE_SEARCH_DIRS
        self.sound_dirs = sound_dirs or FIRE_SOUND_SEARCH_DIRS

        self._frame_paths = None          
        self._raw_frames = None           
        self._scaled_frame_cache = {}     

        self._ignite_sound = None
        self._ignite_sound_missing = False
        self._ignite_channel = None

    # ------------------------------------------------------------------
    def set_ignite_chance(self, chance):
        if chance > 1.0:
            chance /= 100.0
        self.ignite_chance = max(0.0, min(1.0, chance))

    def get_ignite_chance(self):
        return self.ignite_chance

    # ------------------------------------------------------------------
    def is_burning_at(self, tile_x, tile_y):
        return (int(tile_x), int(tile_y)) in self.fires

    def get_burning_tiles(self):
        return list(self.fires.keys())

    def is_ash_at(self, tile_x, tile_y):
        return (int(tile_x), int(tile_y)) in self.ash

    def is_tree_suppressed(self, tile_x, tile_y):
        return (int(tile_x), int(tile_y)) in self.ash

    def get_grass_darken(self, tile_x, tile_y):
        entry = self.ash.get((int(tile_x), int(tile_y)))
        if entry is None:
            return 0.0
        if entry.age >= entry.dark_duration:
            return 0.0
        remaining = entry.dark_duration - entry.age
        if GRASS_DARK_FADE_TIME <= 0:
            return 1.0
        return max(0.0, min(1.0, remaining / GRASS_DARK_FADE_TIME))

    def count_burning(self):
        return len(self.fires)

    def remove_all(self):
        self.fires.clear()
        self.ash.clear()

    # ------------------------------------------------------------------
    def try_ignite_from_lightning(self, tile_x, tile_y, tree_at_fn):
        key = (int(tile_x), int(tile_y))
        if key in self.fires or key in self.ash:
            return False
        if tree_at_fn is None or not tree_at_fn(key[0], key[1]):
            return False
        if self.rng.random() >= self.ignite_chance:
            return False

        self._ignite(key)
        return True

    def ignite(self, tile_x, tile_y):
        key = (int(tile_x), int(tile_y))
        if key in self.fires or key in self.ash:
            return False
        self._ignite(key)
        return True

    def _ignite(self, key):
        burn_duration = self.rng.uniform(*self.burn_duration_range)
        spread_delay = self.rng.uniform(*self.spread_delay_range)
        phase = self.rng.uniform(0, 2 * math.pi)
        self.fires[key] = BurningTree(key[0], key[1], burn_duration, spread_delay, phase)
        self._play_ignite_sound(key[0], key[1])

    # ------------------------------------------------------------------
    def _neighbors(self, tile_x, tile_y):
        r = self.spread_radius
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if dx == 0 and dy == 0:
                    continue
                yield (tile_x + dx, tile_y + dy)

    def update(self, dt, tree_at_fn):
        self.time += dt
        finished = []
        to_ignite = []

        for key, tree in self.fires.items():
            tree.age += dt

            if not tree.spread_done and tree.age >= tree.spread_delay:
                tree.spread_done = True
                if tree_at_fn is not None:
                    for neighbor in self._neighbors(tree.tile_x, tree.tile_y):
                        if neighbor in self.fires or neighbor in self.ash:
                            continue
                        if tree_at_fn(neighbor[0], neighbor[1]):
                            to_ignite.append(neighbor)

            if tree.age >= tree.burn_duration:
                finished.append(key)

        for key in finished:
            del self.fires[key]
            dark_duration = self.grass_dark_duration
            regrow_duration = self.rng.uniform(*self.tree_regrow_range)

            regrow_duration = max(regrow_duration, dark_duration)
            self.ash[key] = BurntGround(key[0], key[1], dark_duration, regrow_duration)

        for key in to_ignite:
            if key not in self.fires and key not in self.ash:
                self._ignite(key)

        regrown = []
        for key, ground in self.ash.items():
            ground.age += dt
            if ground.age >= ground.regrow_duration:
                regrown.append(key)
        for key in regrown:
            del self.ash[key]

    # ------------------------------------------------------------------
    def get_light_boost(self, world_x, world_y):
        if not self.fires:
            return (0.0, 0.0, 0.0)

        total_r = total_g = total_b = 0.0
        for tree in self.fires.values():
            dx = world_x - tree.tile_x
            dy = world_y - tree.tile_y
            dist = math.sqrt(dx * dx + dy * dy)
            if dist >= FIRE_LIGHT_RADIUS:
                continue

            flicker = 1.0 + FIRE_FLICKER_AMPLITUDE * math.sin(self.time * FIRE_FLICKER_SPEED + tree.phase)
            fade = min(1.0, tree.age / 1.0) * min(1.0, (tree.burn_duration - tree.age) / 2.0 + 0.3)
            intensity = FIRE_LIGHT_INTENSITY * flicker * max(0.0, fade)

            falloff = (1.0 - dist / FIRE_LIGHT_RADIUS) ** 2
            strength = falloff * intensity * 90.0
            total_r += FIRE_LIGHT_COLOR[0] / 255.0 * strength
            total_g += FIRE_LIGHT_COLOR[1] / 255.0 * strength
            total_b += FIRE_LIGHT_COLOR[2] / 255.0 * strength

        return (total_r, total_g, total_b)

    # ------------------------------------------------------------------
    def _find_frame_paths(self):
        if self._frame_paths is not None:
            return self._frame_paths

        frames = []
        i = 1
        while True:
            found = None
            for directory in self.texture_dirs:
                for ext in SUPPORTED_IMAGE_EXTENSIONS:
                    path = os.path.join(directory, f'fire_{i}{ext}')
                    if os.path.isfile(path):
                        found = path
                        break
                if found:
                    break
            if not found:
                break
            frames.append(found)
            i += 1

        if not frames:
            for directory in self.texture_dirs:
                for ext in SUPPORTED_IMAGE_EXTENSIONS:
                    path = os.path.join(directory, 'fire' + ext)
                    if os.path.isfile(path):
                        frames.append(path)
                        break
                if frames:
                    break

        self._frame_paths = frames
        return frames

    def _load_raw_frames(self):
        if self._raw_frames is not None:
            return self._raw_frames

        paths = self._find_frame_paths()
        frames = []
        for path in paths:
            try:
                frames.append(pygame.image.load(path).convert_alpha())
            except Exception as e:
                print(f"Fire system: failed to load fire texture from {path}: {e}")

        if not frames:
            searched = " or ".join(self.texture_dirs)
            print(f"Fire system: no fire texture found (looked in {searched}) — using procedural flame instead")
        else:
            print(f"Fire system: loaded {len(frames)} fire texture frame(s)")

        self._raw_frames = frames
        return frames

    def _get_scaled_frames(self, width_px):
        width_px = max(2, int(round(width_px)))
        cached = self._scaled_frame_cache.get(width_px)
        if cached is not None:
            return cached

        raw_frames = self._load_raw_frames()
        scaled = []
        for raw in raw_frames:
            raw_w, raw_h = raw.get_size()
            scale = width_px / raw_w
            height_px = max(2, int(round(raw_h * scale)))
            scaled.append(pygame.transform.smoothscale(raw, (width_px, height_px)))

        if len(self._scaled_frame_cache) > 40:
            self._scaled_frame_cache.clear()
        self._scaled_frame_cache[width_px] = scaled
        return scaled

    def reload(self):
        self._frame_paths = None
        self._raw_frames = None
        self._scaled_frame_cache.clear()
        self._ignite_sound = None
        self._ignite_sound_missing = False

    # ------------------------------------------------------------------
    def _load_ignite_sound(self):
        if self._ignite_sound is not None:
            return self._ignite_sound
        if self._ignite_sound_missing:
            return None

        for directory in self.sound_dirs:
            for ext in SUPPORTED_SOUND_EXTENSIONS:
                path = os.path.join(directory, 'ignite' + ext)
                if os.path.isfile(path):
                    try:
                        self._ignite_sound = pygame.mixer.Sound(path)
                        print(f"Fire system: loaded ignite sound from {path}")
                        return self._ignite_sound
                    except Exception as e:
                        print(f"Fire system: failed to load ignite sound from {path}: {e}")

        searched = " or ".join(self.sound_dirs)
        print(f"Fire system: no ignite sound found (looked in {searched}) — "
              f"using a procedural crackle instead")
        self._ignite_sound_missing = True
        return None

    def _make_procedural_ignite_signal(self, seed):
        rng = np.random.RandomState(seed)
        duration = rng.uniform(0.7, 1.1)
        n = int(IGNITE_SOUND_SAMPLE_RATE * duration)
        t = np.arange(n) / IGNITE_SOUND_SAMPLE_RATE

        noise = rng.uniform(-1.0, 1.0, n).astype(np.float32)
        kernel = np.ones(6, dtype=np.float32) / 6
        hiss = np.convolve(noise, kernel, mode='same').astype(np.float32)
        whoosh_envelope = np.exp(-((t - 0.12) ** 2) / (2 * 0.06 ** 2))
        whoosh = hiss * whoosh_envelope

        crackle = np.zeros(n, dtype=np.float32)
        for _ in range(rng.randint(4, 9)):
            pos = rng.randint(0, max(1, n - 200))
            length = rng.randint(40, 180)
            length = min(length, n - pos)
            if length <= 0:
                continue
            pop_env = np.exp(-np.arange(length) / (length * 0.3))
            crackle[pos:pos + length] += rng.uniform(-1, 1, length).astype(np.float32) * pop_env * 0.6

        signal = whoosh * 0.8 + crackle
        signal = signal / (np.abs(signal).max() + 1e-6) * 0.8
        return signal.astype(np.float32)

    def _play_ignite_sound(self, tile_x, tile_y):
        if self.sound_system is not None and hasattr(self.sound_system, 'is_position_audible'):
            if not self.sound_system.is_position_audible(tile_x, tile_y):
                return
        if pygame.mixer.get_init() is None:
            return

        sound = self._load_ignite_sound()
        if sound is None:
            try:
                seed = self.rng.randint(0, 1_000_000)
                signal = self._make_procedural_ignite_signal(seed)
                pcm = np.clip(signal, -1.0, 1.0)
                pcm = (pcm * 32767).astype(np.int16)
                stereo = np.column_stack([pcm, pcm])
                sound = pygame.sndarray.make_sound(stereo)
            except Exception as e:
                print(f"Fire system: failed to generate ignite sound: {e}")
                return

        volume_mult = self.rng.uniform(0.75, 1.0)

        if self.sound_system is not None and hasattr(self.sound_system, 'play_one_shot'):
            self.sound_system.play_one_shot(sound, volume=volume_mult)
            return

        volume = getattr(self.sound_system, 'master_volume', 1.0) if self.sound_system is not None else 1.0
        volume *= volume_mult

        channel = self._ignite_channel or pygame.mixer.find_channel(True)
        if channel is None:
            return
        channel.set_volume(min(1.0, volume))
        channel.play(sound)

    # ------------------------------------------------------------------
    def render(self, screen, world_to_screen_fn, pixels_per_tile, chunk_bounds=None):
        if not self.fires:
            return

        screen_w, screen_h = screen.get_size()
        half_tile = pixels_per_tile / 2
        quarter_tile = half_tile / 2

        for tree in self.fires.values():
            if chunk_bounds is not None:
                min_x, min_y, max_x, max_y = chunk_bounds
                if not (min_x <= tree.tile_x < max_x and min_y <= tree.tile_y < max_y):
                    continue

            screen_x, screen_y = world_to_screen_fn(tree.tile_x, tree.tile_y)
            if not (-half_tile * 2 <= screen_x <= screen_w + half_tile * 2 and
                    -half_tile * 6 <= screen_y <= screen_h + half_tile * 4):
                continue

            self._draw_flame(screen, tree, screen_x, screen_y, half_tile, quarter_tile)

    def _draw_flame(self, screen, tree, screen_x, screen_y, half_tile, quarter_tile):
        fade_in = min(1.0, tree.age / 1.0)
        fade_out = min(1.0, (tree.burn_duration - tree.age) / 2.5)
        alpha_mult = max(0.0, fade_in * fade_out)
        if alpha_mult <= 0.0:
            return

        base_y = screen_y + quarter_tile

        width_px = max(4, int(round(half_tile * 1.7)))
        frames = self._get_scaled_frames(width_px)
        if frames:
            frame_index = int(self.time * FIRE_ANIMATION_FPS + tree.phase * 3.0) % len(frames)
            sprite = frames[frame_index]
            if alpha_mult < 0.999:
                sprite = sprite.copy()
                sprite.set_alpha(int(255 * alpha_mult))
            rect = sprite.get_rect(midbottom=(screen_x, base_y))
            screen.blit(sprite, rect)
            return

        self._draw_procedural_flame(screen, tree, screen_x, base_y, half_tile, quarter_tile, alpha_mult)

    def _draw_procedural_flame(self, screen, tree, screen_x, base_y, half_tile, quarter_tile, alpha_mult):
        flicker = math.sin(self.time * FIRE_FLICKER_SPEED + tree.phase)
        flicker2 = math.sin(self.time * FIRE_FLICKER_SPEED * 1.7 + tree.phase * 1.3)

        flame_height = quarter_tile * (2.6 + 0.3 * flicker)
        flame_width = half_tile * (0.55 + 0.08 * flicker2)

        flame_surf = pygame.Surface((int(flame_width * 3), int(flame_height * 1.6) + 4), pygame.SRCALPHA)
        cx = flame_surf.get_width() / 2
        base_local_y = flame_surf.get_height() - 2


        glow_radius = int(flame_width * 1.6)
        glow_alpha = int(70 * alpha_mult)
        if glow_radius > 0 and glow_alpha > 0:
            pygame.draw.circle(flame_surf, (255, 140, 40, glow_alpha),
                                (int(cx), int(base_local_y - flame_height * 0.3)), glow_radius)


        layers = [
            ((200, 60, 20), 1.0, int(200 * alpha_mult)),
            ((255, 130, 30), 0.72, int(220 * alpha_mult)),
            ((255, 220, 120), 0.42, int(230 * alpha_mult)),
        ]
        for color, scale, alpha in layers:
            if alpha <= 0:
                continue
            w = flame_width * scale
            h = flame_height * scale
            points = [
                (cx, base_local_y - h),
                (cx + w * 0.55, base_local_y - h * 0.45),
                (cx + w * 0.32, base_local_y),
                (cx - w * 0.32, base_local_y),
                (cx - w * 0.55, base_local_y - h * 0.45),
            ]
            pygame.draw.polygon(flame_surf, (*color, alpha), points)

        rect = flame_surf.get_rect(midbottom=(screen_x, base_y))
        screen.blit(flame_surf, rect, special_flags=pygame.BLEND_RGBA_ADD)

    # ------------------------------------------------------------------
    def get_status_text(self):
        if not self.fires and not self.ash:
            return "no fires"
        parts = []
        if self.fires:
            parts.append(f"{len(self.fires)} burning")
        if self.ash:
            parts.append(f"{len(self.ash)} recovering")
        return ", ".join(parts)
