import math
import random
import numpy as np
import pygame

from sound_system import WEATHER_RAIN, _lowpass, _to_stereo_int16


class LightningStrike:
    __slots__ = ('tile_x', 'tile_y', 'age', 'bolt_life', 'flash_life', 'seed')

    def __init__(self, tile_x, tile_y, seed):
        self.tile_x = tile_x
        self.tile_y = tile_y
        self.age = 0.0
        self.bolt_life = 0.35     
        self.flash_life = 0.6     
        self.seed = seed

    @property
    def finished(self):
        return self.age >= max(self.bolt_life, self.flash_life)

    def bolt_alpha(self):
        if self.age >= self.bolt_life:
            return 0.0
        t = self.age / self.bolt_life
        primary = max(0.0, 1.0 - t * 3.2)
        secondary = 0.0
        if 0.45 <= t <= 0.62:
            secondary = (1.0 - abs(t - 0.535) / 0.085) * 0.7
        return min(1.0, primary + secondary)

    def light_strength(self):
        if self.age >= self.flash_life:
            return 0.0
        t = self.age / self.flash_life
        primary = max(0.0, 1.0 - t) ** 1.5
        secondary = 0.0
        if 0.4 <= t <= 0.6:
            secondary = (1.0 - abs(t - 0.5) / 0.1) * 0.5
        return min(1.0, primary + secondary)


