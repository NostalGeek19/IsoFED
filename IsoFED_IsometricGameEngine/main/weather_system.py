import math
import random
import numpy as np
import pygame



KIND_RAIN = 0
KIND_SNOW = 1
KIND_SAND = 2


class WeatherSystem:

    CLEAR_DURATION = (25.0, 60.0)
    RAIN_DURATION = (15.0, 40.0)


    FADE_SPEED = 1.0 / 3.0

    MAX_DROPS = 550

    MAX_SPLASHES = 125
    SPLASH_LIFETIME = 0.35

    DEFAULT_DENSITY = 0.2

    RAIN_VY = (700.0, 1100.0)
    RAIN_VX = (0.0, 0.0)
    RAIN_SIZE = (10.0, 22.0)
    RAIN_RENDER_OFFSET_Y = 22.0
    RAIN_COLOR = (170, 195, 215, 150)

    SNOW_VY = (60.0, 140.0)
    SNOW_VX = (-25.0, 25.0)
    SNOW_SIZE = (1.5, 3.2)
    SNOW_RENDER_OFFSET_Y = 10.0
    SNOW_COLOR = (255, 255, 255, 220)
    SNOW_SWAY_FREQ = (1.0, 2.5)     
    SNOW_SWAY_AMPLITUDE = (20.0, 45.0)  
    SAND_VY = (120.0, 260.0)
    SAND_VX_MAG = (320.0, 650.0)    
    SAND_SIZE = (16.0, 36.0)
    SAND_RENDER_OFFSET_Y = 4.0
    SAND_COLOR = (200, 160, 95, 120)

    def __init__(self, screen_width, screen_height, seed=None, density=None):
        self.rng = random.Random(seed)
        self.screen_width = screen_width
        self.screen_height = screen_height

        self.density = self.DEFAULT_DENSITY if density is None else density

        self.state = 'clear'          
        self.intensity = 0.0          
        self.target_intensity = 0.0   

        self.time_until_change = self.rng.uniform(*self.CLEAR_DURATION)

        self.area_x = 0.0
        self.area_y = 0.0
        self.area_w = float(screen_width)
        self.area_h = float(screen_height)
        self.area_polygon = None  


        self.rain_points = None
        self.snow_points = None
        self.sand_points = None
        self.landing_jitter_x = 0.0
        self.landing_jitter_y = 0.0

        
        self.target_kind = KIND_RAIN
        self.kind_weight = {KIND_RAIN: 1.0, KIND_SNOW: 0.0, KIND_SAND: 0.0}
        self.KIND_FADE_SPEED = 1.0 / 2.0  
        
        self.drops_x = np.zeros(self.MAX_DROPS, dtype=np.float64)
        self.drops_y = np.zeros(self.MAX_DROPS, dtype=np.float64)
        self.drops_vy = np.zeros(self.MAX_DROPS, dtype=np.float64)
        self.drops_vx = np.zeros(self.MAX_DROPS, dtype=np.float64)
        self.drops_size = np.zeros(self.MAX_DROPS, dtype=np.float64)
        self.drops_ground_y = np.full(self.MAX_DROPS, float(screen_height), dtype=np.float64)
        self.drops_kind = np.zeros(self.MAX_DROPS, dtype=np.int8)

        self.drops_sway_phase = np.zeros(self.MAX_DROPS, dtype=np.float64)
        self.drops_sway_freq = np.zeros(self.MAX_DROPS, dtype=np.float64)
        self.drops_sway_amp = np.zeros(self.MAX_DROPS, dtype=np.float64)
        for i in range(self.MAX_DROPS):
            self._respawn_drop(i, randomize_y=True)


        self.splash_x = np.zeros(self.MAX_SPLASHES, dtype=np.float64)
        self.splash_y = np.zeros(self.MAX_SPLASHES, dtype=np.float64)
        self.splash_age = np.full(self.MAX_SPLASHES, self.SPLASH_LIFETIME + 1.0, dtype=np.float64)
        self.splash_kind = np.zeros(self.MAX_SPLASHES, dtype=np.int8)
        self._splash_cursor = 0


        self._overlay_surface = pygame.Surface((screen_width, screen_height), pygame.SRCALPHA)
        self._drops_surface = pygame.Surface((screen_width, screen_height), pygame.SRCALPHA)
        self._splash_surface = pygame.Surface((screen_width, screen_height), pygame.SRCALPHA)

        self._mask_surface = pygame.Surface((screen_width, screen_height), pygame.SRCALPHA)


    def start_rain(self):
        self.state = 'rain'
        self.target_intensity = 1.0
        self.time_until_change = self.rng.uniform(*self.RAIN_DURATION)

    def stop_rain(self):
        self.state = 'clear'
        self.target_intensity = 0.0
        self.time_until_change = self.rng.uniform(*self.CLEAR_DURATION)

    def toggle_rain(self):
        if self.state == 'clear':
            self.start_rain()
        else:
            self.stop_rain()

    def is_raining(self):
        return self.state == 'rain'

    def get_intensity(self):
        return self.intensity

    def set_density(self, density):
        self.density = max(0.0, float(density))

    def get_density(self):
        return self.density

    def set_area(self, x, y, width, height):
        self.area_x = float(x)
        self.area_y = float(y)
        self.area_w = max(1.0, float(width))
        self.area_h = max(1.0, float(height))
        self.area_polygon = None

    def set_area_polygon(self, points):
        points = [(float(px), float(py)) for px, py in points]
        self.area_polygon = points
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        self.area_x = min(xs)
        self.area_y = min(ys)
        self.area_w = max(1.0, max(xs) - min(xs))
        self.area_h = max(1.0, max(ys) - min(ys))

    def set_biome_landing_points(self, rain_points, snow_points, sand_points,
                                  jitter_x=None, jitter_y=None):

        self.rain_points = list(rain_points) if rain_points else None
        self.snow_points = list(snow_points) if snow_points else None
        self.sand_points = list(sand_points) if sand_points else None
        if jitter_x is not None:
            self.landing_jitter_x = float(jitter_x)
        if jitter_y is not None:
            self.landing_jitter_y = float(jitter_y)

    def set_dominant_kind(self, kind):
        if kind not in (KIND_RAIN, KIND_SNOW, KIND_SAND):
            return
        self.target_kind = kind

    def set_landing_points(self, points, jitter_x=None, jitter_y=None):
        self.set_biome_landing_points(points, None, None, jitter_x, jitter_y)

    def reset_area(self):
        self.set_area(0, 0, self.screen_width, self.screen_height)

    @staticmethod
    def _point_in_convex_polygon(x, y, polygon):
        n = len(polygon)
        sign = 0
        for i in range(n):
            x1, y1 = polygon[i]
            x2, y2 = polygon[(i + 1) % n]
            cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
            if cross != 0:
                current_sign = 1 if cross > 0 else -1
                if sign == 0:
                    sign = current_sign
                elif current_sign != sign:
                    return False
        return True


    def resize(self, screen_width, screen_height):
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.reset_area()
        self._overlay_surface = pygame.Surface((screen_width, screen_height), pygame.SRCALPHA)
        self._drops_surface = pygame.Surface((screen_width, screen_height), pygame.SRCALPHA)
        self._splash_surface = pygame.Surface((screen_width, screen_height), pygame.SRCALPHA)
        self._mask_surface = pygame.Surface((screen_width, screen_height), pygame.SRCALPHA)
        for i in range(self.MAX_DROPS):
            self._respawn_drop(i, randomize_y=True)

    def update(self, dt_seconds):
        self.splash_age += dt_seconds

        self.time_until_change -= dt_seconds
        if self.time_until_change <= 0:
            if self.state == 'clear':
                self.start_rain()
            else:
                self.stop_rain()

        if self.intensity < self.target_intensity:
            self.intensity = min(self.target_intensity, self.intensity + self.FADE_SPEED * dt_seconds)
        elif self.intensity > self.target_intensity:
            self.intensity = max(self.target_intensity, self.intensity - self.FADE_SPEED * dt_seconds)

        for k in (KIND_RAIN, KIND_SNOW, KIND_SAND):
            target_w = 1.0 if k == self.target_kind else 0.0
            w = self.kind_weight[k]
            if w < target_w:
                self.kind_weight[k] = min(target_w, w + self.KIND_FADE_SPEED * dt_seconds)
            elif w > target_w:
                self.kind_weight[k] = max(target_w, w - self.KIND_FADE_SPEED * dt_seconds)

        if self.intensity <= 0.0:
            return

        active = int(self.MAX_DROPS * self.intensity * self.density)
        if active == 0:
            return

        kind_has_points = (
            bool(self.rain_points),
            bool(self.snow_points),
            bool(self.sand_points),
        )
        stale_mask = ~np.array(kind_has_points, dtype=bool)[self.drops_kind[:active]]
        stale_indices = np.nonzero(stale_mask)[0]
        for i in stale_indices:
            self._respawn_drop(int(i))

        self.drops_y[:active] += self.drops_vy[:active] * dt_seconds

        snow_mask = self.drops_kind[:active] == KIND_SNOW
        self.drops_sway_phase[:active][snow_mask] += self.drops_sway_freq[:active][snow_mask] * dt_seconds
        sway = np.sin(self.drops_sway_phase[:active][snow_mask]) * self.drops_sway_amp[:active][snow_mask]
        self.drops_x[:active][snow_mask] += sway * dt_seconds
        self.drops_x[:active] += self.drops_vx[:active] * dt_seconds

        respawn_mask = self.drops_y[:active] > self.drops_ground_y[:active]
        respawn_indices = np.nonzero(respawn_mask)[0]
        for i in respawn_indices:
            self._spawn_splash(self.drops_x[i], self.drops_ground_y[i], int(self.drops_kind[i]))
            self._respawn_drop(int(i))

    def _spawn_splash(self, x, y, kind):
        i = self._splash_cursor
        self.splash_x[i] = x
        self.splash_y[i] = y
        self.splash_age[i] = 0.0
        self.splash_kind[i] = kind
        self._splash_cursor = (i + 1) % self.MAX_SPLASHES

    def _pick_kind_and_point(self):
        pools = []
        if self.rain_points:
            pools.append((KIND_RAIN, self.rain_points))
        if self.snow_points:
            pools.append((KIND_SNOW, self.snow_points))
        if self.sand_points:
            pools.append((KIND_SAND, self.sand_points))

        if pools:
            weights = [len(p) * self.kind_weight[kind] for kind, p in pools]
            if sum(weights) <= 0.0:

                weights = [len(p) for _, p in pools]
            kind, points = self.rng.choices(pools, weights=weights, k=1)[0]
            x, y = self.rng.choice(points)
            if self.landing_jitter_x:
                x += self.rng.uniform(-self.landing_jitter_x, self.landing_jitter_x)
            if self.landing_jitter_y:
                y += self.rng.uniform(-self.landing_jitter_y, self.landing_jitter_y)
            return kind, x, y

        if self.area_polygon is not None:
            for _ in range(20):
                x = self.rng.uniform(self.area_x, self.area_x + self.area_w)
                y = self.rng.uniform(self.area_y, self.area_y + self.area_h)
                if self._point_in_convex_polygon(x, y, self.area_polygon):
                    return KIND_RAIN, x, y
            return KIND_RAIN, self.area_x + self.area_w / 2, self.area_y + self.area_h / 2
        else:
            x = self.rng.uniform(self.area_x, self.area_x + self.area_w)
            ground_top = self.area_y + self.area_h * 0.35
            ground_bottom = self.area_y + self.area_h
            y = self.rng.uniform(ground_top, ground_bottom)
            return KIND_RAIN, x, y

    def _respawn_drop(self, i, randomize_y=False):
        kind, x, ground_y = self._pick_kind_and_point()
        self.drops_kind[i] = kind
        self.drops_x[i] = x
        self.drops_ground_y[i] = ground_y

        if kind == KIND_SNOW:
            self.drops_vy[i] = self.rng.uniform(*self.SNOW_VY)
            self.drops_vx[i] = self.rng.uniform(*self.SNOW_VX)
            self.drops_size[i] = self.rng.uniform(*self.SNOW_SIZE)
            self.drops_sway_freq[i] = self.rng.uniform(*self.SNOW_SWAY_FREQ)
            self.drops_sway_amp[i] = self.rng.uniform(*self.SNOW_SWAY_AMPLITUDE)
            self.drops_sway_phase[i] = self.rng.uniform(0, 2 * math.pi)
        elif kind == KIND_SAND:
            self.drops_vy[i] = self.rng.uniform(*self.SAND_VY)
            vx_mag = self.rng.uniform(*self.SAND_VX_MAG)
            vx_sign = self.rng.choice((-1.0, 1.0))
            self.drops_vx[i] = vx_mag * vx_sign
            self.drops_size[i] = self.rng.uniform(*self.SAND_SIZE)

            travel_time = self.rng.uniform(0.3, 0.9)
            self.drops_x[i] = x - self.drops_vx[i] * travel_time
        else:  # KIND_RAIN
            self.drops_vy[i] = self.rng.uniform(*self.RAIN_VY)
            self.drops_vx[i] = 0.0
            self.drops_size[i] = self.rng.uniform(*self.RAIN_SIZE)

        if randomize_y:
            self.drops_y[i] = self.rng.uniform(self.area_y, ground_y)
        else:
            self.drops_y[i] = self.area_y - self.rng.uniform(0, 40)


    def _biome_weights(self):
        w_rain = self.kind_weight[KIND_RAIN] if self.rain_points else 0.0
        w_snow = self.kind_weight[KIND_SNOW] if self.snow_points else 0.0
        w_sand = self.kind_weight[KIND_SAND] if self.sand_points else 0.0
        total = w_rain + w_snow + w_sand
        if total <= 0.0:
            return 1.0, 0.0, 0.0
        return w_rain / total, w_snow / total, w_sand / total

    def render(self, screen):
        if self.intensity <= 0.01:
            return

        area_rect = pygame.Rect(int(self.area_x), int(self.area_y), int(self.area_w), int(self.area_h))

        previous_clip = screen.get_clip()
        screen.set_clip(area_rect)

        w_rain, w_snow, w_sand = self._biome_weights()
        rain_tint = (20, 25, 35)
        snow_tint = (150, 160, 175)
        sand_tint = (90, 65, 30)
        tint = tuple(
            int(rain_tint[c] * w_rain + snow_tint[c] * w_snow + sand_tint[c] * w_sand)
            for c in range(3)
        )
        overlay_alpha = int(70 * self.intensity)
        self._overlay_surface.fill((*tint, overlay_alpha))

        self._drops_surface.fill((0, 0, 0, 0))
        active = int(self.MAX_DROPS * self.intensity * self.density)
        kinds = self.drops_kind[:active]
        xs = self.drops_x[:active]
        ys_raw = self.drops_y[:active]
        vxs = self.drops_vx[:active]
        sizes = self.drops_size[:active]

        rain_idx = np.nonzero(kinds == KIND_RAIN)[0]
        for i in rain_idx:
            x = xs[i]
            y = ys_raw[i] - self.RAIN_RENDER_OFFSET_Y
            pygame.draw.line(self._drops_surface, self.RAIN_COLOR, (x, y), (x, y + sizes[i]), 1)

        snow_idx = np.nonzero(kinds == KIND_SNOW)[0]
        for i in snow_idx:
            x = xs[i]
            y = ys_raw[i] - self.SNOW_RENDER_OFFSET_Y
            r = sizes[i]
            pygame.draw.circle(self._drops_surface, self.SNOW_COLOR, (int(x), int(y)), max(1, int(r)))

        sand_idx = np.nonzero(kinds == KIND_SAND)[0]
        for i in sand_idx:
            x = xs[i]
            y = ys_raw[i] - self.SAND_RENDER_OFFSET_Y
            vx = vxs[i]
            length = sizes[i]

            direction = 1.0 if vx >= 0 else -1.0
            end_x = x - direction * length
            end_y = y - length * 0.15
            pygame.draw.line(self._drops_surface, self.SAND_COLOR, (x, y), (end_x, end_y), 2)


        self._splash_surface.fill((0, 0, 0, 0))
        alive_mask = self.splash_age < self.SPLASH_LIFETIME
        alive_indices = np.nonzero(alive_mask)[0]
        for i in alive_indices:
            progress = self.splash_age[i] / self.SPLASH_LIFETIME 
            kind = self.splash_kind[i]
            x = self.splash_x[i]
            y = self.splash_y[i]
            
            if kind == KIND_SNOW:
                alpha = int(200 * (1.0 - progress))
                if alpha <= 0:
                    continue
                radius = 2.0 + progress * 1.5
                pygame.draw.circle(self._splash_surface, (255, 255, 255, alpha), (int(x), int(y)), max(1, int(radius)))
            elif kind == KIND_SAND:
                alpha = int(150 * (1.0 - progress))
                if alpha <= 0:
                    continue
                radius_x = 4.0 + progress * 10.0
                radius_y = radius_x * 0.35
                rect = (x - radius_x, y - radius_y, radius_x * 2, radius_y * 2)
                pygame.draw.ellipse(self._splash_surface, (*self.SAND_COLOR[:3], alpha), rect, 1)
            else:  # KIND_RAIN
                radius_x = 3.0 + progress * 7.0
                radius_y = radius_x * 0.45  
                alpha = int(190 * (1.0 - progress))
                if alpha <= 0:
                    continue
                rect = (x - radius_x, y - radius_y, radius_x * 2, radius_y * 2)
                pygame.draw.ellipse(self._splash_surface, (210, 225, 240, alpha), rect, 1)

        if self.area_polygon is not None:
            self._mask_surface.fill((0, 0, 0, 0))
            pygame.draw.polygon(self._mask_surface, (255, 255, 255, 255), self.area_polygon)
            self._overlay_surface.blit(self._mask_surface, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
            self._drops_surface.blit(self._mask_surface, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
            self._splash_surface.blit(self._mask_surface, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)

        screen.blit(self._overlay_surface, (0, 0))
        screen.blit(self._drops_surface, (0, 0))
        screen.blit(self._splash_surface, (0, 0))

        screen.set_clip(previous_clip)

    def get_status_text(self):
        if self.intensity <= 0.01:
            return "clear"
        
        names = {KIND_RAIN: "rain", KIND_SNOW: "snow", KIND_SAND: "sand"}
        target_name = names[self.target_kind]
        
        fading = [names[k] for k in (KIND_RAIN, KIND_SNOW, KIND_SAND)
                  if k != self.target_kind and self.kind_weight[k] > 0.05]
        
        if fading:
            return f"{'/'.join(fading)}→{target_name} {int(self.intensity * 100)}%"
        return f"{target_name} {int(self.intensity * 100)}%"