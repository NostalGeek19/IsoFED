import os
import math
import random
import numpy as np
import pygame


BLAST_RADIUS = 2.2              
SCORCH_RECOVER_DURATION = 20.0  
MAX_DARKEN_STRENGTH = 0.65      
EXPLOSION_VISUAL_DURATION = 0.7   
SHOCKWAVE_DURATION = 0.45         
SCREEN_FLASH_DURATION = 0.18      

CHAIN_RADIUS = 2.0
CHAIN_DELAY = 0.12  


EXPLOSION_TEXTURE_SEARCH_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'textures', 'explosion'),
    '/textures/explosion',
]
SUPPORTED_IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.bmp')

EXPLOSION_SOUND_SEARCH_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sound', 'explosion'),
    '/sound/explosion',
]
SUPPORTED_SOUND_EXTENSIONS = ('.wav', '.mp3', '.ogg')
EXPLOSION_SAMPLE_RATE = 22050


class ScorchedTile:
    __slots__ = ('tile_x', 'tile_y', 'age', 'initial_strength')

    def __init__(self, tile_x, tile_y, initial_strength):
        self.tile_x = tile_x
        self.tile_y = tile_y
        self.age = 0.0
        self.initial_strength = initial_strength

    @property
    def darken_factor(self):
        remaining = 1.0 - min(1.0, self.age / SCORCH_RECOVER_DURATION)
        return self.initial_strength * remaining


class ExplosionEffect:
    __slots__ = ('tile_x', 'tile_y', 'age', 'seed')

    def __init__(self, tile_x, tile_y, seed):
        self.tile_x = tile_x
        self.tile_y = tile_y
        self.age = 0.0
        self.seed = seed

    @property
    def finished(self):
        return self.age >= max(EXPLOSION_VISUAL_DURATION, SHOCKWAVE_DURATION)