class ThunderstormSystem:
    DEFAULT_STORM_CHANCE = 0.25

    LIGHTNING_COLOR = (225, 230, 255)
    LIGHTNING_RADIUS_TILES = 7.0   
    LIGHTNING_INTENSITY = 9.0      

    MIN_STRIKE_INTERVAL = 3.5
    MAX_STRIKE_INTERVAL = 11.0

    THUNDER_SAMPLE_RATE = 22050

    def __init__(self, lighting_system=None, sound_system=None, seed=None,
                 storm_chance=DEFAULT_STORM_CHANCE, debug=False, on_strike=None):
        self.lighting_system = lighting_system
        self.sound_system = sound_system
        self.rng = random.Random(seed)
        self.on_strike = on_strike

        self.storm_chance = max(0.0, min(1.0, storm_chance))
        self.debug = debug

        self.storm_active = False
        self._was_raining = False

        self.strikes = []
        self._time_to_next_strike = self._roll_next_interval()

        self._thunder_channel = None
        if pygame.mixer.get_init() is not None:
            try:
                self._thunder_channel = pygame.mixer.find_channel(True)
            except Exception:
                self._thunder_channel = None

    # ------------------------------------------------------------------
    def set_storm_chance(self, chance):
        if chance > 1.0:
            chance /= 100.0
        self.storm_chance = max(0.0, min(1.0, chance))

    def get_storm_chance(self):
        return self.storm_chance

    def _roll_next_interval(self):
        return self.rng.uniform(self.MIN_STRIKE_INTERVAL, self.MAX_STRIKE_INTERVAL)

    def update(self, dt, weather_kind, weather_intensity, camera_tile_bounds=None,
               biome_at_fn=None, current_biome=None):
        raining_now = (weather_kind == WEATHER_RAIN and weather_intensity > 0.05)

        if raining_now and not self._was_raining:
            self.storm_active = self.rng.random() < self.storm_chance
            self._time_to_next_strike = self._roll_next_interval()
            if self.debug:
                print(f"[Thunderstorm] chance thunder ={self.storm_chance:.0%} -> "
                      f"{'thunder' if self.storm_active else 'rain'}")

        if not raining_now:
            self.storm_active = False

        self._was_raining = raining_now

        if self.storm_active and camera_tile_bounds is not None:
            self._time_to_next_strike -= dt
            if self._time_to_next_strike <= 0:
                self._try_strike(camera_tile_bounds, biome_at_fn, current_biome)

                self._time_to_next_strike = self._roll_next_interval() * (1.35 - 0.5 * weather_intensity)

        for strike in self.strikes:
            strike.age += dt
        self.strikes = [s for s in self.strikes if not s.finished]

    # ------------------------------------------------------------------
    def _try_strike(self, camera_tile_bounds, biome_at_fn, current_biome):
        min_x, min_y, max_x, max_y = camera_tile_bounds
        if max_x <= min_x or max_y <= min_y:
            return

        tile_x = tile_y = None
        attempts = 12 if biome_at_fn is not None else 1
        for _ in range(attempts):
            cx = self.rng.randint(int(min_x), int(max_x))
            cy = self.rng.randint(int(min_y), int(max_y))
            if biome_at_fn is None or current_biome is None or biome_at_fn(cx, cy) == current_biome:
                tile_x, tile_y = cx, cy
                break

        if tile_x is None:
            return 

        strike = LightningStrike(tile_x, tile_y, seed=self.rng.randint(0, 1_000_000))
        self.strikes.append(strike)
        self._play_thunder()
        if self.debug:
            print(f"[Thunderstorm]thunder ({tile_x}, {tile_y})")
        if self.on_strike is not None:
            self.on_strike(tile_x, tile_y)

    def force_start_storm(self):
        self.storm_active = True
        self._was_raining = True
        self._time_to_next_strike = 0.0

    def force_strike(self, camera_tile_bounds, biome_at_fn=None, current_biome=None):
        self._try_strike(camera_tile_bounds, biome_at_fn, current_biome)

    # ------------------------------------------------------------------
    def get_light_boost(self, world_x, world_y):
        if not self.strikes:
            return (0.0, 0.0, 0.0)
        total_r = total_g = total_b = 0.0
        for strike in self.strikes:
            strength = strike.light_strength()
            if strength <= 0.0:
                continue
            dx = world_x - strike.tile_x
            dy = world_y - strike.tile_y
            dist = math.sqrt(dx * dx + dy * dy)
            if dist >= self.LIGHTNING_RADIUS_TILES:
                continue
            falloff = (1.0 - dist / self.LIGHTNING_RADIUS_TILES) ** 2
            s = falloff * strength * self.LIGHTNING_INTENSITY * 90.0
            total_r += self.LIGHTNING_COLOR[0] / 255.0 * s
            total_g += self.LIGHTNING_COLOR[1] / 255.0 * s
            total_b += self.LIGHTNING_COLOR[2] / 255.0 * s
        return (total_r, total_g, total_b)

    # ------------------------------------------------------------------
    def render(self, screen, world_to_screen_fn, pixels_per_tile):
        if not self.strikes:
            return

        screen_w, screen_h = screen.get_size()
        max_alpha = 0.0

        for strike in self.strikes:
            alpha = strike.bolt_alpha()
            if alpha <= 0.0:
                continue
            max_alpha = max(max_alpha, alpha)

            target_x, target_y = world_to_screen_fn(strike.tile_x, strike.tile_y)
            if not (-100 <= target_x <= screen_w + 100 and -100 <= target_y <= screen_h + 100):
                continue

            self._draw_bolt(screen, strike, target_x, target_y, alpha, pixels_per_tile)

        if max_alpha > 0.01:
            flash = pygame.Surface((screen_w, screen_h), pygame.SRCALPHA)
            flash.fill((235, 240, 255, int(70 * max_alpha)))
            screen.blit(flash, (0, 0))

    def _draw_bolt(self, screen, strike, target_x, target_y, alpha, pixels_per_tile):
        bolt_rng = random.Random(strike.seed)
        start_y = -40
        start_x = target_x + bolt_rng.uniform(-30, 30)

        points = [(start_x, start_y)]
        segments = 9
        for i in range(1, segments + 1):
            t = i / segments
            x = start_x + (target_x - start_x) * t + bolt_rng.uniform(-18, 18) * (1 - t * 0.5)
            y = start_y + (target_y - start_y) * t
            points.append((x, y))
        points[-1] = (target_x, target_y)

        core_color = (255, 255, 255)
        glow_color = self.LIGHTNING_COLOR
        width_core = max(1, int(2 * alpha + 1))
        width_glow = width_core + 4

        bolt_surf = pygame.Surface(screen.get_size(), pygame.SRCALPHA)
        pygame.draw.lines(bolt_surf, (*glow_color, int(120 * alpha)), False, points, width_glow)
        pygame.draw.lines(bolt_surf, (*core_color, int(255 * alpha)), False, points, width_core)

        for _ in range(bolt_rng.randint(1, 3)):
            branch_start_idx = bolt_rng.randint(1, len(points) - 2)
            bx, by = points[branch_start_idx]
            blen = bolt_rng.uniform(15, 40)
            bang = bolt_rng.uniform(-1.2, 1.2) + math.pi / 2
            bx2 = bx + math.cos(bang) * blen
            by2 = by + math.sin(bang) * blen
            pygame.draw.line(bolt_surf, (*core_color, int(160 * alpha)), (bx, by), (bx2, by2), 1)

        r = max(3, int(pixels_per_tile * 0.5))
        pygame.draw.circle(bolt_surf, (*glow_color, int(90 * alpha)), (int(target_x), int(target_y)), r)

        screen.blit(bolt_surf, (0, 0))

    # ------------------------------------------------------------------
    def _make_thunder_signal(self, seed):
        rng = np.random.RandomState(seed)
        duration = rng.uniform(1.6, 2.6)
        n = int(self.THUNDER_SAMPLE_RATE * duration)

        noise = rng.uniform(-1.0, 1.0, n).astype(np.float32)
        crack = _lowpass(noise, 3)     
        rumble = _lowpass(noise, 45)  

        t = np.arange(n) / self.THUNDER_SAMPLE_RATE
        crack_env = np.exp(-t * 18.0)
        rumble_env = np.exp(-t * 1.1) * (1.0 - np.exp(-t * 10.0))

        signal = crack * crack_env * 0.9 + rumble * rumble_env * 0.6
        signal = signal / (np.abs(signal).max() + 1e-6) * 0.85
        return signal.astype(np.float32)

    def _play_thunder(self):
        if self.sound_system is not None and not getattr(self.sound_system, 'enabled', True):
            return
        if pygame.mixer.get_init() is None:
            return

        seed = self.rng.randint(0, 1_000_000)
        try:
            signal = self._make_thunder_signal(seed)
            sound = pygame.sndarray.make_sound(_to_stereo_int16(signal))
        except Exception as e:
            print(f"no sound: {e}")
            return

        volume = getattr(self.sound_system, 'master_volume', 1.0) if self.sound_system is not None else 1.0
        volume *= self.rng.uniform(0.75, 1.0) 

        channel = self._thunder_channel or pygame.mixer.find_channel(True)
        if channel is None:
            return
        channel.set_volume(min(1.0, volume))
        channel.play(sound)

    # ------------------------------------------------------------------
    def get_status_text(self):
        return "thunder" if self.storm_active else ""