class ExplosionSystem:

    def __init__(self, sound_system=None, blast_radius=BLAST_RADIUS,
                 chain_radius=CHAIN_RADIUS, chain_delay=CHAIN_DELAY,
                 find_bombs_fn=None, remove_bomb_fn=None, destroy_objects_fn=None,
                 sound_dirs=None, texture_dirs=None, seed=None):
        self.sound_system = sound_system
        self.blast_radius = blast_radius
        self.chain_radius = chain_radius
        self.chain_delay = chain_delay
        self.find_bombs_fn = find_bombs_fn
        self.remove_bomb_fn = remove_bomb_fn
        self.destroy_objects_fn = destroy_objects_fn
        self.sound_dirs = sound_dirs or EXPLOSION_SOUND_SEARCH_DIRS
        self.texture_dirs = texture_dirs or EXPLOSION_TEXTURE_SEARCH_DIRS

        self.effects = []          
        self.scorched = {}         
        self._pending_chain = []   
        self._chain_scheduled = set()   

        self.rng = random.Random(seed)

        self._boom_sound = None
        self._boom_sound_missing = False

        self._frame_paths = None
        self._raw_frames = None
        self._scaled_frame_cache = {}

    # ------------------------------------------------------------------
    def detonate(self, tile_x, tile_y):
        tile_x, tile_y = int(tile_x), int(tile_y)

        self.effects.append(ExplosionEffect(tile_x, tile_y, seed=self.rng.randint(0, 1_000_000)))

        radius = self.blast_radius
        r_int = int(math.ceil(radius))
        for dx in range(-r_int, r_int + 1):
            for dy in range(-r_int, r_int + 1):
                dist = math.sqrt(dx * dx + dy * dy)
                if dist > radius:
                    continue
                key = (tile_x + dx, tile_y + dy)
                falloff = (1.0 - dist / radius) ** 1.5 if radius > 0 else 1.0
                strength = MAX_DARKEN_STRENGTH * max(0.15, falloff)
                existing = self.scorched.get(key)
                if existing is None or strength > existing.darken_factor:
                    self.scorched[key] = ScorchedTile(key[0], key[1], strength)

        self._play_boom()

        if self.destroy_objects_fn is not None:
            self.destroy_objects_fn(tile_x, tile_y, self.blast_radius)

        if self.find_bombs_fn is not None:
            for (bx, by) in self.find_bombs_fn(tile_x, tile_y, self.chain_radius):
                bkey = (int(bx), int(by))
                if bkey == (tile_x, tile_y) or bkey in self._chain_scheduled:
                    continue
                self._chain_scheduled.add(bkey)
                self._pending_chain.append([self.chain_delay, bkey[0], bkey[1]])

    def update(self, dt):
        for effect in self.effects:
            effect.age += dt
        self.effects = [e for e in self.effects if not e.finished]

        expired = []
        for key, tile in self.scorched.items():
            tile.age += dt
            if tile.age >= SCORCH_RECOVER_DURATION:
                expired.append(key)
        for key in expired:
            del self.scorched[key]

        if self._pending_chain:
            still_pending = []
            for entry in self._pending_chain:
                entry[0] -= dt
                if entry[0] <= 0:
                    bx, by = entry[1], entry[2]
                    self._chain_scheduled.discard((bx, by))
                    if self.remove_bomb_fn is not None:
                        self.remove_bomb_fn(bx, by)
                    self.detonate(bx, by)
                else:
                    still_pending.append(entry)
            self._pending_chain = still_pending

        if not self._pending_chain and not self.effects and not self.scorched:
            self._chain_scheduled.clear()

    # ------------------------------------------------------------------
    def get_darken_factor(self, tile_x, tile_y):
        tile = self.scorched.get((int(tile_x), int(tile_y)))
        if tile is None:
            return 0.0
        return tile.darken_factor

    def apply_darken(self, tile_x, tile_y, color):
        darken = self.get_darken_factor(tile_x, tile_y)
        if darken <= 0.0:
            return color
        mult = 1.0 - darken
        return tuple(max(0, int(c * mult)) for c in color)

    def is_active(self, tile_x, tile_y):
        return (int(tile_x), int(tile_y)) in self.scorched

    def count_scorched(self):
        return len(self.scorched)

    def remove_all(self):
        self.effects.clear()
        self.scorched.clear()

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
                    path = os.path.join(directory, f'explosion_{i}{ext}')
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
                    path = os.path.join(directory, 'explosion' + ext)
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
                print(f"Explosion system: failed to load explosion texture from {path}: {e}")

        if not frames:
            searched = " or ".join(self.texture_dirs)
            print(f"Explosion system: no explosion texture found (looked in {searched}) — "
                  f"using a procedural fireball instead")
        else:
            print(f"Explosion system: loaded {len(frames)} explosion texture frame(s)")

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
        self._boom_sound = None
        self._boom_sound_missing = False

    # ------------------------------------------------------------------
    def render(self, screen, world_to_screen_fn, pixels_per_tile, chunk_bounds=None):
        if not self.effects:
            return

        screen_w, screen_h = screen.get_size()
        max_flash_alpha = 0.0

        for effect in self.effects:
            if chunk_bounds is not None:
                min_x, min_y, max_x, max_y = chunk_bounds
                if not (min_x <= effect.tile_x < max_x and min_y <= effect.tile_y < max_y):
                    continue

            screen_x, screen_y = world_to_screen_fn(effect.tile_x, effect.tile_y)
            if not (-200 <= screen_x <= screen_w + 200 and -200 <= screen_y <= screen_h + 200):
                continue

            flash_alpha = self._draw_effect(screen, effect, screen_x, screen_y, pixels_per_tile)
            max_flash_alpha = max(max_flash_alpha, flash_alpha)

        if max_flash_alpha > 0.01:
            flash = pygame.Surface((screen_w, screen_h), pygame.SRCALPHA)
            flash.fill((255, 235, 200, int(90 * max_flash_alpha)))
            screen.blit(flash, (0, 0))

    def _draw_effect(self, screen, effect, screen_x, screen_y, pixels_per_tile):
        half_tile = pixels_per_tile / 2
        eff_rng = random.Random(effect.seed)

        flash_t = effect.age / SCREEN_FLASH_DURATION
        flash_alpha = max(0.0, 1.0 - flash_t) if flash_t < 1.0 else 0.0


        if effect.age < SHOCKWAVE_DURATION:
            t = effect.age / SHOCKWAVE_DURATION
            ring_radius = half_tile * (0.4 + 3.2 * t)
            ring_alpha = int(160 * (1.0 - t))
            if ring_alpha > 0:
                ring_surf = pygame.Surface((int(ring_radius * 2 + 4), int(ring_radius * 2 + 4)), pygame.SRCALPHA)
                center = ring_surf.get_width() / 2
                width = max(1, int(6 * (1.0 - t) + 1))
                pygame.draw.circle(ring_surf, (255, 200, 120, ring_alpha),
                                    (int(center), int(center)), int(ring_radius), width)
                rect = ring_surf.get_rect(center=(int(screen_x), int(screen_y)))
                screen.blit(ring_surf, rect, special_flags=pygame.BLEND_RGBA_ADD)


        if effect.age < EXPLOSION_VISUAL_DURATION:
            t = effect.age / EXPLOSION_VISUAL_DURATION
            fade = max(0.0, 1.0 - t) ** 0.8
            base_radius = half_tile * (0.9 + 0.5 * min(1.0, t * 4))

            width_px = max(4, int(base_radius * 3))
            frames = self._get_scaled_frames(width_px)

            if frames:
                frame_index = min(len(frames) - 1, int(t * len(frames)))
                sprite = frames[frame_index]
                if fade < 0.999:
                    sprite = sprite.copy()
                    sprite.set_alpha(int(255 * fade))
                rect = sprite.get_rect(center=(int(screen_x), int(screen_y)))
                screen.blit(sprite, rect)
            else:
                fireball_surf = pygame.Surface((width_px, width_px), pygame.SRCALPHA)
                fc = fireball_surf.get_width() / 2

                layers = [
                    ((90, 40, 20), 1.05, int(120 * fade)),
                    ((230, 90, 30), 0.8, int(170 * fade)),
                    ((255, 175, 60), 0.55, int(200 * fade)),
                    ((255, 235, 160), 0.3, int(220 * fade)),
                ]
                for color, scale, alpha in layers:
                    if alpha <= 0:
                        continue
                    jitter = eff_rng.uniform(-0.08, 0.08)
                    r = base_radius * (scale + jitter)
                    pygame.draw.circle(fireball_surf, (*color, alpha), (int(fc), int(fc)), max(1, int(r)))

                rect = fireball_surf.get_rect(center=(int(screen_x), int(screen_y)))
                screen.blit(fireball_surf, rect, special_flags=pygame.BLEND_RGBA_ADD)

        return flash_alpha

    # ------------------------------------------------------------------
    def _load_boom_sound(self):
        if self._boom_sound is not None:
            return self._boom_sound
        if self._boom_sound_missing:
            return None

        for directory in self.sound_dirs:
            for ext in SUPPORTED_SOUND_EXTENSIONS:
                path = os.path.join(directory, 'boom' + ext)
                if os.path.isfile(path):
                    try:
                        self._boom_sound = pygame.mixer.Sound(path)
                        print(f"Explosion system: loaded boom sound from {path}")
                        return self._boom_sound
                    except Exception as e:
                        print(f"Explosion system: failed to load boom sound from {path}: {e}")

        searched = " or ".join(self.sound_dirs)
        print(f"Explosion system: no boom sound found (looked in {searched}) — "
              f"using a procedural explosion instead")
        self._boom_sound_missing = True
        return None

    def _make_procedural_boom_signal(self, seed):
        rng = np.random.RandomState(seed)
        duration = rng.uniform(1.4, 1.9)
        n = int(EXPLOSION_SAMPLE_RATE * duration)
        t = np.arange(n) / EXPLOSION_SAMPLE_RATE


        noise = rng.uniform(-1.0, 1.0, n).astype(np.float32)
        kernel = np.ones(4, dtype=np.float32) / 4
        crack = np.convolve(noise, kernel, mode='same').astype(np.float32)
        crack_env = np.exp(-t * 9.0)


        rumble_noise = rng.uniform(-1.0, 1.0, n).astype(np.float32)
        kernel2 = np.ones(60, dtype=np.float32) / 60
        rumble = np.convolve(rumble_noise, kernel2, mode='same').astype(np.float32)
        rumble_env = np.exp(-t * 1.4) * (1.0 - np.exp(-t * 25.0))


        freq_sweep = 130.0 * np.exp(-t * 10.0) + 35.0
        phase = np.cumsum(2 * np.pi * freq_sweep / EXPLOSION_SAMPLE_RATE)
        thump = np.sin(phase) * np.exp(-t * 7.0)

        signal = crack * crack_env * 0.9 + rumble * rumble_env * 0.8 + thump * 0.7
        signal = signal / (np.abs(signal).max() + 1e-6) * 0.9
        return signal.astype(np.float32)

    def _play_boom(self):
        if pygame.mixer.get_init() is None:
            return

        sound = self._load_boom_sound()
        if sound is None:
            try:
                seed = self.rng.randint(0, 1_000_000)
                signal = self._make_procedural_boom_signal(seed)
                pcm = np.clip(signal, -1.0, 1.0)
                pcm = (pcm * 32767).astype(np.int16)
                stereo = np.column_stack([pcm, pcm])
                sound = pygame.sndarray.make_sound(stereo)
            except Exception as e:
                print(f"Explosion system: failed to generate boom sound: {e}")
                return

        volume_mult = self.rng.uniform(0.85, 1.0)

        if self.sound_system is not None and hasattr(self.sound_system, 'play_one_shot'):
            self.sound_system.play_one_shot(sound, volume=volume_mult)
            return

        volume = getattr(self.sound_system, 'master_volume', 1.0) if self.sound_system is not None else 1.0
        volume *= volume_mult
        channel = pygame.mixer.find_channel(True)
        if channel is None:
            return
        channel.set_volume(min(1.0, volume))
        channel.play(sound)

    # ------------------------------------------------------------------
    def get_status_text(self):
        if not self.effects and not self.scorched:
            return "no explosions"
        return f"{len(self.scorched)} scorched tile(s)"
